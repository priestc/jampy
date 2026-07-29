"""Backend abstraction: everything a UI tab needs, independent of whether the
data/hardware lives on this machine or a remote takeloom instance.

`LocalBackend` talks directly to local config, disk, and audio/video
hardware — this is the historical behavior of the UI tabs, just extracted
behind an interface. `RemoteBackend` (in `takeloom/remote/backend.py`) adapts
the same interface over the network to a `RemoteServer` running elsewhere,
which itself wraps its own `LocalBackend`.

Nothing in this module touches tkinter. Callers on the UI side are
responsible for running blocking calls on a background thread and
marshalling results back to the Tk thread (e.g. via `widget.after(0, ...)`).
Event callbacks registered via `on_event`/camera-preview frame callbacks may
be invoked from a non-UI thread for the same reason.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import DEFAULT_CONFIG_PATH, StudioConfig
from .project import Project, Setlist, TakeInfo, TrackEntry
from .utils import ensure_dir, next_take_number, take_filename, timestamp_now, wall_timestamp


class BackendError(Exception):
    """Raised by any Backend method on failure. Message is safe to show to the user."""


@dataclass
class StartRecordingRequest:
    project_name: str
    instrument_name: str
    track_source: str  # "playlist" | "inspiration"
    track_index: int | None = None       # for track_source == "playlist"
    inspiration_info: dict | None = None  # for track_source == "inspiration"


EventCallback = Callable[[str, dict], None]
FrameCallback = Callable[[bytes], None]


class PreviewSubscription:
    """Returned by open_camera_preview(); call close() to stop receiving frames."""

    def close(self) -> None:  # pragma: no cover - overridden by subclasses
        raise NotImplementedError


class Backend(ABC):
    """Interface every UI tab depends on (constructor-injected via AppState)."""

    @abstractmethod
    def hostname(self) -> str: ...

    def is_remote(self) -> bool:
        return False

    def close(self) -> None:
        pass

    # --- config ---

    @abstractmethod
    def get_config(self) -> StudioConfig: ...

    @abstractmethod
    def save_config(self, config: StudioConfig) -> None: ...

    # --- devices ---

    @abstractmethod
    def list_audio_devices(self) -> list[dict]: ...

    @abstractmethod
    def list_cameras(self) -> list[tuple[str, str]]: ...

    def list_streamdecks(self) -> list[tuple[str, str]]:
        """(serial_number, label) pairs for every physically attached Stream
        Deck. Concrete default (not abstract) since a Stream Deck is
        inherently local hardware — RemoteBackend has nothing meaningful to
        return and just inherits this empty-list default rather than
        proxying it over the wire, the same reasoning as get_levels()."""
        return []

    @abstractmethod
    def refresh_devices(self) -> None: ...

    # --- projects / setlists ---

    @abstractmethod
    def list_projects(self) -> list[str]: ...

    @abstractmethod
    def get_setlist(self, project_name: str) -> dict: ...

    @abstractmethod
    def save_setlist(self, project_name: str, setlist_data: dict) -> None: ...

    def next_untaken_track_index(
        self, project_name: str, instrument_name: str, start_index: int = 0,
    ) -> int | None:
        """Index of the first playlist track from start_index onward that
        doesn't already have a take for instrument_name, or None if every
        remaining track already has one. Pure setlist query on top of
        get_setlist() — concrete here (not per-subclass) since it needs no
        hardware access and works identically for Local and Remote.

        The single shared "what's next" primitive every recording-driving
        context (Tk UI's StreamDeck Next key, headless takeloom server,
        the CLI) uses instead of each reimplementing this search."""
        setlist = Setlist.from_dict(self.get_setlist(project_name))
        for i in range(start_index, len(setlist.tracks)):
            if setlist.tracks[i].get_take_for_instrument(instrument_name) is None:
                return i
        return None

    @abstractmethod
    def create_project(self, name: str) -> str:
        """Create a new, empty project. Returns its final (sanitized) name."""
        ...

    @abstractmethod
    def add_local_backing_track(
        self, project_name: str, source_path: str, track_name: str | None = None,
    ) -> dict:
        """Add a local audio or video file as a backing track (a video's
        audio stream is extracted for playback/mixing). `source_path` must
        be reachable on the machine the backend actually runs on —
        RemoteBackend refuses, since a path on the controlling client isn't
        reachable from the remote studio's disk."""
        ...

    @abstractmethod
    def add_youtube_backing_track(
        self, project_name: str, url: str, on_progress: Callable[[float | None, str], None] | None = None,
    ) -> dict:
        """Download a full YouTube video (via yt-dlp) and add it as a backing
        track; its audio stream is extracted for playback/mixing as needed.

        If given, on_progress(percent, message) reports live download
        progress — percent is 0-100 when known, else None with just a
        status message. RemoteBackend can't stream this live over its
        simple request/response RPC, so it calls on_progress once with a
        placeholder message instead."""
        ...

    # --- inspiration ---

    @abstractmethod
    def query_inspiration_tracks(self, project_name: str) -> list[dict]: ...

    # --- recording ---

    @abstractmethod
    def start_recording(self, req: StartRecordingRequest) -> None: ...

    @abstractmethod
    def unpause_recording(self) -> None: ...

    @abstractmethod
    def stop_recording(self) -> None: ...

    @abstractmethod
    def is_recording(self) -> bool: ...

    def get_levels(self) -> tuple[float, float]:
        """Returns (instrument_peak, backing_peak), each 0.0-1.0, for the
        Record tab's VU meters. Not every backend can report this cheaply
        (e.g. RemoteBackend, absent a dedicated streaming RPC); the default
        is silence rather than an error."""
        return (0.0, 0.0)

    @abstractmethod
    def adjust_backing_volume(self, delta: int) -> None: ...

    @abstractmethod
    def adjust_takes_volume(self, delta: int) -> None: ...

    @abstractmethod
    def on_event(self, callback: EventCallback) -> None: ...

    @abstractmethod
    def off_event(self, callback: EventCallback) -> None: ...

    # --- camera preview ---

    @abstractmethod
    def open_camera_preview(self, on_frame: FrameCallback) -> PreviewSubscription: ...

    # --- camera latency test (local-only; RemoteBackend refuses) ---

    @abstractmethod
    def start_latency_test(self, instrument_name: str, camera_device: str, play_metronome: bool = True) -> None: ...

    @abstractmethod
    def stop_latency_test(self) -> None: ...

    # --- sound check (local-only; RemoteBackend refuses) ---

    @abstractmethod
    def start_sound_check(self, req: StartRecordingRequest) -> None:
        """Impromptu, throwaway recording against the currently selected
        backing track (same track/instrument selection as start_recording),
        for the performer to verify mic/camera/levels are set up correctly.
        Never touches the project's completed_takes_dir or setlist."""
        ...

    @abstractmethod
    def stop_sound_check(self) -> None: ...

    # --- continuous multi-track session recording (local-only; RemoteBackend refuses) ---

    @abstractmethod
    def begin_session(self, project_name: str, instrument_name: str) -> None:
        """Start a continuous whole-session audio+video recording spanning
        every track subsequently recorded via start_recording() for this
        project/instrument, plus a JSON event log — on top of, not instead
        of, each track's normal per-take recording. Used by the CLI's
        `start-session` command. end_session() finalizes it."""
        ...

    @abstractmethod
    def end_session(self) -> None: ...


