"""Session state machine and event logging."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable

from .utils import timestamp_now, wall_timestamp, ensure_dir
from .project import Project, TrackEntry


class SessionState(Enum):
    IDLE = auto()
    WAITING = auto()      # Waiting for user to press record
    PLAYING = auto()      # Backing track playing, recording
    BETWEEN_TRACKS = auto()  # Song ended, waiting for next/end
    ENDED = auto()


@dataclass
class SessionEvent:
    """A single event in the session log."""
    timestamp: float      # monotonic time since session start
    wall_time: str        # human-readable wall time
    event_type: str       # start, record_start, back_to_start, song_end, next_track, end
    track_index: int
    track_name: str
    details: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "wall_time": self.wall_time,
            "event_type": self.event_type,
            "track_index": self.track_index,
            "track_name": self.track_name,
            "details": self.details,
        }


class Session:
    """Manages the recording session state machine.

    State transitions:
        IDLE → start() → WAITING
        WAITING → start_recording() → PLAYING
        PLAYING → back_to_start() → PLAYING (loops from beginning)
        PLAYING → song_end() → BETWEEN_TRACKS
        BETWEEN_TRACKS → next_track() → WAITING
        Any → end_session() → ENDED
    """

    def __init__(
        self,
        project: Project,
        instrument: str,
        on_state_change: Callable[[SessionState], None] | None = None,
    ) -> None:
        self.project = project
        self.instrument = instrument
        self.state = SessionState.IDLE
        self.current_track_index: int = 0
        self.events: list[SessionEvent] = []
        self._session_start: float = 0.0
        self._recording_frame_start: int = 0  # frame offset in raw recording
        self._had_back_to_start: bool = False  # track if current take had a restart
        self._on_state_change = on_state_change
        self.session_dir: Path | None = None
        self.musician: str = ""
        self.studio_name: str = ""
        self.studio_location: str = ""

    @property
    def current_track(self) -> TrackEntry | None:
        tracks = self.project.setlist.tracks
        if 0 <= self.current_track_index < len(tracks):
            return tracks[self.current_track_index]
        return None

    @property
    def elapsed(self) -> float:
        """Seconds since session started."""
        if self._session_start == 0:
            return 0.0
        return timestamp_now() - self._session_start

    def _log(self, event_type: str, details: str = "") -> None:
        track = self.current_track
        event = SessionEvent(
            timestamp=self.elapsed,
            wall_time=wall_timestamp(),
            event_type=event_type,
            track_index=self.current_track_index,
            track_name=track.name if track else "",
            details=details,
        )
        self.events.append(event)

    def _set_state(self, new_state: SessionState) -> None:
        self.state = new_state
        if self._on_state_change:
            self._on_state_change(new_state)

    def start(self) -> None:
        """Start the session. Transitions IDLE → WAITING."""
        if self.state != SessionState.IDLE:
            return
        self._session_start = timestamp_now()
        # Create session directory
        session_name = wall_timestamp().replace(":", "-").replace(" ", "_")
        self.session_dir = ensure_dir(
            self.project.sessions_dir / f"{session_name}_{self.instrument}"
        )
        self.current_track_index = 0
        self._log("session_start", f"instrument={self.instrument}")
        self._set_state(SessionState.WAITING)

    def start_recording(self, recording_frame: int = 0) -> None:
        """User presses 'r'. Transitions WAITING → PLAYING."""
        if self.state != SessionState.WAITING:
            return
        self._recording_frame_start = recording_frame
        self._had_back_to_start = False
        self._log("record_start", f"frame={recording_frame}")
        self._set_state(SessionState.PLAYING)

    def back_to_start(self, recording_frame: int = 0) -> None:
        """User presses 'b'. Stays in PLAYING, loops from beginning."""
        if self.state != SessionState.PLAYING:
            return
        self._had_back_to_start = True
        self._log("back_to_start", f"frame={recording_frame}")
        # Stay in PLAYING — the mixer/engine will reset position
        self._recording_frame_start = recording_frame

    def song_end(self, recording_frame: int = 0) -> None:
        """User presses 'e' or song finishes. Transitions PLAYING → BETWEEN_TRACKS."""
        if self.state != SessionState.PLAYING:
            return
        self._log("song_end", f"frame={recording_frame}, had_restart={self._had_back_to_start}")
        self._set_state(SessionState.BETWEEN_TRACKS)

    def next_track(self) -> None:
        """User presses 'n'. Transitions BETWEEN_TRACKS → WAITING."""
        if self.state != SessionState.BETWEEN_TRACKS:
            return
        self.current_track_index += 1
        if self.current_track_index >= len(self.project.setlist.tracks):
            self.end_session()
            return
        self._log("next_track")
        self._set_state(SessionState.WAITING)

    def end_session(self) -> None:
        """End the session from any state."""
        self._log("session_end")
        self._set_state(SessionState.ENDED)

    def save_log(self) -> Path | None:
        """Save session log to JSON file."""
        if not self.session_dir:
            return None
        log_path = self.session_dir / "session_log.json"
        data = {
            "instrument": self.instrument,
            "musician": self.musician,
            "project": self.project.name,
            "studio_name": self.studio_name,
            "studio_location": self.studio_location,
            "events": [e.to_dict() for e in self.events],
        }
        log_path.write_text(json.dumps(data, indent=2))
        return log_path

    @property
    def has_more_tracks(self) -> bool:
        return self.current_track_index < len(self.project.setlist.tracks) - 1
