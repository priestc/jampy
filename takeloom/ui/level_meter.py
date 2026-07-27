"""LevelMeter: a small horizontal VU-style bar for the Record tab.

Fast attack / slow decay so a single instantaneous peak reading (as sampled
each poll) still reads as a smooth, "dancing" meter rather than a flickering
one, with green/yellow/red zones like a hardware VU meter.
"""

from __future__ import annotations

import tkinter as tk


class LevelMeter(tk.Canvas):
    _BG = "#1a1a1a"
    _LOW = "#2a7d2a"
    _MID = "#c9a227"
    _HOT = "#b00020"
    _LOW_ZONE = 0.7
    _MID_ZONE = 0.9

    def __init__(self, master: tk.Misc, height: int = 14) -> None:
        super().__init__(master, height=height, background=self._BG, highlightthickness=0)
        self._displayed = 0.0
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_level(self, level: float) -> None:
        level = max(0.0, min(1.0, level))
        if level > self._displayed:
            self._displayed = level  # fast attack
        else:
            self._displayed *= 0.75  # slow decay
            if self._displayed < 0.01:
                self._displayed = 0.0
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        filled = int(width * self._displayed)
        if width <= 1 or filled <= 0:
            return
        low_end = min(filled, int(width * self._LOW_ZONE))
        mid_end = min(filled, int(width * self._MID_ZONE))
        self.create_rectangle(0, 0, low_end, height, fill=self._LOW, width=0)
        if filled > low_end:
            self.create_rectangle(low_end, 0, mid_end, height, fill=self._MID, width=0)
        if filled > mid_end:
            self.create_rectangle(mid_end, 0, filled, height, fill=self._HOT, width=0)
