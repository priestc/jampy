"""VU meter widget for input level monitoring."""

from __future__ import annotations

import math

from textual.widgets import Static, Label
from textual.containers import Vertical


class VUMeterWidget(Static):
    """Simple text-based VU meter showing input level."""

    BAR_WIDTH = 25
    BLOCKS = " ▏▎▍▌▋▊▉█"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._level: float = 0.0
        self._peak: float = 0.0
        self._peak_decay: float = 0.0

    def compose(self):
        yield Vertical(
            Label("Input Level", id="vu-title"),
            Label("", id="vu-bar"),
            Label("", id="vu-db"),
        )

    def update_level(self, level: float) -> None:
        """Update the VU meter with a new peak level (0.0 - 1.0)."""
        self._level = min(level, 1.0)

        # Peak hold with decay
        if self._level > self._peak:
            self._peak = self._level
            self._peak_decay = 0.0
        else:
            self._peak_decay += 0.05
            self._peak = max(self._level, self._peak - self._peak_decay * 0.02)

        db = 20 * math.log10(max(self._level, 1e-10))
        db_str = f"{db:+.1f} dB" if db > -60 else "-inf dB"

        # Build bar
        filled = int(self._level * self.BAR_WIDTH)
        bar = "█" * filled + "░" * (self.BAR_WIDTH - filled)

        # Color indicator
        if self._level > 0.9:
            indicator = "[red]CLIP[/red]"
        elif self._level > 0.7:
            indicator = "[yellow]HOT[/yellow]"
        else:
            indicator = "[green]OK[/green]"

        try:
            self.query_one("#vu-bar", Label).update(f"[{bar}] {indicator}")
            self.query_one("#vu-db", Label).update(db_str)
        except Exception:
            pass
