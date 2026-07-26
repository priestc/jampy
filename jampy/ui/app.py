"""Jam.py graphical interface entry point."""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox, ttk

from .app_state import AppState
from .instruments import InstrumentsFrame
from .record import RecordFrame
from .recording_devices import RecordingDevicesFrame
from .remote import RemoteFrame
from .studio_setup import StudioSetupFrame

# (title, frame class, persistent). Persistent tabs are built once and never
# torn down on tab switches — needed for Record, which holds a live audio
# stream / ffmpeg process / camera handle that a rebuild would kill outright,
# and for Remote, which holds the connected RemoteClient / hosted RemoteServer.
TABS = [
    ("Studio Setup", StudioSetupFrame, False),
    ("Recording Devices", RecordingDevicesFrame, False),
    ("Instruments", InstrumentsFrame, False),
    ("Record", RecordFrame, True),
    ("Remote", RemoteFrame, True),
]


def run(remote_ip: str | None = None) -> None:
    root = tk.Tk()
    if sys.platform.startswith("linux"):
        # Many Linux setups (bad EDID physical-size data, VMs, some laptop
        # panels) make Tk miscompute the display DPI, so point-sized fonts
        # render much smaller than the same code produces on macOS. Pin the
        # DPI assumption to 96, what a 100%-scaled Linux desktop actually
        # uses, so text comes out the same size as on macOS.
        root.tk.call("tk", "scaling", 96 / 72)
    root.title("Jam.py")
    root.geometry("1100x650")

    app_state = AppState()

    status_var = tk.StringVar(value="")
    status_label = ttk.Label(root, textvariable=status_var, foreground="#2a6db0")
    status_label.pack(fill="x", padx=16, pady=(12, 0))

    def on_state_change() -> None:
        status_var.set(f"Remote: {app_state.remote_name}" if app_state.remote_name else "")

    app_state.add_listener(on_state_change)
    on_state_change()

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
        entry["cls"](entry["frame"], app_state).pack(fill="both", expand=True)
        entry["built"] = True

    def on_tab_changed(_event: object) -> None:
        index = notebook.index(notebook.select())
        rebuild(containers[index])

    rebuild(containers[0])
    notebook.bind("<<NotebookTabChanged>>", on_tab_changed)

    if remote_ip:
        from .remote import connect_async

        def on_remote_error(error: str) -> None:
            messagebox.showerror("Could not connect", f"{remote_ip}: {error}")

        config = app_state.backend.get_config()
        connect_async(
            root, app_state, remote_ip, config.remote_server_port, config.remote_token,
            on_done=lambda: notebook.select(len(containers) - 1),
            on_error=on_remote_error,
        )

    root.mainloop()
