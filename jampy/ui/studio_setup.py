"""Studio Setup screen: studio identity and backup/inspiration server settings.

Mirrors the fields configured by the `jampy setup-studio` CLI command.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from ..config import DEFAULT_CONFIG_PATH, StudioConfig

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

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.config_obj = StudioConfig.load()
        self._vars: dict[str, tk.StringVar] = {}

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
        ttk.Button(button_row, text="Save", command=self._on_save).pack(side="right")

    def _on_save(self) -> None:
        for attr, _label in FIELDS:
            setattr(self.config_obj, attr, self._vars[attr].get())

        errors = self.config_obj.validate()
        if errors:
            messagebox.showerror("Invalid configuration", "\n".join(errors))
            return

        self.config_obj.save()
        self.status_var.set(f"Saved to {DEFAULT_CONFIG_PATH}")
