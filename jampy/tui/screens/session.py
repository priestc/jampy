"""Live recording session screen."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, Label
from textual.containers import Horizontal, Vertical
from textual.timer import Timer

from ...config import StudioConfig
from ...session import Session, SessionState
from ...audio.engine import AudioEngine
from ...project import Project
from ..widgets.transport import TransportWidget
from ..widgets.waveform import VUMeterWidget
from ..widgets.session_log import SessionLogWidget
from ..widgets.track_list import TrackListWidget


class SessionScreen(Screen):
    """Live recording session with real-time audio."""

    BINDINGS = [
        ("r", "record", "Record"),
        ("b", "back_to_start", "Restart"),
        ("e", "song_end", "End Song"),
        ("n", "next_track", "Next"),
        ("q", "end_session", "End Session"),
        ("escape", "end_session", "End Session"),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session: Session | None = None
        self._engine: AudioEngine | None = None
        self._update_timer: Timer | None = None
        self._raw_recording_path: Path | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("", id="session-header")
        with Vertical(id="session-body"):
            yield TransportWidget(id="transport-panel")
            with Horizontal():
                yield VUMeterWidget(id="vu-panel")
                yield SessionLogWidget(id="log-panel")
            yield TrackListWidget(id="session-tracklist")
        yield Label("", id="session-footer")
        yield Footer()

    def on_mount(self) -> None:
        project: Project = self.app.project
        instrument: str = self.app.session_instrument
        config = StudioConfig.load()

        # Initialize session
        self._session = Session(
            project=project,
            instrument=instrument,
            on_state_change=self._on_state_change,
        )

        # Initialize audio engine
        self._engine = AudioEngine(
            sample_rate=config.sample_rate,
            buffer_size=config.buffer_size,
            input_device=config.input_device,
            output_device=config.output_device,
            input_channels=config.input_channels,
            output_channels=config.output_channels,
        )
        self._engine.set_on_song_end(self._on_song_finished)

        # Start session
        self._session.start()

        # Start audio stream and continuous recording
        self._engine.start()
        self._raw_recording_path = self._session.session_dir / "raw_recording.flac"
        self._engine.start_recording(self._raw_recording_path)

        # Load backing tracks for first song
        self._load_current_track()

        # Update header
        header = self.query_one("#session-header", Label)
        header.update(f"Session: {project.name} | Instrument: {instrument}")

        # Update track list
        tl = self.query_one("#session-tracklist", TrackListWidget)
        tl.update_setlist(project.setlist, self._session.current_track_index, instrument)

        # Log
        self._log("Session started")

        # Start UI update timer
        self._update_timer = self.set_interval(1 / 15, self._update_ui)

    def _load_current_track(self) -> None:
        """Load the backing track and preferred takes for the current song."""
        if not self._session or not self._engine:
            return
        track = self._session.current_track
        if not track:
            return

        project = self._session.project
        self._engine.mixer.clear()

        # Load backing track
        backing_path = project.backing_tracks_dir / track.backing_track
        if backing_path.exists():
            self._engine.mixer.add_source("backing", backing_path)

        # Load preferred takes for other instruments
        for inst, take_info in track.preferred_takes.items():
            if inst == self._session.instrument:
                continue  # Don't play back the instrument we're recording
            take_path = project.completed_takes_dir / take_info.filename
            if take_path.exists():
                self._engine.mixer.add_source(
                    f"take-{inst}", take_path, volume=take_info.volume
                )

    def _on_state_change(self, new_state: SessionState) -> None:
        """Called when session state changes."""
        if new_state == SessionState.ENDED:
            self.app.call_from_thread(self._finish_session)

    def _on_song_finished(self) -> None:
        """Called from audio engine when backing track reaches the end."""
        if self._session and self._session.state == SessionState.PLAYING:
            frame = self._engine.recorder.frames_written if self._engine and self._engine.recorder else 0
            self._session.song_end(recording_frame=frame)
            self.app.call_from_thread(
                self._log, "Song finished (auto)"
            )

    def _update_ui(self) -> None:
        """Periodic UI update."""
        if not self._session or not self._engine:
            return

        track = self._session.current_track
        transport = self.query_one("#transport-panel", TransportWidget)
        transport.update_display(
            track_name=track.name if track else "",
            state=self._session.state,
            session_elapsed=self._session.elapsed,
            song_position=self._engine.mixer.position_seconds,
            song_duration=self._engine.mixer.duration_seconds,
        )

        vu = self.query_one("#vu-panel", VUMeterWidget)
        vu.update_level(self._engine.peak_level)

        # Update footer with valid actions
        footer_text = self._get_footer_text()
        footer = self.query_one("#session-footer", Label)
        footer.update(footer_text)

    def _get_footer_text(self) -> str:
        if not self._session:
            return ""
        state = self._session.state
        if state == SessionState.WAITING:
            return "[r] Start Recording  |  [q] End Session"
        elif state == SessionState.PLAYING:
            return "[b] Back to Start  |  [e] End Song  |  [q] End Session"
        elif state == SessionState.BETWEEN_TRACKS:
            if self._session.has_more_tracks:
                return "[n] Next Track  |  [q] End Session"
            else:
                return "Last track done!  |  [q] End Session"
        return ""

    def _log(self, message: str) -> None:
        elapsed = self._session.elapsed if self._session else 0.0
        try:
            log_widget = self.query_one("#log-panel", SessionLogWidget)
            log_widget.add_entry(elapsed, message)
        except Exception:
            pass

    # --- Key actions ---

    def action_record(self) -> None:
        if not self._session or self._session.state != SessionState.WAITING:
            return
        frame = self._engine.recorder.frames_written if self._engine and self._engine.recorder else 0
        self._session.start_recording(recording_frame=frame)
        self._engine.mixer.reset()
        self._engine.mixer.set_playing(True)
        self._log(f"Recording: {self._session.current_track.name}")

    def action_back_to_start(self) -> None:
        if not self._session or self._session.state != SessionState.PLAYING:
            return
        frame = self._engine.recorder.frames_written if self._engine and self._engine.recorder else 0
        self._session.back_to_start(recording_frame=frame)
        self._engine.mixer.reset()
        self._engine.mixer.set_playing(True)
        self._log("Back to start (restart take)")

    def action_song_end(self) -> None:
        if not self._session or self._session.state != SessionState.PLAYING:
            return
        frame = self._engine.recorder.frames_written if self._engine and self._engine.recorder else 0
        self._session.song_end(recording_frame=frame)
        self._engine.mixer.set_playing(False)
        self._log("Song end (take completed)")

        # Update track list
        tl = self.query_one("#session-tracklist", TrackListWidget)
        tl.update_setlist(
            self._session.project.setlist,
            self._session.current_track_index,
            self._session.instrument,
        )

    def action_next_track(self) -> None:
        if not self._session or self._session.state != SessionState.BETWEEN_TRACKS:
            return
        self._session.next_track()
        if self._session.state == SessionState.WAITING:
            self._load_current_track()
            self._log(f"Next track: {self._session.current_track.name}")
            tl = self.query_one("#session-tracklist", TrackListWidget)
            tl.update_setlist(
                self._session.project.setlist,
                self._session.current_track_index,
                self._session.instrument,
            )
        elif self._session.state == SessionState.ENDED:
            self._finish_session()

    def action_end_session(self) -> None:
        if not self._session:
            return
        self._session.end_session()

    def _finish_session(self) -> None:
        """Clean up and transition to post-session screen."""
        if self._update_timer:
            self._update_timer.stop()

        # Stop audio
        if self._engine:
            self._engine.mixer.set_playing(False)
            self._engine.stop()

        # Save session log
        if self._session:
            self._session.save_log()

        # Store references for post-session processing
        self.app.last_session_dir = self._session.session_dir if self._session else None
        self.app.last_raw_recording = self._raw_recording_path

        self._log("Session ended")

        # Go to post-session screen
        self.app.switch_screen("post_session")
