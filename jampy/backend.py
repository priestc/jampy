"""Backend abstraction: everything a UI tab needs, independent of whether the
data/hardware lives on this machine or a remote jampy instance.

`LocalBackend` talks directly to local config, disk, and audio/video
hardware — this is the historical behavior of the UI tabs, just extracted
behind an interface. `RemoteBackend` (in `jampy/remote/backend.py`) adapts
the same interface over the network to a `RemoteServer` running elsewhere,
which itself wraps its own `LocalBackend`.

Nothing in this module touches tkinter. Callers on the UI side are
responsible for running blocking calls on a background thread and
marshalling results back to the Tk thread (e.g. via `widget.after(0, ...)`).
Event callbacks registered via `on_event`/camera-preview frame callbacks may
be invoked from a non-UI thread for the same reason.
"""

from __future__ import annotations

import socket
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import DEFAULT_CONFIG_PATH, StudioConfig
from .project import Project, Setlist, TakeInfo, TrackEntry
from .utils import next_take_number, take_filename, wall_timestamp


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

    # --- projects / setlists ---

    @abstractmethod
    def list_projects(self) -> list[str]: ...

    @abstractmethod
    def get_setlist(self, project_name: str) -> dict: ...

    @abstractmethod
    def save_setlist(self, project_name: str, setlist_data: dict) -> None: ...

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


class _CameraPreviewManager:
    """Owns the single physical camera's capture loop and fans JPEG frames out
    to any number of subscribers. Paused/resumed around exclusive ffmpeg
    access during recording (mirrors RecordFrame's old _stop_preview/
    _start_preview dance, just headless)."""

    def __init__(self, get_camera_device: Callable[[], str], fps: float = 10.0) -> None:
        self._get_camera_device = get_camera_device
        self._fps = fps
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

    def _run(self, device: str) -> None:
        try:
            import cv2
        except ImportError:
            return
        index = int(device) if device.isdigit() else device
        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            cap.release()
            return
        interval = 1.0 / self._fps
        target_width = 320
        try:
            while not self._stop_event.is_set():
                ok, frame = cap.read()
                if ok:
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


class LocalBackend(Backend):
    """Direct local implementation — talks to this machine's config, disk,
    and audio/video hardware. Historical RecordFrame behavior, unchanged."""

    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self._config_path = config_path
        self._event_callbacks: list[EventCallback] = []
        self._record_lock = threading.Lock()
        self._active_recording: _ActiveRecording | None = None
        self._preview = _CameraPreviewManager(self._current_camera_device)
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
            if self._active_recording is not None:
                raise BackendError("Another recording is already in progress.")

            config = self.get_config()
            project = self._open_project(req.project_name)

            inst = config.get_instrument(req.instrument_name)
            if inst is None:
                raise BackendError(f"Instrument '{req.instrument_name}' not found.")

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

            try:
                import sounddevice as sd
            except Exception as e:
                raise BackendError(f"sounddevice unavailable: {e}") from e

            from .audio.devices import resolve_device
            out_dev = resolve_device(sd, config.output_device, "output")
            in_dev = resolve_device(sd, input_info.device, "input")
            if in_dev is None:
                raise BackendError(f"Input device '{input_info.device}' not found.")

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
            engine.start()
            engine.mixer.reset()
            engine.mixer.set_playing(False)

            self._active_recording = _ActiveRecording(
                engine=engine, project=project, track=track, inst=inst, phase="waiting",
                is_new_inspiration_track=is_new_inspiration_track,
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

            config = self.get_config()
            take_num = next_take_number(active.project.completed_takes_dir, active.track.name, active.inst.name)
            fname = take_filename(active.track.name, active.inst.name, take_num, "flac")
            rec_path = active.project.completed_takes_dir / fname

            # Release the preview capture so ffmpeg can open the camera exclusively.
            self._preview.pause()
            self._emit("preview_paused", {})

            record_start_wall_time = wall_timestamp()
            active.engine.mixer.reset()
            active.engine.mixer.set_playing(True)
            active.engine.start_recording(rec_path)
            active.engine.set_on_song_end(self._on_song_naturally_ended)

            video_recorder = None
            video_raw = mix_flac = final_video = None
            if config.camera_device:
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

            self._emit("recording_status", {
                "phase": "recording",
                "status": f"Recording '{active.track.name}' — take {take_num}",
                "track_name": active.track.name,
                "take_number": take_num,
            })

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
                    watermark_text=watermark_text,
                ):
                    active.video_raw.unlink(missing_ok=True)
                    active.mix_flac.unlink(missing_ok=True)
                    status += " + video"
                else:
                    status += "; video mux failed"
            else:
                active.video_raw.unlink(missing_ok=True)
                active.mix_flac.unlink(missing_ok=True)

        self._preview.resume()
        self._emit("preview_resumed", {})
        self._emit("recording_status", {"phase": "idle", "status": status})

    # --- camera preview ---

    def open_camera_preview(self, on_frame: FrameCallback) -> PreviewSubscription:
        return self._preview.subscribe(on_frame)
