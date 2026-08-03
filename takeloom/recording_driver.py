"""Shared physical Stream Deck driver for every recording context (Tk UI,
headless `takeloom server`, and the CLI) — one place for button layout,
key dispatch, and state, so the deck behaves identically no matter which
one is driving it.

This module touches only `Backend` and `StreamDeckController` — nothing
Tk-specific, nothing terminal-specific. A context supplies two hooks:

- `resolve_start_request()`: how to pick a project/instrument/track when
  "r" (idle) or "n" is pressed. The Tk UI answers from whatever's selected
  in its Setlist/Inspiration picker; the headless server and CLI answer
  from the last-used project/instrument plus the next untaken track.
- `on_video_check_result(path, has_video)`: how to hand off a finished
  video check — there's no Stream Deck button for starting one anymore
  (key index 3 is now the Live/Production monitor toggle, "m"), but a
  check started from the Tk UI or a Remote client still finishes here.
  The Tk UI opens a review dialog; headless/CLI open the OS default
  player directly (no GUI to pop a window in).

Both hooks — and `log()` — may be called from a background thread (the
Stream Deck's own key-event thread, or a backend event callback); a
context whose hooks touch UI-toolkit state is responsible for marshalling
onto its own main thread itself, the same way it already does for any
other cross-thread backend call.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from .backend import Backend, BackendError, StartRecordingRequest
from .streamdeck_controller import StreamDeckController


class RecordingDeckDriver:
    def __init__(
        self,
        backend: Backend,
        resolve_start_request: Callable[[], StartRecordingRequest | None],
        on_video_check_result: Callable[[Path, bool], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._backend = backend
        self._resolve_start_request = resolve_start_request
        self._on_video_check_result = on_video_check_result
        self._log = log or (lambda msg: None)
        self.streamdeck = StreamDeckController()
        self.phase = "idle"  # "idle" | "waiting" | "recording"
        self.video_check_phase = "idle"  # "idle" | "recording"
        self._events_subscribed = False

    # --- connect / disconnect ---

    def _subscribe_backend_events(self) -> None:
        if not self._events_subscribed:
            self._backend.on_event(self._on_backend_event)
            self._events_subscribed = True

    def _unsubscribe_backend_events(self) -> None:
        if self._events_subscribed:
            self._backend.off_event(self._on_backend_event)
            self._events_subscribed = False

    def connect(self, key_callback: Callable[[str], None] | None = None) -> bool:
        """Start listening for backend events — this drives phase tracking
        and hooks like on_video_check_result() regardless of whether a
        physical Stream Deck is present, since e.g. a mouse-triggered video
        check still needs its result handed off. Then, separately, open the
        Stream Deck selected in settings (StudioConfig.streamdeck_id): does
        no hardware probing at all if none is configured. `key_callback`, if
        given, wraps handle_key (e.g. to marshal onto a UI's main thread) —
        otherwise handle_key is used directly. Returns whether an actual
        Stream Deck connected."""
        self._subscribe_backend_events()
        try:
            device_id = self._backend.get_config().streamdeck_id
        except BackendError:
            device_id = ""
        if not device_id:
            self._log("Skipping StreamDeck initialization, as none are configured.")
            return False
        if not self.streamdeck.connect(key_callback or self.handle_key, device_id=device_id):
            return False
        self.streamdeck.use_recording_layout()
        self.streamdeck.update_recording_page(self.phase, self.video_check_phase)
        self._refresh_monitoring_mode()
        return True

    def disconnect(self) -> None:
        self._unsubscribe_backend_events()
        self.streamdeck.disconnect()

    def rebind_backend(self, backend: Backend) -> None:
        """Point this driver at a different backend (e.g. the Tk UI
        connecting to/disconnecting from a Remote) without touching the
        physical Stream Deck connection itself. Resets phase state to idle
        since the new backend's actual state is unknown until its first
        event arrives. Always re-subscribes to the new backend's events,
        independent of whether a physical Stream Deck is connected — see
        connect()."""
        self._unsubscribe_backend_events()
        self._backend = backend
        self.phase = "idle"
        self.video_check_phase = "idle"
        self._subscribe_backend_events()
        self.streamdeck.update_recording_page(self.phase, self.video_check_phase)
        self._refresh_monitoring_mode()

    def _refresh_monitoring_mode(self) -> None:
        try:
            self.streamdeck.update_monitoring_mode(self._backend.get_monitoring_mode())
        except BackendError:
            pass

    # --- key dispatch (the single canonical behavior for every context) ---

    def handle_key(self, key: str) -> None:
        try:
            if key == "r":
                if self.phase == "idle":
                    self._start_recording()
                elif self.phase == "waiting":
                    self._backend.unpause_recording()
                elif self.phase == "recording":
                    self._backend.stop_recording()
            elif key == "n":
                self._advance_to_next_track()
            elif key == "b":
                if self.phase == "recording":
                    self._backend.restart_take()
            elif key == "m":
                self._toggle_monitoring_mode()
            elif key in ("l", "u", "[", "]", ",", "."):
                delta = 5 if key in ("u", "]", ".") else -5
                if key in ("[", "]"):
                    self._backend.adjust_takes_volume(delta)
                elif key in (",", "."):
                    self._backend.adjust_instrument_volume(delta)
                else:
                    self._backend.adjust_backing_volume(delta)
        except BackendError as e:
            self._log(f"StreamDeck: {e}")

    def _start_recording(self) -> None:
        req = self._resolve_start_request()
        if req is not None:
            self._backend.start_recording(req)

    def _toggle_monitoring_mode(self) -> None:
        current = self._backend.get_monitoring_mode()
        self._backend.set_monitoring_mode("production" if current == "recording" else "recording")
        # _on_backend_event below also redraws the key once the backend's
        # monitoring_mode_changed event arrives — for a RemoteBackend that's
        # a second network round trip away, so update it right away here
        # too rather than leaving the key stale until the event catches up.
        self._refresh_monitoring_mode()

    def _advance_to_next_track(self) -> None:
        """Next: always available. If a take is in progress (waiting or
        recording), discard it and advance from there; otherwise start from
        wherever resolve_start_request() would currently target. Same
        behavior everywhere — previously the UI only allowed this while
        idle and the headless server only while recording."""
        if self.phase != "idle":
            target = self._backend.get_active_recording_target()
            self._backend.stop_recording()
            if target is None:
                return
            project_name, instrument_name, current_index = target
            start_index = current_index + 1
            # stop_recording() just closed the audio stream and
            # start_recording() below opens a brand new one on the same
            # physical interface — every normal session already does this
            # between songs, but always with a natural human-paced gap.
            # Doing it at machine speed, back to back, is the one new
            # pattern Next introduces; give the audio driver a moment to
            # fully settle rather than slamming it shut and immediately
            # back open.
            time.sleep(0.5)
        else:
            req = self._resolve_start_request()
            if req is None or req.track_source != "setlist":
                return
            project_name, instrument_name = req.project_name, req.instrument_name
            start_index = (req.track_index or 0) + 1

        index = self._backend.next_untaken_track_index(project_name, instrument_name, start_index)
        if index is None:
            self._log(f"No more tracks in '{project_name}' need a take for '{instrument_name}'.")
            return
        self._backend.start_recording(StartRecordingRequest(
            project_name=project_name, instrument_name=instrument_name,
            track_source="setlist", track_index=index,
        ))

    # --- backend events ---

    def _on_backend_event(self, event: str, data: dict) -> None:
        if event == "recording_status":
            if "phase" in data:
                self.phase = data["phase"]
                self.streamdeck.update_recording_page(self.phase, self.video_check_phase)
        elif event == "video_check_status":
            # RemoteServer never broadcasts the raw video_check_status a
            # server-side check emits (its result_path only means anything
            # on that machine). Over a Remote connection this event only
            # ever reaches us re-dispatched by RemoteBackend after it's
            # transferred the finished file and rewritten result_path to a
            # local copy (see RemoteBackend._on_raw_event's "video_check_
            # result" handling) — so by the time it's here, result_path is
            # always valid for this machine, local or remote alike.
            if "status" in data:
                self._log(data["status"])
            if "phase" in data:
                self.video_check_phase = data["phase"]
                self.streamdeck.update_recording_page(self.phase, self.video_check_phase)
            if self.video_check_phase == "idle" and "result_path" in data and self._on_video_check_result:
                self._on_video_check_result(Path(data["result_path"]), bool(data.get("has_video")))
        elif event == "monitoring_mode_changed":
            # Fired by set_monitoring_mode() from any client (this deck's
            # own "m" key, the Tk UI's radio toggle, or a Remote client) —
            # keeps the physical key in sync regardless of who changed it.
            if "mode" in data:
                self.streamdeck.update_monitoring_mode(data["mode"])
