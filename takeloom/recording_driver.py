"""Shared physical Stream Deck driver for every recording context (Tk UI,
headless `takeloom server`, and the CLI) — one place for button layout,
key dispatch, and state, so the deck behaves identically no matter which
one is driving it.

This module touches only `Backend` and `StreamDeckController` — nothing
Tk-specific, nothing terminal-specific. A context supplies two hooks:

- `resolve_start_request()`: how to pick a project/instrument/track when
  "r" (idle) or "c" is pressed. The Tk UI answers from whatever's selected
  in its Setlist/Inspiration picker; the headless server and CLI answer
  from the last-used project/instrument plus the next untaken track.
- `on_sound_check_result(path, has_video)`: how to hand off a finished
  sound check. The Tk UI opens a review dialog; headless/CLI open the OS
  default player directly (no GUI to pop a window in).

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
        on_sound_check_result: Callable[[Path, bool], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._backend = backend
        self._resolve_start_request = resolve_start_request
        self._on_sound_check_result = on_sound_check_result
        self._log = log or (lambda msg: None)
        self.streamdeck = StreamDeckController()
        self.phase = "idle"  # "idle" | "waiting" | "recording"
        self.sound_check_phase = "idle"  # "idle" | "recording"

    # --- connect / disconnect ---

    def connect(self, key_callback: Callable[[str], None] | None = None) -> bool:
        """Open the Stream Deck and start listening for backend events.
        `key_callback`, if given, wraps handle_key (e.g. to marshal onto a
        UI's main thread) — otherwise handle_key is used directly."""
        if not self.streamdeck.connect(key_callback or self.handle_key):
            return False
        self.streamdeck.use_recording_layout()
        self.streamdeck.update_recording_page(self.phase, self.sound_check_phase)
        self._backend.on_event(self._on_backend_event)
        return True

    def disconnect(self) -> None:
        self._backend.off_event(self._on_backend_event)
        self.streamdeck.disconnect()

    def rebind_backend(self, backend: Backend) -> None:
        """Point this driver at a different backend (e.g. the Tk UI
        connecting to/disconnecting from a Remote) without touching the
        physical Stream Deck connection itself. Resets phase state to idle
        since the new backend's actual state is unknown until its first
        event arrives. No-op if not currently connected."""
        if not self.streamdeck.connected:
            self._backend = backend
            return
        self._backend.off_event(self._on_backend_event)
        self._backend = backend
        self.phase = "idle"
        self.sound_check_phase = "idle"
        self._backend.on_event(self._on_backend_event)
        self.streamdeck.update_recording_page(self.phase, self.sound_check_phase)

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
            elif key == "c":
                if self.sound_check_phase == "idle":
                    self._start_sound_check()
                else:
                    self._backend.stop_sound_check()
            elif key == "n":
                self._advance_to_next_track()
            elif key == "b":
                if self.phase == "recording":
                    self._backend.restart_take()
            elif key in ("l", "u", "[", "]"):
                delta = 5 if key in ("u", "]") else -5
                if key in ("[", "]"):
                    self._backend.adjust_takes_volume(delta)
                else:
                    self._backend.adjust_backing_volume(delta)
        except BackendError as e:
            self._log(f"StreamDeck: {e}")

    def _start_recording(self) -> None:
        req = self._resolve_start_request()
        if req is not None:
            self._backend.start_recording(req)

    def _start_sound_check(self) -> None:
        req = self._resolve_start_request()
        if req is not None:
            self._backend.start_sound_check(req)

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
            if req is None or req.track_source != "playlist":
                return
            project_name, instrument_name = req.project_name, req.instrument_name
            start_index = (req.track_index or 0) + 1

        index = self._backend.next_untaken_track_index(project_name, instrument_name, start_index)
        if index is None:
            self._log(f"No more tracks in '{project_name}' need a take for '{instrument_name}'.")
            return
        self._backend.start_recording(StartRecordingRequest(
            project_name=project_name, instrument_name=instrument_name,
            track_source="playlist", track_index=index,
        ))

    # --- backend events ---

    def _on_backend_event(self, event: str, data: dict) -> None:
        if event == "recording_status":
            if "phase" in data:
                self.phase = data["phase"]
                self.streamdeck.update_recording_page(self.phase, self.sound_check_phase)
        elif event == "sound_check_status":
            # Sound check is local-only — RemoteBackend refuses to ever
            # trigger one — so a sound_check_status event reaching a Remote-
            # connected driver can only be RemoteServer's blanket broadcast
            # of another client's/the server's own local sound check (see
            # RemoteServer._on_backend_event, which forwards every backend
            # event to every connected client unfiltered). Its result_path
            # names a file on THAT machine, not this one — never act on it
            # here, or e.g. open_in_default_player() gets handed a path that
            # doesn't exist locally.
            if self._backend.is_remote():
                return
            if "status" in data:
                self._log(data["status"])
            if "phase" in data:
                self.sound_check_phase = data["phase"]
                self.streamdeck.update_recording_page(self.phase, self.sound_check_phase)
            if self.sound_check_phase == "idle" and "result_path" in data and self._on_sound_check_result:
                self._on_sound_check_result(Path(data["result_path"]), bool(data.get("has_video")))
