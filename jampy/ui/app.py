"""Jam.py graphical interface entry point."""

from __future__ import annotations

import tkinter as tk

from .studio_setup import StudioSetupFrame


def run() -> None:
    root = tk.Tk()
    root.title("Jam.py")
    root.geometry("520x420")
    frame = StudioSetupFrame(root)
    frame.pack(fill="both", expand=True, padx=16, pady=16)
    root.mainloop()
