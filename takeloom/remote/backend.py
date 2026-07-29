"""RemoteBackend: adapts takeloom.backend.Backend over a RemoteClient, so UI
tabs can't tell whether they're talking to local hardware or a remote
takeloom instance."""

from __future__ import annotations

import base64
import threading
from typing import Callable

from ..backend import (
    Backend,
    BackendError,
    EventCallback,
    FrameCallback,
    PreviewSubscription,
    StartRecordingRequest,
)
from ..config import StudioConfig
from .client import RemoteClient

# Recording/inspiration calls touch remote disk, network, and hardware —
# give them more room than simple config/device lookups.
LONG_TIMEOUT = 20.0

# yt-dlp downloads can take a while on a slow connection or a long video.
DOWNLOAD_TIMEOUT = 300.0


class RemoteBackend(Backend):
    def __init__(self, client: RemoteClient) -> None:
        self._client = client
        self._event_callbacks: list[EventCallback] = []
        self._preview_lock = threading.Lock()
        self._preview_callbacks: list[FrameCallback] = []
        self._preview_subscribed = False
        client._on_event = self._on_raw_event

    def is_remote(self) -> bool:
        return True

    def hostname(self) -> str:
        return self._client.hostname

    def close(self) -> None:
        self._client.close()

    # --- config ---

    def get_config(self) -> StudioConfig:
        result = self._client.call("get_config", {})
        return StudioConfig.from_dict(result["config"])

    def save_config(self, config: StudioConfig) -> None:
        self._client.call("save_config", {"config": config.to_dict()})

    # --- devices ---

    def list_audio_devices(self) -> list[dict]:
        return self._client.call("list_audio_devices", {})["devices"]

    def list_cameras(self) -> list[tuple[str, str]]:
        return [tuple(c) for c in self._client.call("list_cameras", {})["cameras"]]

    def refresh_devices(self) -> None:
        self._client.call("refresh_devices", {})

    # --- projects / setlists ---

    def list_projects(self) -> list[str]:
        return self._client.call("list_projects", {})["projects"]

    def get_setlist(self, project_name: str) -> dict:
        return self._client.call("get_setlist", {"project_name": project_name})["setlist"]

    def save_setlist(self, project_name: str, setlist_data: dict) -> None:
        self._client.call("save_setlist", {"project_name": project_name, "setlist": setlist_data})

    def create_project(self, name: str) -> str:
        return self._client.call("create_project", {"name": name}, timeout=LONG_TIMEOUT)["name"]

    def add_local_backing_track(
        self, project_name: str, source_path: str, track_name: str | None = None,
    ) -> dict:
        raise BackendError(
            "Adding local files as backing tracks isn't available over Remote connections "
            "yet — paste a YouTube URL instead."
        )

    def add_youtube_backing_track(
        self, project_name: str, url: str, on_progress: Callable[[float | None, str], None] | None = None,
    ) -> dict:
        if on_progress:
            on_progress(None, "Downloading on the remote studio (live progress isn't available over Remote yet)...")
        return self._client.call(
            "add_youtube_backing_track", {"project_name": project_name, "url": url}, timeout=DOWNLOAD_TIMEOUT,
        )

    # --- inspiration ---

    def query_inspiration_tracks(self, project_name: str) -> list[dict]:
        result = self._client.call(
            "query_inspiration_tracks", {"project_name": project_name}, timeout=LONG_TIMEOUT,
        )
        return result["tracks"]

    # --- recording ---

    def start_recording(self, req: StartRecordingRequest) -> None:
        self._client.call(
            "start_recording",
            {
                "project_name": req.project_name,
                "instrument_name": req.instrument_name,
                "track_source": req.track_source,
                "track_index": req.track_index,
                "inspiration_info": req.inspiration_info,
            },
            timeout=LONG_TIMEOUT,
        )

    def unpause_recording(self) -> None:
        self._client.call("unpause_recording", {}, timeout=LONG_TIMEOUT)

    def stop_recording(self) -> None:
        self._client.call("stop_recording", {}, timeout=LONG_TIMEOUT)

    def is_recording(self) -> bool:
        return self._client.call("is_recording", {})["recording"]

    def adjust_backing_volume(self, delta: int) -> None:
        self._client.call("adjust_backing_volume", {"delta": delta})

    def adjust_takes_volume(self, delta: int) -> None:
        self._client.call("adjust_takes_volume", {"delta": delta})

    def on_event(self, callback: EventCallback) -> None:
        self._event_callbacks.append(callback)

    def off_event(self, callback: EventCallback) -> None:
        if callback in self._event_callbacks:
            self._event_callbacks.remove(callback)

    # --- camera preview ---

    def open_camera_preview(self, on_frame: FrameCallback) -> PreviewSubscription:
        with self._preview_lock:
            self._preview_callbacks.append(on_frame)
            if not self._preview_subscribed:
                self._preview_subscribed = True
                try:
                    self._client.call("subscribe_preview", {})
                except BackendError:
                    self._preview_subscribed = False
        return _RemotePreviewSubscription(self, on_frame)

    # --- camera latency test (not supported over Remote; see LatencyFrame) ---

    def start_latency_test(self, instrument_name: str, camera_device: str, play_metronome: bool = True) -> None:
        raise BackendError("Camera latency measurement isn't available over Remote connections yet.")

    def stop_latency_test(self) -> None:
        raise BackendError("Camera latency measurement isn't available over Remote connections yet.")

    # --- sound check (not supported over Remote; see RecordFrame) ---

    def start_sound_check(self, req: StartRecordingRequest) -> None:
        raise BackendError("Sound check isn't available over Remote connections yet.")

    def stop_sound_check(self) -> None:
        raise BackendError("Sound check isn't available over Remote connections yet.")

    # --- continuous session recording (not supported over Remote; CLI-only) ---

    def begin_session(self, project_name: str, instrument_name: str) -> None:
        raise BackendError("Continuous session recording isn't available over Remote connections.")

    def end_session(self) -> None:
        raise BackendError("Continuous session recording isn't available over Remote connections.")

    def _unsubscribe_preview(self, on_frame: FrameCallback) -> None:
        with self._preview_lock:
            if on_frame in self._preview_callbacks:
                self._preview_callbacks.remove(on_frame)
            if not self._preview_callbacks and self._preview_subscribed:
                self._preview_subscribed = False
                try:
                    self._client.call("unsubscribe_preview", {})
                except BackendError:
                    pass

    # --- event dispatch from the RemoteClient's reader thread ---

    def _on_raw_event(self, event: str, data: dict) -> None:
        if event == "preview_frame":
            try:
                jpeg = base64.b64decode(data["jpeg_b64"])
            except Exception:
                return
            with self._preview_lock:
                callbacks = list(self._preview_callbacks)
            for cb in callbacks:
                try:
                    cb(jpeg)
                except Exception:
                    pass
            return

        for cb in list(self._event_callbacks):
            try:
                cb(event, data)
            except Exception:
                pass


class _RemotePreviewSubscription(PreviewSubscription):
    def __init__(self, backend: RemoteBackend, callback: FrameCallback) -> None:
        self._backend = backend
        self._callback = callback
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._backend._unsubscribe_preview(self._callback)
