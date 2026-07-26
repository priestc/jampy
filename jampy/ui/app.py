"""Jam.py graphical interface entry point."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .instruments import InstrumentsFrame
from .recording_devices import RecordingDevicesFrame
from .studio_setup import StudioSetupFrame

TABS = [
    ("Studio Setup", StudioSetupFrame),
    ("Recording Devices", RecordingDevicesFrame),
    ("Instruments", InstrumentsFrame),
]


def run() -> None:
    root = tk.Tk()
    root.title("Jam.py")
    root.geometry("820x600")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=16, pady=16)

    containers = []
    for title, frame_cls in TABS:
        container = ttk.Frame(notebook)
        notebook.add(container, text=title)
        containers.append((container, frame_cls))

    def rebuild(container: ttk.Frame, frame_cls) -> None:
        # Reload from disk each time a tab is shown, so edits saved on one
        # tab (e.g. a new input label) are visible on tabs that depend on it.
        for child in container.winfo_children():
            child.destroy()
        frame_cls(container).pack(fill="both", expand=True)

    def on_tab_changed(_event: object) -> None:
        index = notebook.index(notebook.select())
        rebuild(*containers[index])

    rebuild(*containers[0])
    notebook.bind("<<NotebookTabChanged>>", on_tab_changed)

    root.mainloop()
