"""Takeloom graphical interface entry point."""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox, ttk

from .app_state import AppState
from .instruments import InstrumentsFrame
from .latency import LatencyFrame
from .record import RecordFrame
from .recording_devices import RecordingDevicesFrame
from .remote import RemoteFrame
from .studio_setup import StudioSetupFrame

# (title, frame class, persistent). Persistent tabs are built once and never
# torn down on tab switches — needed for Record, which holds a live audio
# stream / ffmpeg process / camera handle that a rebuild would kill outright;
# for Latency, which holds the same during a camera latency test; and for
# Remote, which holds the connected RemoteClient / hosted RemoteServer.
TABS = [
    ("Record", RecordFrame, True),
    ("Studio Setup", StudioSetupFrame, False),
    ("Recording Devices", RecordingDevicesFrame, False),
    ("Instruments", InstrumentsFrame, False),
    ("Latency", LatencyFrame, True),
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
    root.title("Takeloom")
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

    notebook.bind("<<NotebookTabChanged>>", on_tab_changed)

    # Land on Studio Setup until a studio has been configured (studio_name is
    # the identity field CLI setup treats as "configured"); once one exists,
    # open straight to Record, the tab actually used session to session.
    config = app_state.local_backend.get_config()
    initial_title = "Studio Setup" if not config.studio_name else "Record"
    initial_index = next(i for i, (title, _, _) in enumerate(TABS) if title == initial_title)
    notebook.select(initial_index)
    rebuild(containers[initial_index])

    # Auto-connect on launch: an explicit --remote=IP wins over a configured
    # "always connect" remote. Either way, a host with no matching
    # known_remotes entry connects with no token, kicking off the same
    # pairing/approval flow as a first-time Connect click in the Remote tab.
    target = None
    if remote_ip:
        match = next((r for r in config.known_remotes if r.host == remote_ip), None)
        target = (match.host, match.port, match.token) if match else (remote_ip, config.remote_server_port, "")
    else:
        always = next((r for r in config.known_remotes if r.always_connect), None)
        if always is not None:
            target = (always.host, always.port, always.token)

    if target is not None:
        from .remote import connect_async, remember_remote_token

        host, port, token = target

        def on_remote_error(error: str) -> None:
            messagebox.showerror("Could not connect", f"{host}: {error}")

        def on_remote_done(client) -> None:
            remember_remote_token(app_state, host, port, client)
            notebook.select(len(containers) - 1)

        connect_async(
            root, app_state, host, port, token,
            on_done=on_remote_done,
            on_error=on_remote_error,
        )

    root.mainloop()
