"""Studio Setup screen: studio identity and backup/inspiration server settings.

Mirrors the fields configured by the `takeloom setup-studio` CLI command.
Reads/writes whichever machine's config `app_state.backend` currently points
at — local by default, or a connected remote instance's config in remote mode.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError
from ..config import StudioConfig
from .app_state import AppState

FIELDS = [
    ("studio_name", "Studio name"),
    ("studio_location", "Studio location"),
    ("studio_musician", "Studio musician (default performer)"),
    ("backup_server", "Backup server (user@host:/path)"),
    ("inspiration_server", "Inspiration server URL"),
    ("inspiration_api_key", "Inspiration API key"),
]


class StudioSetupFrame(ttk.Frame):
    """Form for the studio-level settings in studio_config.json."""

    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None
        self._vars: dict[str, tk.StringVar] = {}

        ttk.Label(self, text="Loading...").pack(anchor="w")
        self._load()

    def _load(self) -> None:
        backend = self.app_state.backend

        def worker() -> None:
            try:
                config, error = backend.get_config(), None
            except BackendError as e:
                config, error = None, str(e)
            self.after(0, lambda: self._on_loaded(config, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, config: StudioConfig | None, error: str | None) -> None:
        if not self.winfo_exists():
            return  # tab was switched away (and rebuilt/destroyed) before this load finished
        for child in self.winfo_children():
            child.destroy()
        if error or config is None:
            ttk.Label(self, text=error or "Could not load configuration.", foreground="#b00020").pack(anchor="w")
            return
        self.config_obj = config
        self._build()

    def _build(self) -> None:
        ttk.Label(self, text="Studio Setup", font=("TkDefaultFont", 14, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        row = 1
        for attr, label in FIELDS:
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            var = tk.StringVar(value=getattr(self.config_obj, attr))
            show = "*" if attr == "inspiration_api_key" else ""
            entry = ttk.Entry(self, textvariable=var, width=42, show=show)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            self._vars[attr] = var
            row += 1

        self.columnconfigure(1, weight=1)

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, foreground="#2a7d2a").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )
        row += 1

        button_row = ttk.Frame(self)
        button_row.grid(row=row, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.save_button = ttk.Button(button_row, text="Save", command=self._on_save)
        self.save_button.pack(side="right")

    def _on_save(self) -> None:
        for attr, _label in FIELDS:
            setattr(self.config_obj, attr, self._vars[attr].get())

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