class _CameraPreviewManager:
    """Owns the single physical camera's capture loop and fans JPEG frames out
    to any number of subscribers. Paused/resumed around exclusive ffmpeg
    access during recording (mirrors RecordFrame's old _stop_preview/
    _start_preview dance, just headless)."""

    def __init__(
        self,
        get_camera_device: Callable[[], str],
        fps: float = 10.0,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._get_camera_device = get_camera_device
        self._fps = fps
        self._on_error = on_error
        self._lock = threading.Lock()
        self._subscribers: list[FrameCallback] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._paused = False

    def subscribe(self, on_frame: FrameCallback) -> PreviewSubscription:
        with self._lock:
            self._subscribers.append(on_frame)
            if self._thread is None and not self._paused:
                self._start_thread_locked()
        return _ManagerSubscription(self, on_frame)

    def _unsubscribe(self, on_frame: FrameCallback) -> None:
        with self._lock:
            if on_frame in self._subscribers:
                self._subscribers.remove(on_frame)
            if not self._subscribers:
                self._stop_thread_locked()

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            self._stop_thread_locked()

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            if self._subscribers and self._thread is None:
                self._start_thread_locked()

    def restart(self) -> None:
        """Force the capture thread to close and reopen the camera device —
        used by refresh_devices() for a camera that wasn't plugged in yet
        when a subscriber first opened the preview (in which case the
        capture thread would have opened, immediately failed, and exited,
        leaving nothing to retry the open on its own)."""
        with self._lock:
            self._stop_thread_locked()
            if self._subscribers and not self._paused:
                self._start_thread_locked()

    def _start_thread_locked(self) -> None:
        device = self._get_camera_device()
        if not device:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, args=(device,), daemon=True)
        self._thread.start()

    def _stop_thread_locked(self) -> None:
        self._stop_event.set()
        self._thread = None  # the running thread notices stop_event and exits/releases the camera itself

    def _report_error(self, message: str) -> None:
        if self._on_error is not None:
            try:
                self._on_error(message)
            except Exception:
                pass

    def _run(self, device: str) -> None:
        try:
            import cv2
        except ImportError:
            self._report_error("Camera preview requires opencv-python (cv2), which is not installed.")
            return
        index = int(device) if device.isdigit() else device
        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            cap.release()
            self._report_error(
                f"Could not open camera device '{device}'. It may be disconnected, in use by "
                "another app, or lacking camera permission. Reconnect/grant access, then use "
                "↻ Refresh Devices."
            )
            return
        interval = 1.0 / self._fps
        target_width = 320
        # Some failure modes (e.g. macOS denying camera permission to this
        # process) leave isOpened() True but every read() failing forever —
        # treat a long unbroken run of failed reads the same as a failed open.
        consecutive_failures = 0
        max_consecutive_failures = round(self._fps * 3)  # ~3 seconds of nothing but failures
        ever_succeeded = False
        try:
            while not self._stop_event.is_set():
                ok, frame = cap.read()
                if ok:
                    ever_succeeded = True
                    consecutive_failures = 0
                    h, w = frame.shape[:2]
                    new_h = max(1, int(target_width * h / w))
                    frame = cv2.resize(frame, (target_width, new_h))
                    # cv2.imencode expects BGR (what cap.read() already returns) — no color
                    # conversion here, or the encoded JPEG's colors come out swapped.
                    ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if ok2:
                        jpeg = buf.tobytes()
                        with self._lock:
                            subscribers = list(self._subscribers)
                        for cb in subscribers:
                            try:
                                cb(jpeg)
                            except Exception:
                                pass
                else:
                    consecutive_failures += 1
                    if not ever_succeeded and consecutive_failures >= max_consecutive_failures:
                        self._report_error(
                            f"Camera device '{device}' opened but never produced a frame — "
                            "check camera permissions for this app, then use ↻ Refresh Devices."
                        )
                        return
                self._stop_event.wait(interval)
        finally:
            cap.release()


class _ManagerSubscription(PreviewSubscription):
    def __init__(self, manager: _CameraPreviewManager, callback: FrameCallback) -> None:
        self._manager = manager
        self._callback = callback
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._manager._unsubscribe(self._callback)


@dataclass
class _ActiveRecording:
    engine: object
    project: Project
    track: TrackEntry
    inst: object
    phase: str  # "waiting" (loaded, not yet playing/recording) | "recording"
    is_new_inspiration_track: bool = False
    video_recorder: object | None = None
    take_num: int | None = None
    rec_path: Path | None = None
    video_raw: Path | None = None
    mix_flac: Path | None = None
    final_video: Path | None = None
    record_start_wall_time: str | None = None
    # True if this take is running inside an active session (begin_session())
    # — its engine is shared/owned by that session, so per-take teardown must
    # not stop the stream or touch the camera preview; end_session() owns both.
    in_session: bool = False


@dataclass
class _SessionEvent:
    """One entry in a session's session_log.json — see _ActiveSession."""
    timestamp: float  # seconds since session start
    wall_time: str
    event_type: str  # session_start | record_start | restart | take_finished | session_end
    details: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp, "wall_time": self.wall_time,
            "event_type": self.event_type, "details": self.details,
        }


@dataclass
class _ActiveSession:
    """A continuous whole-session audio+video recording spanning every take
    subsequently recorded via start_recording() for the same project/
    instrument, plus a JSON event log — the CLI's `start-session` behavior,
    layered on top of (not instead of) LocalBackend's normal per-take
    recording. See begin_session()/end_session()."""
    engine: object
    project: Project
    inst: object
    session_dir: Path
    session_start: float  # timestamp_now() at begin_session()
    musician: str
    instrument_full_name: str
    studio_name: str
    studio_location: str
    session_flac: Path
    session_video: Path
    events: list = field(default_factory=list)
    video_recorder: object | None = None
    session_video_raw: Path | None = None
    session_mix_flac: Path | None = None
    video_start_wall_time: str | None = None
    video_start_track_name: str = ""


@dataclass
class _ActiveLatencyTest:
    engine: object
    video_recorder: object
    metronome_wav: Path
    take_path: Path
    video_raw: Path
    mix_flac: Path
    final_video: Path
    camera_paired_with_preview: bool  # True if this test's camera is the one open_camera_preview streams


