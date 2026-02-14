"""Scrolling session event log widget."""

from __future__ import annotations

from textual.widgets import Static, Label, RichLog
from textual.containers import Vertical

from ...utils import format_duration


class SessionLogWidget(Static):
    """Displays a scrolling log of session events."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

    def compose(self):
        yield Vertical(
            Label("Session Log", id="log-title"),
            RichLog(id="log-content", wrap=True, markup=True, max_lines=200),
        )

    def add_entry(self, timestamp: float, message: str) -> None:
        time_str = format_duration(timestamp)
        try:
            log = self.query_one("#log-content", RichLog)
            log.write(f"[{time_str}] {message}")
        except Exception:
            pass

    def clear_log(self) -> None:
        try:
            log = self.query_one("#log-content", RichLog)
            log.clear()
        except Exception:
            pass
