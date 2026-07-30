"""Video Check review popup: plays back an impromptu, throwaway take (opened
in the OS's default player, same as the Latency tab's test clip) and deletes
it the moment this window closes — video check is meant to verify a
recording setup is ready, not to produce a keepable take.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from ..video.capture import open_in_default_player


class VideoCheckDialog(tk.Toplevel):
    """Popup shown after a video check finishes. Auto-plays the result in the
    OS's default player; closing the window (Close button or the window's own
    close box) discards the temp file for good."""

    def __init__(self, master: tk.Misc, result_path: Path, has_video: bool) -> None:
        super().__init__(master)
        self.title("Video Check")
        self.resizable(False, False)
        self.transient(master)

        self._result_path = result_path
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        kind = "Video" if has_video else "Audio"
        ttk.Label(
            frame, text="Video check recorded — opened in your default player.",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w")
        note = (
            "Check your framing, levels, and sync, then close this window — "
            "the video check recording isn't saved anywhere."
        )
        if not has_video:
            note = f"(Audio only — no camera configured.) {note}"
        ttk.Label(frame, text=note, wraplength=360, foreground="#666666").pack(anchor="w", pady=(4, 12))

        button_row = ttk.Frame(frame)
        button_row.pack(anchor="e", fill="x")
        ttk.Button(button_row, text=f"▶ Play {kind} Again", command=self._on_play_again).pack(side="left")
        ttk.Button(button_row, text="Close", command=self._on_close).pack(side="right")

        self._play()

    def _play(self) -> None:
        open_in_default_player(self._result_path)

    def _on_play_again(self) -> None:
        self._play()

    def _on_close(self) -> None:
        self._result_path.unlink(missing_ok=True)
        self.destroy()
