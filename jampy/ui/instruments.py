"""Instruments screen: instrument name-to-input assignment.

Mirrors the `jampy setup-instruments` CLI command. Reads/writes whichever
machine's config `app_state.backend` currently points at.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError
from ..config import Instrument, StudioConfig
from .app_state import AppState


class _InstrumentRow:
    """One editable instrument row, gridded directly into the shared table
    so its columns line up exactly with the header above."""

    def __init__(
        self,
        table: ttk.Frame,
        row: int,
        input_label_names: list[str],
        on_remove,
        name: str = "",
        input_label: str = "",
        full_name: str = "",
        musician: str = "",
    ) -> None:
        self._on_remove = on_remove

        self.name_var = tk.StringVar(value=name)
        self.input_label_var = tk.StringVar(value=input_label or (input_label_names[0] if input_label_names else ""))
        self.full_name_var = tk.StringVar(value=full_name)
        self.musician_var = tk.StringVar(value=musician)

        self.widgets = [
            ttk.Entry(table, textvariable=self.name_var, width=12),
            ttk.Combobox(table, textvariable=self.input_label_var, values=input_label_names, state="readonly", width=13),
            ttk.Entry(table, textvariable=self.full_name_var, width=20),
            ttk.Entry(table, textvariable=self.musician_var, width=12),
            ttk.Button(table, text="Remove", command=lambda: self._on_remove(self)),
        ]
        for col, widget in enumerate(self.widgets):
            widget.grid(row=row, column=col, sticky="w", padx=(0, 6), pady=2)

    def set_row(self, row: int) -> None:
        for widget in self.widgets:
            widget.grid(row=row)

    def destroy(self) -> None:
        for widget in self.widgets:
            widget.destroy()

    def to_instrument(self) -> Instrument | None:
        name = self.name_var.get().strip()
        input_label = self.input_label_var.get().strip()
        if not name or not input_label:
            return None
        return Instrument(
            name=name, input_label=input_label,
            full_name=self.full_name_var.get().strip(),
            musician=self.musician_var.get().strip(),
        )


class InstrumentsFrame(ttk.Frame):
    """Form assigning instruments to configured input labels."""

    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None
        self._rows: list[_InstrumentRow] = []

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
            return
        for child in self.winfo_children():
            child.destroy()
        self._rows = []
        if error or config is None:
            ttk.Label(self, text=error or "Could not load configuration.", foreground="#b00020").pack(anchor="w")
            return
        self.config_obj = config
        self._build()

    def _build(self) -> None:
        if not self.config_obj.input_labels:
            ttk.Label(
                self,
                text="No inputs configured. Set them up on the Recording Devices tab first.",
                foreground="#b00020",
            ).pack(anchor="w")
            return

        input_label_names = [il.label for il in self.config_obj.input_labels]

        ttk.Label(self, text="Instruments", font=("TkDefaultFont", 14, "bold")).pack(anchor="w", pady=(0, 12))

        self.table = ttk.Frame(self)
        self.table.pack(fill="x")

        for col, text in enumerate(["Name", "Input", "Full name", "Musician", ""]):
            ttk.Label(self.table, text=text).grid(row=0, column=col, sticky="w", padx=(0, 6))

        for inst in self.config_obj.instruments:
            self._add_row(
                input_label_names, name=inst.name, input_label=inst.input_label,
                full_name=inst.full_name, musician=inst.musician,
            )
        if not self.config_obj.instruments:
            self._add_row(input_label_names)

        ttk.Button(self, text="+ Add Instrument", command=lambda: self._add_row(input_label_names)).pack(
            anchor="w", pady=(8, 10)
        )

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, foreground="#2a7d2a").pack(anchor="w", pady=(12, 0))

        self.save_button = ttk.Button(self, text="Save", command=self._on_save)
        self.save_button.pack(anchor="e", pady=(12, 0))

    def _add_row(
        self, input_label_names: list[str], name: str = "", input_label: str = "",
        full_name: str = "", musician: str = "",
    ) -> None:
        row_index = len(self._rows) + 1  # row 0 is the header
        row = _InstrumentRow(
            self.table, row_index, input_label_names, self._remove_row,
            name=name, input_label=input_label, full_name=full_name, musician=musician,
        )
        self._rows.append(row)

    def _remove_row(self, row: _InstrumentRow) -> None:
        row.destroy()
        self._rows.remove(row)
        for idx, remaining in enumerate(self._rows, start=1):
            remaining.set_row(idx)

    def _on_save(self) -> None:
        instruments = [inst for row in self._rows if (inst := row.to_instrument())]
        self.config_obj.instruments = instruments

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
