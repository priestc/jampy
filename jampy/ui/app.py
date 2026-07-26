"""Jam.py graphical interface entry point."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .instruments import InstrumentsFrame
from .record import RecordFrame
from .recording_devices import RecordingDevicesFrame
from .studio_setup import StudioSetupFrame

# (title, frame class, persistent). Persistent tabs are built once and never
# torn down on tab switches — needed for Record, which holds a live audio
# stream / ffmpeg process / camera handle that a rebuild would kill outright.
TABS = [
    ("Studio Setup", StudioSetupFrame, False),
    ("Recording Devices", RecordingDevicesFrame, False),
    ("Instruments", InstrumentsFrame, False),
    ("Record", RecordFrame, True),
]


def run() -> None:
    root = tk.Tk()
    root.title("Jam.py")
    root.geometry("820x600")

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=16, pady=16)

    containers = []
    for title, frame_cls, persistent in TABS:
        container = ttk.Frame(notebook)
        notebook.add(container, text=title)
        containers.append({"frame": container, "cls": frame_cls, "persistent": persistent, "built": False})

    def rebuild(entry: dict) -> None:
        # Reload from disk each time a tab is shown, so edits saved on one
        # tab (e.g. a new input label) are visible on tabs that depend on it.
        # Persistent tabs are the exception: built once, left alone after.
        if entry["persistent"] and entry["built"]:
            return
        for child in entry["frame"].winfo_children():
            child.destroy()
        entry["cls"](entry["frame"]).pack(fill="both", expand=True)
        entry["built"] = True

    def on_tab_changed(_event: object) -> None:
        index = notebook.index(notebook.select())
        rebuild(containers[index])

    rebuild(containers[0])
    notebook.bind("<<NotebookTabChanged>>", on_tab_changed)

    root.mainloop()