@dataclass
class _ActiveSoundCheck:
    engine: object
    video_recorder: object | None
    take_path: Path
    video_raw: Path | None
    mix_flac: Path | None
    final_video: Path | None
    camera_paired_with_preview: bool  # True if this check's camera is the one open_camera_preview streams


class LocalBackend(Backend):
    """Direct local implementation — talks to this machine's config, disk,
    and audio/video hardware. Historical RecordFrame behavior, unchanged."""

    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self._config_path = config_path
        self._event_callbacks: list[EventCallback] = []
        self._record_lock = threading.Lock()
        self._active_recording: _ActiveRecording | None = None
        self._active_latency_test: _ActiveLatencyTest | None = None
        self._active_sound_check: _ActiveSoundCheck | None = None
        self._active_session: _ActiveSession | None = None
        self._preview = _CameraPreviewManager(self._current_camera_device, on_error=self._on_preview_error)
        # "Sticky" mixer levels: once the operator nudges backing/takes volume,
        # that level carries forward to every track loaded afterward (like a
        # mixing-console fader), instead of each track reverting to its own
        # saved default. Seeded from — and persisted back to — StudioConfig, so
        # a fresh app launch starts from last session's level rather than
        # jumping to whatever full volume an untouched track happened to save.
        config = self.get_config()
        self._backing_volume: int = config.last_backing_volume
        self._takes_volume: int = config.last_takes_volume

    def _save_last_volumes(self) -> None:
        config = self.get_config()
        config.last_backing_volume = self._backing_volume
        config.last_takes_volume = self._takes_volume
        config.save(self._config_path)

    def hostname(self) -> str:
        return socket.gethostname()

    def _current_camera_device(self) -> str:
        return self.get_config().camera_device

    def _on_preview_error(self, message: str) -> None:
        self._emit("preview_error", {"message": message})

    # --- config ---

    def get_config(self) -> StudioConfig:
        return StudioConfig.load(self._config_path)

    def save_config(self, config: StudioConfig) -> None:
        errors = config.validate()
        if errors:
            raise BackendError("\n".join(errors))
        config.save(self._config_path)

    # --- devices ---

    def list_audio_devices(self) -> list[dict]:
        try:
            import sounddevice as sd
            return list(sd.query_devices())
        except Exception:
            return []

    def list_cameras(self) -> list[tuple[str, str]]:
        from .video.devices import list_cameras
        try:
            return list_cameras()
        except Exception:
            return []

    def list_streamdecks(self) -> list[tuple[str, str]]:
        from .streamdeck_controller import list_streamdecks
        try:
            return list_streamdecks()
        except Exception:
            return []

    def refresh_devices(self) -> None:
        """Re-scan hardware for the case the UI was launched (or a remote
        connection made) before the camera/audio interface was plugged in.

        list_cameras() already shells out to ffmpeg fresh each call, so it
        always sees current hardware. Audio is different: PortAudio snapshots
        its device list once, at first use, so a plugged-in-later interface
        stays invisible to sd.query_devices() until PortAudio is
        re-initialized. The camera preview also needs a nudge of its own —
        if the camera wasn't present when the preview was first opened, its
        capture thread will have opened, failed, and exited for good.
        """
        try:
            import sounddevice as sd
            sd._terminate()
            sd._initialize()
        except Exception:
            pass
        self._preview.restart()

    # --- projects / setlists ---

    def list_projects(self) -> list[str]:
        config = self.get_config()
        return [p.name for p in Project.list_projects(Path(config.projects_dir))]

    def _open_project(self, project_name: str) -> Project:
        config = self.get_config()
        projects = Project.list_projects(Path(config.projects_dir))
        path = next((p for p in projects if p.name == project_name), None)
        if path is None:
            raise BackendError(f"Project '{project_name}' not found.")
        return Project.open(path)

    def get_setlist(self, project_name: str) -> dict:
        return self._open_project(project_name).setlist.to_dict()

    def save_setlist(self, project_name: str, setlist_data: dict) -> None:
        project = self._open_project(project_name)
        project.setlist = Setlist.from_dict(setlist_data)
        project.save_setlist()

    def create_project(self, name: str) -> str:
        config = self.get_config()
        projects_dir = Path(config.projects_dir)
        from .utils import sanitize_filename
        safe_name = sanitize_filename(name)
        if not safe_name:
            raise BackendError("Enter a project name.")
        if (projects_dir / safe_name).exists():
            raise BackendError(f"A project named '{safe_name}' already exists.")
        project = Project.create_new(projects_dir, name)
        return project.name

    def add_local_backing_track(
        self, project_name: str, source_path: str, track_name: str | None = None,
    ) -> dict:
        project = self._open_project(project_name)
        src = Path(source_path)
        if not src.exists():
            raise BackendError(f"File not found: {source_path}")
        from .audio.formats import get_duration
        try:
            duration = get_duration(src)
        except Exception:
            duration = 0.0
        entry = project.add_backing_track(src, track_name=track_name, duration_seconds=duration)
        return entry.to_dict()

    def add_youtube_backing_track(
        self, project_name: str, url: str, on_progress: Callable[[float | None, str], None] | None = None,
    ) -> dict:
        project = self._open_project(project_name)
        from .youtube import YouTubeDownloadError, download_youtube_video
        try:
            dest_path, title, duration = download_youtube_video(
                url, project.backing_tracks_dir, on_progress=on_progress,
            )
        except YouTubeDownloadError as e:
            raise BackendError(str(e)) from e
        entry = project.add_backing_track(dest_path, track_name=title, duration_seconds=duration)
        return entry.to_dict()

    # --- inspiration ---

    def query_inspiration_tracks(self, project_name: str) -> list[dict]:
        from .inspiration import InspirationError, query_inspiration_tracks
        project = self._open_project(project_name)
        config = self.get_config()
        try:
            return query_inspiration_tracks(project, config)
        except InspirationError as e:
            raise BackendError(str(e)) from e

    # --- events ---

    def on_event(self, callback: EventCallback) -> None:
        self._event_callbacks.append(callback)

    def off_event(self, callback: EventCallback) -> None:
        if callback in self._event_callbacks:
            self._event_callbacks.remove(callback)

    def _emit(self, event: str, data: dict) -> None:
        for cb in list(self._event_callbacks):
            try:
                cb(event, data)
            except Exception:
                pass

    # --- recording ---

    def is_recording(self) -> bool:
        return self._active_recording is not None

    def get_levels(self) -> tuple[float, float]:
        active = self._active_recording
        if active is None:
            return (0.0, 0.0)
        return (active.engine.peak_level, active.engine.backing_peak_level)

    def adjust_backing_volume(self, delta: int) -> None:
        with self._record_lock:
            active = self._active_recording
            if active is None:
                return
            track = active.track
            self._backing_volume = max(0, self._backing_volume + delta)
            track.volume = self._backing_volume
            active.engine.mixer.set_volume("backing", track.volume / 100.0)
            self._save_last_volumes()
            self._emit("recording_status", {"status": f"Backing volume: {track.volume}%"})

    def adjust_takes_volume(self, delta: int) -> None:
        with self._record_lock:
            active = self._active_recording
            if active is None:
                return
            track = active.track
            self._takes_volume = max(0, self._takes_volume + delta)
            track.takes_volume = self._takes_volume
            for src in active.engine.mixer.sources:
                if src.name.startswith("take:"):
                    inst_name = src.name[5:]
                    take_info = track.preferred_takes.get(inst_name)
                    base_vol = take_info.volume if take_info else 1.0
                    active.engine.mixer.set_volume(src.name, base_vol * (track.takes_volume / 100.0))
            self._save_last_volumes()
            self._emit("recording_status", {"status": f"Takes volume: {track.takes_volume}%"})

    def start_recording(self, req: StartRecordingRequest) -> None:
        with self._record_lock:
            if (
                self._active_recording is not None
                or self._active_latency_test is not None
                or self._active_sound_check is not None
            ):
                raise BackendError("Another recording is already in progress.")

            config = self.get_config()
            project = self._open_project(req.project_name)

            inst = config.get_instrument(req.instrument_name)
            if inst is None:
                raise BackendError(f"Instrument '{req.instrument_name}' not found.")

            session = self._active_session
            if session is not None and (
                session.project.name != project.name or session.inst.name.lower() != inst.name.lower()
            ):
                raise BackendError(
                    f"A session is active for '{session.inst.name}' in '{session.project.name}' — "
                    "end it before recording a different project/instrument."
                )

            is_new_inspiration_track = False
            if req.track_source == "playlist":
                if req.track_index is None or not (0 <= req.track_index < len(project.setlist.tracks)):
                    raise BackendError("Invalid track selection.")
                track = project.setlist.tracks[req.track_index]
            elif req.track_source == "inspiration":
                if not req.inspiration_info:
                    raise BackendError("No inspiration track selected.")
                from .inspiration import find_or_add_inspiration_track
                existing_ids = {t.inspiration_track_id for t in project.setlist.tracks if t.inspiration_track_id}
                track = find_or_add_inspiration_track(project, req.inspiration_info)
                # Only added to the playlist on disk once a take actually completes
                # (see _finish_recording/stop_recording) — a discarded or cancelled
                # take shouldn't leave an orphaned entry behind.
                is_new_inspiration_track = track.inspiration_track_id not in existing_ids
            else:
                raise BackendError("Select a track from the Playlist or Inspiration list first.")

            input_info = config.resolve_input(inst.input_label)
            if input_info is None:
                raise BackendError(f"Input label '{inst.input_label}' not found in config.")

            backing_path = project.backing_tracks_dir / track.backing_track
            if track.inspiration_track_id and not backing_path.exists():
                from .inspiration import InspirationError, download_inspiration_track
                self._emit("recording_status", {"status": f"Downloading '{track.name}'..."})
                try:
                    download_inspiration_track(track, backing_path, config)
                except InspirationError as e:
                    raise BackendError(str(e)) from e

            if session is not None:
                # Session already has its own open stream targeting this exact
                # project/instrument (checked above) — reuse it rather than
                # opening a second one on the same hardware.
                engine = session.engine
            else:
                try:
                    import sounddevice as sd
                except Exception as e:
                    raise BackendError(f"sounddevice unavailable: {e}") from e

                from .audio.devices import resolve_device
                out_dev = resolve_device(sd, config.output_device, "output")
                in_dev = resolve_device(sd, input_info.device, "input")
                if in_dev is None:
                    raise BackendError(f"Input device '{input_info.device}' not found.")
                # config.output_device is optional (empty means "just use the
                # system default"), so only treat a miss as an error when a
                # specific device *was* configured — otherwise a device that was
                # explicitly chosen but has since disconnected (e.g. a USB audio
                # interface dropping out) would silently fall back to whatever
                # the system default output happens to be instead of raising,
                # playing audio to the wrong place with no indication why.
                if config.output_device and out_dev is None:
                    raise BackendError(f"Output device '{config.output_device}' not found.")

                in_info = sd.query_devices(in_dev, "input")
                out_info = sd.query_devices(out_dev, "output")
                max_in = in_info["max_input_channels"]
                if input_info.channel > max_in:
                    raise BackendError(
                        f"Instrument '{inst.name}' needs input channel {input_info.channel} "
                        f"but device only has {max_in} channels."
                    )
                output_channels = min(config.output_channels, out_info["max_output_channels"])

                from .audio.engine import AudioEngine
                engine = AudioEngine(
                    sample_rate=config.sample_rate,
                    buffer_size=config.buffer_size,
                    input_device=in_dev,
                    output_device=out_dev,
                    input_channels=max(input_info.channel, 1),
                    output_channels=max(1, output_channels),
                    monitor_channel=input_info.channel - 1,
                )

            # The remembered mixer level (this run's, or carried over from the
            # last time anything was recorded) always wins over the track's own
            # saved default — otherwise an untouched track loads at whatever
            # volume it last happened to be saved at, which can be jarringly loud.
            track.volume = self._backing_volume
            track.takes_volume = self._takes_volume

            if backing_path.exists():
                engine.mixer.add_source("backing", backing_path, volume=track.volume / 100.0)

            trim = int(config.latency_compensation_ms / 1000.0 * config.sample_rate)
            for other_inst, take_info in track.preferred_takes.items():
                if other_inst.lower() == inst.name.lower():
                    continue
                take_path = project.completed_takes_dir / take_info.filename
                if take_path.exists():
                    effective_vol = take_info.volume * (track.takes_volume / 100.0)
                    engine.mixer.add_source(f"take:{other_inst}", take_path, volume=effective_vol, trim_frames=trim)

            # Audio stream running (so input monitoring works right away), backing
            # track loaded but silent — actual recording starts on unpause_recording().
            # (Already running if a session owns this engine.)
            if session is None:
                engine.start()
            engine.mixer.reset()
            engine.mixer.set_playing(False)

            self._active_recording = _ActiveRecording(
                engine=engine, project=project, track=track, inst=inst, phase="waiting",
                is_new_inspiration_track=is_new_inspiration_track, in_session=session is not None,
            )
            self._emit("recording_status", {
                "phase": "waiting",
                "status": f"Loaded '{track.name}' — press Unpause to begin",
                "track_name": track.name,
            })

    def unpause_recording(self) -> None:
        with self._record_lock:
            active = self._active_recording
            if active is None or active.phase != "waiting":
                raise BackendError("Not ready to unpause.")

            # Release the preview capture so ffmpeg can open the camera
            # exclusively — unless a session already owns the camera for its
            # own continuous video (paused for the whole session in that
            # case, not per-take).
            if not active.in_session:
                self._preview.pause()
                self._emit("preview_paused", {})

            take_num = self._begin_take(active, self.get_config())
            self._emit("recording_status", {
                "phase": "recording",
                "status": f"Recording '{active.track.name}' — take {take_num}",
                "track_name": active.track.name,
                "take_number": take_num,
            })

    def restart_take(self) -> None:
        """Discard the take in progress and start it over from 0:00 — same
        track/instrument/session, a fresh take number. Only valid while
        actually recording (unlike stop_recording(), the active session
        itself survives this — it's a redo, not a cancel)."""
        with self._record_lock:
            active = self._active_recording
            if active is None or active.phase != "recording":
                raise BackendError("Not currently recording.")

            active.engine.set_on_song_end(None)
            active.engine.stop_recording()
            active.engine.mixer.set_playing(False)
            active.rec_path.unlink(missing_ok=True)
            if active.video_recorder:
                active.video_recorder.stop()
                if active.video_raw:
                    active.video_raw.unlink(missing_ok=True)
                if active.mix_flac:
                    active.mix_flac.unlink(missing_ok=True)

            take_num = self._begin_take(active, self.get_config(), event_type="restart")
            self._emit("recording_status", {
                "phase": "recording",
                "status": f"Restarted '{active.track.name}' from the beginning — take {take_num}",
                "track_name": active.track.name,
                "take_number": take_num,
            })

    def _begin_take(self, active: "_ActiveRecording", config: StudioConfig, event_type: str = "record_start") -> int:
        """Allocate a fresh take file and (re)start the per-take audio/video
        recorders against the given active session, from mixer position 0.
        Shared by unpause_recording() (first time) and restart_take()
        (discard and redo) — called with self._record_lock held."""
        take_num = next_take_number(active.project.completed_takes_dir, active.track.name, active.inst.name)
        fname = take_filename(active.track.name, active.inst.name, take_num, "flac")
        rec_path = active.project.completed_takes_dir / fname

        record_start_wall_time = wall_timestamp()
        active.engine.mixer.reset()
        active.engine.mixer.set_playing(True)
        active.engine.start_recording(rec_path)
        active.engine.set_on_song_end(self._on_song_naturally_ended)

        video_recorder = None
        video_raw = mix_flac = final_video = None
        # A session already owns the camera continuously for its own
        # whole-session video — a second, per-take VideoRecorder on the same
        # device would conflict with it, so per-take video is skipped for
        # the duration of a session (matches the legacy start-session CLI,
        # which only ever produced one video for the whole session).
        if config.camera_device and not active.in_session:
            from .video.capture import VideoRecorder, ffmpeg_available
            if ffmpeg_available():
                video_raw = rec_path.with_name(rec_path.stem + "_video_raw.mp4")
                mix_flac = rec_path.with_name(rec_path.stem + "_mix.flac")
                final_video = rec_path.with_suffix(".mp4")
                video_recorder = VideoRecorder(config.camera_device, video_raw)
                if video_recorder.start():
                    active.engine.start_mix_recording(mix_flac)
                else:
                    video_recorder = None

        active.phase = "recording"
        active.take_num = take_num
        active.rec_path = rec_path
        active.video_recorder = video_recorder
        active.video_raw = video_raw
        active.mix_flac = mix_flac
        active.final_video = final_video
        active.record_start_wall_time = record_start_wall_time
        if self._active_session is not None and not self._active_session.video_start_track_name:
            self._active_session.video_start_track_name = active.track.name
        self._log_session_event(event_type, f"track={active.track.name} take={take_num}")
        return take_num

    def get_active_recording_target(self) -> tuple[str, str, int] | None:
        """Returns (project_name, instrument_name, track_index) for the take
        currently loaded/recording, or None if idle. Lets a driver with no
        track-selection UI of its own (the headless server's StreamDeck)
        figure out "what's next" regardless of whether the current take was
        started by it or by a connected remote client."""
        with self._record_lock:
            active = self._active_recording
            if active is None:
                return None
            if active.track not in active.project.setlist.tracks:
                return None  # defensive; shouldn't happen
            index = active.project.setlist.tracks.index(active.track)
            return active.project.name, active.inst.name, index

    def _on_song_naturally_ended(self) -> None:
        """Called (off the audio thread) when the backing track plays to its
        end while recording — the only case where the take is kept."""
        with self._record_lock:
            active = self._active_recording
            if active is None or active.phase != "recording":
                return
            self._active_recording = None
            self._finish_recording(active, keep=True)

    def stop_recording(self) -> None:
        """User-initiated stop. Before the backing track finishes on its own
        this discards the take as incomplete; during the pre-recording
        'waiting' phase it just cancels the load."""
        with self._record_lock:
            active = self._active_recording
            if active is None:
                return
            self._active_recording = None

            if active.phase == "waiting":
                if not active.in_session:
                    active.engine.stop()
                self._discard_new_inspiration_track(active)
                active.project.save_setlist()  # keep any volume tweaks made before unpausing
                self._emit("recording_status", {"phase": "idle", "status": "Recording cancelled."})
                return

            self._finish_recording(active, keep=False)

    def _discard_new_inspiration_track(self, active: "_ActiveRecording") -> None:
        """Undo find_or_add_inspiration_track's addition when a take never
        completes — an inspiration track should only land in the playlist
        once it actually has a finished take."""
        if not active.is_new_inspiration_track:
            return
        tracks = active.project.setlist.tracks
        for i, t in enumerate(tracks):
            if t is active.track:
                tracks.pop(i)
                break

    def _finish_recording(self, active: "_ActiveRecording", keep: bool) -> None:
        """Tear down an in-progress (phase='recording') take. Called with
        self._record_lock held and self._active_recording already cleared.

        Volume tweaks made during the take are saved either way — the take
        itself only survives if `keep` is True (i.e. the song played to its
        natural end rather than being stopped early)."""
        engine = active.engine
        engine.set_on_song_end(None)
        engine.stop_recording()
        engine.mixer.set_playing(False)

        if keep:
            take_info = TakeInfo(
                instrument=active.inst.name, take_number=active.take_num, filename=active.rec_path.name,
            )
            active.track.set_preferred_take(active.inst.name, take_info)
            status = f"Saved take {active.take_num} for '{active.track.name}'"
        else:
            active.rec_path.unlink(missing_ok=True)
            self._discard_new_inspiration_track(active)
            status = f"Discarded incomplete take for '{active.track.name}'"
        active.project.save_setlist()
        self._log_session_event("take_finished", f"track={active.track.name} take={active.take_num} kept={keep}")

        if not active.in_session:
            engine.stop()

        if active.video_recorder:
            active.video_recorder.stop()
            if keep:
                from .video.capture import format_watermark_text, mux_video_audio
                config = self.get_config()
                musician = active.inst.musician or config.studio_musician
                watermark_text = format_watermark_text(
                    musician, active.inst.name, active.record_start_wall_time, active.track.name,
                )
                if mux_video_audio(
                    active.video_raw, active.mix_flac, active.rec_path, active.final_video,
                    watermark_text=watermark_text, video_offset_ms=config.video_latency_compensation_ms,
                ):
                    active.video_raw.unlink(missing_ok=True)
                    active.mix_flac.unlink(missing_ok=True)
                    status += " + video"
                else:
                    status += "; video mux failed"
            else:
                active.video_raw.unlink(missing_ok=True)
                active.mix_flac.unlink(missing_ok=True)

        if not active.in_session:
            self._preview.resume()
            self._emit("preview_resumed", {})
        self._emit("recording_status", {"phase": "idle", "status": status})

    # --- camera preview ---

    def open_camera_preview(self, on_frame: FrameCallback) -> PreviewSubscription:
        return self._preview.subscribe(on_frame)

    # --- camera latency test ---

    def start_latency_test(self, instrument_name: str, camera_device: str, play_metronome: bool = True) -> None:
        with self._record_lock:
            if (
                self._active_recording is not None
                or self._active_latency_test is not None
                or self._active_sound_check is not None
                or self._active_session is not None
            ):
                raise BackendError("Another recording is already in progress.")
            if not camera_device:
                raise BackendError("Select a camera first.")

            config = self.get_config()
            inst = config.get_instrument(instrument_name)
            if inst is None:
                raise BackendError(f"Instrument '{instrument_name}' not found.")
            input_info = config.resolve_input(inst.input_label)
            if input_info is None:
                raise BackendError(f"Input label '{inst.input_label}' not found in config.")

            from .video.capture import ffmpeg_available
            if not ffmpeg_available():
                raise BackendError("ffmpeg is required for the camera latency test.")

            try:
                import sounddevice as sd
            except Exception as e:
                raise BackendError(f"sounddevice unavailable: {e}") from e

            from .audio.devices import resolve_device
            out_dev = resolve_device(sd, config.output_device, "output")
            in_dev = resolve_device(sd, input_info.device, "input")
            if in_dev is None:
                raise BackendError(f"Input device '{input_info.device}' not found.")
            # config.output_device is optional (empty means "just use the
            # system default"), so only treat a miss as an error when a
            # specific device *was* configured — otherwise a device that was
            # explicitly chosen but has since disconnected (e.g. a USB audio
            # interface dropping out) would silently fall back to whatever
            # the system default output happens to be instead of raising,
            # playing audio to the wrong place with no indication why.
            if config.output_device and out_dev is None:
                raise BackendError(f"Output device '{config.output_device}' not found.")

            in_info = sd.query_devices(in_dev, "input")
            out_info = sd.query_devices(out_dev, "output")
            if input_info.channel > in_info["max_input_channels"]:
                raise BackendError(
                    f"Instrument '{inst.name}' needs input channel {input_info.channel} "
                    f"but device only has {in_info['max_input_channels']} channels."
                )
            output_channels = min(config.output_channels, out_info["max_output_channels"])

            from .audio.engine import AudioEngine
            from .audio.metronome import generate_metronome_wav
            from .video.capture import VideoRecorder

            work_dir = ensure_dir(Path(tempfile.gettempdir()) / "takeloom_latency_test")
            metronome_wav = work_dir / "metronome.wav"
            take_path = work_dir / "instrument.flac"
            video_raw = work_dir / "video_raw.mp4"
            mix_flac = work_dir / "mix.flac"
            final_video = work_dir / "result.mp4"

            if play_metronome:
                generate_metronome_wav(metronome_wav, config.sample_rate)

            engine = AudioEngine(
                sample_rate=config.sample_rate,
                buffer_size=config.buffer_size,
                input_device=in_dev,
                output_device=out_dev,
                input_channels=max(input_info.channel, 1),
                output_channels=max(1, output_channels),
                monitor_channel=input_info.channel - 1,
            )
            if play_metronome:
                engine.mixer.add_source("metronome", metronome_wav)
            engine.start()
            engine.mixer.reset()
            engine.mixer.set_playing(True)
            engine.start_recording(take_path)
            engine.start_mix_recording(mix_flac)

            # Same pause/resume dance as a real take (unpause_recording): if
            # the chosen test camera is the one open_camera_preview streams,
            # its cv2 capture has to let go before ffmpeg can open it exclusively.
            camera_paired_with_preview = camera_device == config.camera_device
            if camera_paired_with_preview:
                self._preview.pause()
                self._emit("preview_paused", {})

            video_recorder = VideoRecorder(camera_device, video_raw)
            if not video_recorder.start():
                engine.stop()
                if camera_paired_with_preview:
                    self._preview.resume()
                    self._emit("preview_resumed", {})
                raise BackendError("Could not start camera capture.")

            self._active_latency_test = _ActiveLatencyTest(
                engine=engine, video_recorder=video_recorder, metronome_wav=metronome_wav,
                take_path=take_path, video_raw=video_raw, mix_flac=mix_flac, final_video=final_video,
                camera_paired_with_preview=camera_paired_with_preview,
            )
            self._emit("latency_test_status", {
                "phase": "recording",
                "status": "Recording — clap or hit your instrument along with the click, then Stop.",
            })

    def stop_latency_test(self) -> None:
        with self._record_lock:
            active = self._active_latency_test
            if active is None:
                raise BackendError("No latency test in progress.")
            self._active_latency_test = None

            active.engine.stop_recording()
            active.engine.mixer.set_playing(False)
            active.engine.stop()
            active.video_recorder.stop()

            if active.camera_paired_with_preview:
                self._preview.resume()
                self._emit("preview_resumed", {})

            from .video.capture import mux_video_audio, open_in_default_player

            # Muxed with the currently saved video offset applied, so this
            # clip previews exactly what a real take would look like — the
            # operator dials the offset in by re-running the test after each
            # Save, not by eyeballing a fixed raw gap and doing the ms math
            # themselves.
            video_offset_ms = self.get_config().video_latency_compensation_ms
            ok = mux_video_audio(
                active.video_raw, active.mix_flac, active.take_path, active.final_video,
                video_offset_ms=video_offset_ms,
            )

            active.video_raw.unlink(missing_ok=True)
            active.mix_flac.unlink(missing_ok=True)
            active.take_path.unlink(missing_ok=True)
            active.metronome_wav.unlink(missing_ok=True)

            if not ok:
                self._emit("latency_test_status", {"phase": "idle", "status": "Video mux failed."})
                raise BackendError("Could not combine video and audio.")

            open_in_default_player(active.final_video)
            self._emit("latency_test_status", {
                "phase": "idle",
                "status": "Test recording opened for review — adjust the offsets below and Save.",
                "video_path": str(active.final_video),
            })

    # --- sound check (local-only; RemoteBackend refuses) ---

    def start_sound_check(self, req: StartRecordingRequest) -> None:
        with self._record_lock:
            if (
                self._active_recording is not None
                or self._active_latency_test is not None
                or self._active_sound_check is not None
                or self._active_session is not None
            ):
                raise BackendError("Another recording is already in progress.")

            config = self.get_config()
            project = self._open_project(req.project_name)

            inst = config.get_instrument(req.instrument_name)
            if inst is None:
                raise BackendError(f"Instrument '{req.instrument_name}' not found.")

            if req.track_source == "playlist":
                if req.track_index is None or not (0 <= req.track_index < len(project.setlist.tracks)):
                    raise BackendError("Invalid track selection.")
                track = project.setlist.tracks[req.track_index]
            elif req.track_source == "inspiration":
                if not req.inspiration_info:
                    raise BackendError("No inspiration track selected.")
                from .inspiration import find_or_add_inspiration_track
                # Only mutates this call's in-memory Project — sound check never
                # calls save_setlist(), so this never actually lands on disk.
                track = find_or_add_inspiration_track(project, req.inspiration_info)
            else:
                raise BackendError("Select a track from the Playlist or Inspiration list first.")

            input_info = config.resolve_input(inst.input_label)
            if input_info is None:
                raise BackendError(f"Input label '{inst.input_label}' not found in config.")

            backing_path = project.backing_tracks_dir / track.backing_track
            if track.inspiration_track_id and not backing_path.exists():
                from .inspiration import InspirationError, download_inspiration_track
                self._emit("sound_check_status", {"status": f"Downloading '{track.name}'..."})
                try:
                    download_inspiration_track(track, backing_path, config)
                except InspirationError as e:
                    raise BackendError(str(e)) from e

            try:
                import sounddevice as sd
            except Exception as e:
                raise BackendError(f"sounddevice unavailable: {e}") from e

            from .audio.devices import resolve_device
            out_dev = resolve_device(sd, config.output_device, "output")
            in_dev = resolve_device(sd, input_info.device, "input")
            if in_dev is None:
                raise BackendError(f"Input device '{input_info.device}' not found.")
            if config.output_device and out_dev is None:
                raise BackendError(f"Output device '{config.output_device}' not found.")

            in_info = sd.query_devices(in_dev, "input")
            out_info = sd.query_devices(out_dev, "output")
            max_in = in_info["max_input_channels"]
            if input_info.channel > max_in:
                raise BackendError(
                    f"Instrument '{inst.name}' needs input channel {input_info.channel} "
                    f"but device only has {max_in} channels."
                )
            output_channels = min(config.output_channels, out_info["max_output_channels"])

            from .audio.engine import AudioEngine
            engine = AudioEngine(
                sample_rate=config.sample_rate,
                buffer_size=config.buffer_size,
                input_device=in_dev,
                output_device=out_dev,
                input_channels=max(input_info.channel, 1),
                output_channels=max(1, output_channels),
                monitor_channel=input_info.channel - 1,
            )

            if backing_path.exists():
                engine.mixer.add_source("backing", backing_path, volume=self._backing_volume / 100.0)

            trim = int(config.latency_compensation_ms / 1000.0 * config.sample_rate)
            for other_inst, take_info in track.preferred_takes.items():
                if other_inst.lower() == inst.name.lower():
                    continue
                take_path = project.completed_takes_dir / take_info.filename
                if take_path.exists():
                    effective_vol = take_info.volume * (self._takes_volume / 100.0)
                    engine.mixer.add_source(f"take:{other_inst}", take_path, volume=effective_vol, trim_frames=trim)

            work_dir = ensure_dir(Path(tempfile.gettempdir()) / "takeloom_sound_check")
            take_path = work_dir / "instrument.flac"
            video_raw = work_dir / "video_raw.mp4"
            mix_flac = work_dir / "mix.flac"
            final_video = work_dir / "result.mp4"

            engine.start()
            engine.mixer.reset()
            engine.mixer.set_playing(True)
            engine.start_recording(take_path)
            engine.set_on_song_end(self._on_sound_check_naturally_ended)

            video_recorder = None
            camera_paired_with_preview = False
            if config.camera_device:
                from .video.capture import VideoRecorder, ffmpeg_available
                if ffmpeg_available():
                    # Sound check always uses the configured camera (unlike the
                    # latency test, which lets the operator pick a different one
                    # to test), so it's always the same device open_camera_preview
                    # streams — the preview must be paused to free it for ffmpeg.
                    camera_paired_with_preview = True
                    self._preview.pause()
                    self._emit("preview_paused", {})
                    video_recorder = VideoRecorder(config.camera_device, video_raw)
                    if video_recorder.start():
                        engine.start_mix_recording(mix_flac)
                    else:
                        video_recorder = None
                        self._preview.resume()
                        self._emit("preview_resumed", {})
                        camera_paired_with_preview = False

            self._active_sound_check = _ActiveSoundCheck(
                engine=engine, video_recorder=video_recorder, take_path=take_path,
                video_raw=video_raw if video_recorder else None,
                mix_flac=mix_flac if video_recorder else None,
                final_video=final_video if video_recorder else None,
                camera_paired_with_preview=camera_paired_with_preview,
            )
            self._emit("sound_check_status", {
                "phase": "recording",
                "status": f"Sound check — playing '{track.name}'...",
                "track_name": track.name,
            })

    def stop_sound_check(self) -> None:
        with self._record_lock:
            active = self._active_sound_check
            if active is None:
                raise BackendError("No sound check in progress.")
            self._active_sound_check = None
            self._finish_sound_check(active)

    def _on_sound_check_naturally_ended(self) -> None:
        with self._record_lock:
            active = self._active_sound_check
            if active is None:
                return
            self._active_sound_check = None
            self._finish_sound_check(active)

    def _finish_sound_check(self, active: "_ActiveSoundCheck") -> None:
        """Tear down an in-progress sound check. Called with self._record_lock
        held and self._active_sound_check already cleared."""
        active.engine.set_on_song_end(None)
        active.engine.stop_recording()
        active.engine.mixer.set_playing(False)
        active.engine.stop()
        if active.video_recorder:
            active.video_recorder.stop()

        if active.camera_paired_with_preview:
            self._preview.resume()
            self._emit("preview_resumed", {})

        if active.video_recorder and active.video_raw and active.mix_flac and active.final_video:
            from .video.capture import mux_video_audio
            video_offset_ms = self.get_config().video_latency_compensation_ms
            ok = mux_video_audio(
                active.video_raw, active.mix_flac, active.take_path, active.final_video,
                video_offset_ms=video_offset_ms,
            )
            active.video_raw.unlink(missing_ok=True)
            active.mix_flac.unlink(missing_ok=True)
            active.take_path.unlink(missing_ok=True)
            if not ok:
                self._emit("sound_check_status", {"phase": "idle", "status": "Video mux failed."})
                raise BackendError("Could not combine video and audio.")
            self._emit("sound_check_status", {
                "phase": "idle",
                "status": "Sound check recorded — review it, then close the window to discard it.",
                "result_path": str(active.final_video),
                "has_video": True,
            })
        else:
            self._emit("sound_check_status", {
                "phase": "idle",
                "status": "Sound check recorded — review it, then close the window to discard it.",
                "result_path": str(active.take_path),
                "has_video": False,
            })

    # --- continuous multi-track session recording (local-only; RemoteBackend refuses) ---

    def _log_session_event(self, event_type: str, details: str = "") -> None:
        session = self._active_session
        if session is None:
            return
        session.events.append(_SessionEvent(
            timestamp=timestamp_now() - session.session_start,
            wall_time=wall_timestamp(),
            event_type=event_type,
            details=details,
        ))

    def begin_session(self, project_name: str, instrument_name: str) -> None:
        with self._record_lock:
            if (
                self._active_recording is not None
                or self._active_latency_test is not None
                or self._active_sound_check is not None
                or self._active_session is not None
            ):
                raise BackendError("Another recording is already in progress.")

            config = self.get_config()
            project = self._open_project(project_name)
            inst = config.get_instrument(instrument_name)
            if inst is None:
                raise BackendError(f"Instrument '{instrument_name}' not found.")
            input_info = config.resolve_input(inst.input_label)
            if input_info is None:
                raise BackendError(f"Input label '{inst.input_label}' not found in config.")

            try:
                import sounddevice as sd
            except Exception as e:
                raise BackendError(f"sounddevice unavailable: {e}") from e

            from .audio.devices import resolve_device
            out_dev = resolve_device(sd, config.output_device, "output")
            in_dev = resolve_device(sd, input_info.device, "input")
            if in_dev is None:
                raise BackendError(f"Input device '{input_info.device}' not found.")
            if config.output_device and out_dev is None:
                raise BackendError(f"Output device '{config.output_device}' not found.")

            in_info = sd.query_devices(in_dev, "input")
            out_info = sd.query_devices(out_dev, "output")
            max_in = in_info["max_input_channels"]
            if input_info.channel > max_in:
                raise BackendError(
                    f"Instrument '{inst.name}' needs input channel {input_info.channel} "
                    f"but device only has {max_in} channels."
                )
            output_channels = min(config.output_channels, out_info["max_output_channels"])

            from .audio.engine import AudioEngine
            engine = AudioEngine(
                sample_rate=config.sample_rate,
                buffer_size=config.buffer_size,
                input_device=in_dev,
                output_device=out_dev,
                input_channels=max(input_info.channel, 1),
                output_channels=max(1, output_channels),
                monitor_channel=input_info.channel - 1,
            )
            engine.start()

            session_name = wall_timestamp().replace(":", "-").replace(" ", "_")
            session_dir = ensure_dir(project.sessions_dir / f"{session_name}_{inst.name}")
            session_flac = session_dir / "session.flac"
            engine.start_session_recording(session_flac)

            video_recorder = None
            session_video_raw = session_mix_flac = None
            video_start_wall_time = None
            if config.camera_device:
                from .video.capture import VideoRecorder, ffmpeg_available
                if ffmpeg_available():
                    self._preview.pause()
                    self._emit("preview_paused", {})
                    session_video_raw = session_dir / "session_video_raw.mp4"
                    session_mix_flac = session_dir / "session_mix.flac"
                    video_recorder = VideoRecorder(config.camera_device, session_video_raw)
                    if video_recorder.start():
                        engine.start_mix_recording(session_mix_flac)
                        video_start_wall_time = wall_timestamp()
                    else:
                        video_recorder = None
                        self._preview.resume()
                        self._emit("preview_resumed", {})

            self._active_session = _ActiveSession(
                engine=engine, project=project, inst=inst, session_dir=session_dir,
                session_start=timestamp_now(),
                musician=inst.musician or config.studio_musician,
                instrument_full_name=inst.full_name,
                studio_name=config.studio_name, studio_location=config.studio_location,
                session_flac=session_flac, session_video=session_dir / "session_video.mp4",
                video_recorder=video_recorder, session_video_raw=session_video_raw,
                session_mix_flac=session_mix_flac, video_start_wall_time=video_start_wall_time,
            )
            self._log_session_event("session_start", f"instrument={inst.name}")
            self._emit("recording_status", {
                "status": f"Session started for '{inst.name}' in '{project.name}'.",
            })

    def end_session(self) -> None:
        with self._record_lock:
            session = self._active_session
            if session is None:
                raise BackendError("No session in progress.")
            if self._active_recording is not None:
                raise BackendError("Stop or finish the current take before ending the session.")

            self._log_session_event("session_end")
            self._active_session = None

            session.engine.stop()  # closes the stream and every recorder on it (per-take/session/mix)

            if session.video_recorder:
                session.video_recorder.stop()

            if session.video_recorder and session.session_video_raw and session.session_mix_flac:
                from .video.capture import format_watermark_text, mux_video_audio
                watermark_text = format_watermark_text(
                    session.musician, session.inst.name,
                    session.video_start_wall_time or "", session.video_start_track_name,
                )
                ok = mux_video_audio(
                    session.session_video_raw, session.session_mix_flac, session.session_flac,
                    session.session_video, watermark_text=watermark_text,
                    video_offset_ms=self.get_config().video_latency_compensation_ms,
                )
                if ok:
                    session.session_video_raw.unlink(missing_ok=True)
                    session.session_mix_flac.unlink(missing_ok=True)
                    status = f"Session ended — video saved to {session.session_video}"
                else:
                    status = f"Session ended; video mux failed, raw files kept in {session.session_dir}"
                self._preview.resume()
                self._emit("preview_resumed", {})
            else:
                status = "Session ended."

            log_path = self._save_session_log(session)
            if log_path:
                status += f" Log saved to {log_path}."

            self._emit("recording_status", {"phase": "idle", "status": status})

    def _save_session_log(self, session: "_ActiveSession") -> Path | None:
        log_path = session.session_dir / "session_log.json"
        data = {
            "instrument": session.inst.name,
            "instrument_full_name": session.instrument_full_name,
            "musician": session.musician,
            "project": session.project.name,
            "studio_name": session.studio_name,
            "studio_location": session.studio_location,
            "events": [e.to_dict() for e in session.events],
        }
        log_path.write_text(json.dumps(data, indent=2))
        return log_path
