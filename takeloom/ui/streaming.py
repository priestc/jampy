"""Streaming tab: turn on live streaming to YouTube and set the stream key.

When enabled, every session automatically streams live for its whole
duration — starting the moment recording starts and ending the moment it
stops (see backend.py's _begin_session_locked/_end_session and
takeloom/streaming.py). Reads/writes whichever machine's config
app_state.backend currently points at, same as Studio Setup."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError
from ..config import StudioConfig
from .app_state import AppState


class StreamingFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None

        ttk.Label(self, text="Loading...").pack(anchor="w")
        self._load()

    # --- loading ---

    def _load(self) -> None:
        backend = self.app_state.backend

        def worker() -> None:
            try:
                config = backend.get_config()
                error = None
            except BackendError as e:
                config, error = None, str(e)
            self.after(0, lambda: self._on_loaded(config, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, config: StudioConfig | None, error: str | None) -> None:
        if not self.winfo_exists():
            return
        for child in self.winfo_children():
            child.destroy()
        if error or config is None:
            ttk.Label(self, text=error or "Could not load configuration.", foreground="#b00020").pack(anchor="w")
            return
        self.config_obj = config
        self._build()

    # --- build ---

    def _build(self) -> None:
        ttk.Label(self, text="Streaming", font=("TkDefaultFont", 14, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        ttk.Label(
            self,
            text="When enabled, every session streams live to YouTube for its whole duration — the "
                 "stream starts the moment Record is pressed and ends the moment the session stops. "
                 "Requires a camera to be configured on the Recording Devices tab.",
            foreground="#666666", wraplength=560, justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 16))

        self.enabled_var = tk.BooleanVar(value=self.config_obj.streaming_enabled)
        ttk.Checkbutton(
            self, text="Stream every session live to YouTube", variable=self.enabled_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(self, text="YouTube stream key").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        self.key_var = tk.StringVar(value=self.config_obj.youtube_stream_key)
        ttk.Entry(self, textvariable=self.key_var, width=48, show="*").grid(row=3, column=1, sticky="ew", pady=4)
        self.columnconfigure(1, weight=1)

        ttk.Label(
            self, text="Find this under “Go Live” in YouTube Studio (studio.youtube.com).",
            foreground="#666666",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 16))

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, foreground="#2a7d2a").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

        button_row = ttk.Frame(self)
        button_row.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.save_button = ttk.Button(button_row, text="Save", command=self._on_save)
        self.save_button.pack(side="right")

    # --- save ---

    def _on_save(self) -> None:
        self.config_obj.streaming_enabled = self.enabled_var.get()
        self.config_obj.youtube_stream_key = self.key_var.get().strip()

        errors = self.config_obj.validate()
        if errors:
            messagebox.showerror("Invalid configuration", "\n".join(errors))
            return

        backend = self.app_state.backend
        config = self.config_obj
        self.save_button.state(["disabled"])

        def worker() -> None:
            try:
                backend.save_config(config)
                error = None
            except BackendError as e:
                error = str(e)
            self.after(0, lambda: self._on_saved(error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_saved(self, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self.save_button.state(["!disabled"])
        if error:
            messagebox.showerror("Save failed", error)
            return
        target = f"remote ({self.app_state.remote_name})" if self.app_state.backend.is_remote() else "local config"
        self.status_var.set(f"Saved to {target}")
