"""Transport display: current track, elapsed time, session state."""

from __future__ import annotations

from textual.widgets import Static, Label
from textual.containers import Horizontal, Vertical

from ...session import SessionState
from ...utils import format_duration


STATE_LABELS = {
    SessionState.IDLE: ("IDLE", "state-idle"),
    SessionState.WAITING: ("WAITING - Press [r] to record", "state-waiting"),
    SessionState.PLAYING: ("RECORDING", "state-playing"),
    SessionState.BETWEEN_TRACKS: ("BETWEEN TRACKS - [n] next / [q] end", "state-between"),
    SessionState.ENDED: ("SESSION ENDED", "state-ended"),
}


class TransportWidget(Static):
    """Shows current track name, state, and elapsed time."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._track_name = ""
        self._state = SessionState.IDLE
        self._elapsed = 0.0
        self._song_position = 0.0
        self._song_duration = 0.0

    def compose(self):
        yield Vertical(
            Label("No track", id="transport-track"),
            Label("", id="transport-state", classes="state-label state-idle"),
            Horizontal(
                Label("Session: 00:00", id="transport-session-time"),
                Label("  |  ", id="transport-sep"),
                Label("Song: 00:00 / 00:00", id="transport-song-time"),
            ),
        )

    def update_display(
        self,
        track_name: str = "",
        state: SessionState = SessionState.IDLE,
        session_elapsed: float = 0.0,
        song_position: float = 0.0,
        song_duration: float = 0.0,
    ) -> None:
        self._track_name = track_name
        self._state = state
        self._elapsed = session_elapsed
        self._song_position = song_position
        self._song_duration = song_duration

        try:
            track_label = self.query_one("#transport-track", Label)
            track_label.update(track_name or "No track")

            state_label = self.query_one("#transport-state", Label)
            text, css_class = STATE_LABELS.get(state, ("UNKNOWN", "state-idle"))
            state_label.update(text)
            # Reset state classes
            for cls in ("state-idle", "state-waiting", "state-playing", "state-between", "state-ended"):
                state_label.remove_class(cls)
            state_label.add_class(css_class)

            session_time = self.query_one("#transport-session-time", Label)
            session_time.update(f"Session: {format_duration(session_elapsed)}")

            song_time = self.query_one("#transport-song-time", Label)
            song_time.update(
                f"Song: {format_duration(song_position)} / {format_duration(song_duration)}"
            )
        except Exception:
            pass  # Widget may not be mounted yet
