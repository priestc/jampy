"""On-screen Stream Deck emulator for the Record tab.

Draws the same buttons — icons, labels, colors — the physical Stream Deck
shows for the recording page (see streamdeck_controller.py: RECORDING_
BUTTONS/RECORDING_VOLUME_BUTTONS and the *_visual() helpers), and drives
clicks through the same key characters RecordingDeckDriver.handle_key()
expects, so an on-screen tile behaves exactly like pressing the
corresponding physical key — one set of layout/behavior tables, two
input devices.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from PIL import ImageTk

from ..streamdeck_controller import (
    RECORDING_BUTTONS,
    RECORDING_MONITOR_TOGGLE_KEY_INDEX,
    RECORDING_VOLUME_BUTTONS,
    button_visual,
    monitor_toggle_visual,
    recording_toggle_visual,
    render_button_image,
)
from .platform_style import scaled_px

# Rendered pixel size of each key image — outside ttk's styling system, so
# it needs its own Linux scaling (see platform_style.py) rather than
# picking up the ttk font/padding normalization automatically.
_KEY_SIZE = scaled_px(68)
_COLS = 5
_ALL_BUTTONS: list[tuple] = list(RECORDING_BUTTONS) + list(RECORDING_VOLUME_BUTTONS)


class StreamDeckEmulator(ttk.Frame):
    """Grid of on-screen keys mirroring the physical Stream Deck's
    recording-page layout. `on_key` is called with the same key characters
    RecordingDeckDriver.handle_key() expects — the Record tab wires it
    straight to that method (with "r" special-cased to the existing
    Start/Unpause/Stop button flow) so clicking a tile does exactly what
    pressing the real one would."""

    def __init__(self, master: tk.Misc, on_key: Callable[[str], None], session_capable: bool = True) -> None:
        super().__init__(master)
        self._on_key = on_key
        self._session_capable = session_capable
        self._phase = "idle"
        self._video_check_phase = "idle"
        self._monitoring_mode = "production"
        self._images: dict[int, ImageTk.PhotoImage] = {}
        self._buttons: dict[int, tk.Button] = {}

        for btn in _ALL_BUTTONS:
            idx, _icon, _label, key_char, *_ = btn
            row, col = divmod(idx, _COLS)
            button = tk.Button(
                self, borderwidth=0, highlightthickness=0, relief="flat", cursor="hand2",
                command=lambda k=key_char: self._on_key(k),
            )
            button.grid(row=row, column=col, padx=scaled_px(3), pady=scaled_px(3))
            self._buttons[idx] = button

        self._redraw()

    def _set_face(self, idx: int, icon: str | None, label: str | None, color: tuple) -> None:
        photo = ImageTk.PhotoImage(render_button_image(icon, label, color, size=_KEY_SIZE))
        self._images[idx] = photo  # keep a reference — Tk drops images with no live Python reference
        self._buttons[idx].configure(image=photo)

    def _redraw(self) -> None:
        icon, label, color = recording_toggle_visual(self._phase, self._video_check_phase, self._session_capable)
        self._set_face(0, icon, label, color)
        for btn in _ALL_BUTTONS:
            idx = btn[0]
            if idx in (0, RECORDING_MONITOR_TOGGLE_KEY_INDEX):
                continue
            icon, label, color = button_visual(btn, self._phase)
            self._set_face(idx, icon, label, color)
        icon, label, color = monitor_toggle_visual(self._monitoring_mode)
        self._set_face(RECORDING_MONITOR_TOGGLE_KEY_INDEX, icon, label, color)

    def update_recording_page(self, phase: str, video_check_phase: str = "idle") -> None:
        self._phase = phase
        self._video_check_phase = video_check_phase
        self._redraw()

    def update_monitoring_mode(self, mode: str) -> None:
        self._monitoring_mode = mode
        self._redraw()

    def set_key_enabled(self, idx: int, enabled: bool) -> None:
        """Guard the Record key (idx 0) against double-clicks while a start/
        unpause/stop request is in flight — mirrors the old ttk button's
        .state(["disabled"]) during that same window."""
        self._buttons[idx].configure(state=tk.NORMAL if enabled else tk.DISABLED)
