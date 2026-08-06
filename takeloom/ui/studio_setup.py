"""Studio Setup screen: studio identity, input labels, and instrument
assignment.

Mirrors the fields configured by the `takeloom setup-studio` CLI command,
plus the input-label and instrument sections of `setup-recording-devices`
and `setup-instruments` — grouped here because instruments are just names
attached to input labels, so editing both together avoids bouncing between
tabs. Sample rate/buffer/output device/camera/Stream Deck selection stays
on the Recording Devices tab (see recording_devices.py). Reads/writes
whichever machine's config `app_state.backend` currently points at — local
by default, or a connected remote instance's config in remote mode.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError
from ..config import Instrument, InputLabel, StudioConfig
from .app_state import AppState

FIELDS = [
    ("studio_name", "Studio name"),
    ("studio_location", "Studio location"),
    ("studio_musician", "Studio musician (default performer)"),
    ("backup_server", "Backup server (user@host:/path)"),
    ("session_vault_path", "Studio Session Vault path"),
    ("inspiration_server", "Inspiration server URL"),
    ("inspiration_api_key", "Inspiration API key"),
]

_VAULT_MODE_LABELS = [("local", "Local only"), ("remote", "Remote only"), ("both", "Both")]


class _InputRow(ttk.Frame):
    """One editable input-label row: label text, device choice, channel number."""

    def __init__(
        self,
        master: tk.Misc,
        input_devices: list[dict],
        on_remove,
        label: str = "",
        device: str = "",
        channel: int = 1,
    ) -> None:
        super().__init__(master)
        self.input_devices = input_devices
        self._on_remove = on_remove

        self.label_var = tk.StringVar(value=label)
        self.device_var = tk.StringVar(value=device)
        self.channel_var = tk.IntVar(value=channel or 1)

        ttk.Entry(self, textvariable=self.label_var, width=18).grid(row=0, column=0, padx=(0, 6))

        self.device_box = ttk.Combobox(self, textvariable=self.device_var, width=28)
        self.device_box.grid(row=0, column=1, padx=(0, 6))
        self.device_box.bind("<<ComboboxSelected>>", lambda _e: self._update_channel_range())

        self.channel_spin = ttk.Spinbox(self, from_=1, to=64, textvariable=self.channel_var, width=4)
        self.channel_spin.grid(row=0, column=2, padx=(0, 6))

        ttk.Button(self, text="Remove", command=lambda: self._on_remove(self)).grid(row=0, column=3)

        self.set_input_devices(input_devices)

    def set_input_devices(self, input_devices: list[dict]) -> None:
        """Refresh the device dropdown's choices, e.g. after a device reload."""
        self.input_devices = input_devices
        device_names = [d["name"] for d in input_devices]
        self.device_box.configure(values=device_names, state="readonly" if device_names else "normal")
        self._update_channel_range()

    def _update_channel_range(self) -> None:
        dev = next((d for d in self.input_devices if d["name"] == self.device_var.get()), None)
        max_ch = dev["max_input_channels"] if dev else 64
        self.channel_spin.configure(to=max(1, max_ch))
        if max_ch and self.channel_var.get() > max_ch:
            self.channel_var.set(max_ch)

    def to_input_label(self) -> InputLabel | None:
        label = self.label_var.get().strip()
        device = self.device_var.get().strip()
        if not label or not device:
            return None
        return InputLabel(label=label, device=device, channel=self.channel_var.get())


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


class StudioSetupFrame(ttk.Frame):
    """Form for studio identity, input labels, and instrument assignment."""

    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None
        self.input_devices: list[dict] = []
        self.table: ttk.Frame | None = None
        self._vars: dict[str, tk.StringVar] = {}
        self._input_rows: list[_InputRow] = []
        self._instrument_rows: list[_InstrumentRow] = []

        ttk.Label(self, text="Loading...").pack(anchor="w")
        self._load()

    # --- loading ---

    def _load(self) -> None:
        backend = self.app_state.backend

        def worker() -> None:
            try:
                config = backend.get_config()
                devices = backend.list_audio_devices()
                error = None
            except BackendError as e:
                config, devices, error = None, [], str(e)
            self.after(0, lambda: self._on_loaded(config, devices, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, config: StudioConfig | None, devices: list[dict], error: str | None) -> None:
        if not self.winfo_exists():
            return  # tab was switched away (and rebuilt/destroyed) before this load finished
        for child in self.winfo_children():
            child.destroy()
        self._input_rows = []
        self._instrument_rows = []
        if error or config is None:
            ttk.Label(self, text=error or "Could not load configuration.", foreground="#b00020").pack(anchor="w")
            return
        self.config_obj = config
        self.input_devices = [d for d in devices if d["max_input_channels"] > 0]
        self._build()

    # --- build ---

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
            if attr == "session_vault_path":
                ttk.Label(self, text="Vault storage").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
                self.vault_mode_var = tk.StringVar(value=self._current_vault_mode_label())
                ttk.Combobox(
                    self, textvariable=self.vault_mode_var, values=[label for _v, label in _VAULT_MODE_LABELS],
                    state="readonly", width=16,
                ).grid(row=row, column=1, sticky="w", pady=4)
                row += 1
                ttk.Label(
                    self,
                    text="Where recorded sessions (the continuous audio/video, not the setlist itself) are "
                         "stored. \"Remote only\" pushes each session to the backup server above and removes "
                         "the local copy once that's verified; \"Both\" pushes but keeps the local copy too.",
                    foreground="#666666", wraplength=440, justify="left",
                ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 10))
                row += 1

        self.columnconfigure(1, weight=1)

        row = self._build_input_labels(row)
        row = self._build_instruments(row)

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, foreground="#2a7d2a").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )
        row += 1

        button_row = ttk.Frame(self)
        button_row.grid(row=row, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.save_button = ttk.Button(button_row, text="Save", command=self._on_save)
        self.save_button.pack(side="right")

    def _build_input_labels(self, row: int) -> int:
        ttk.Separator(self, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1

        header = ttk.Frame(self)
        header.grid(row=row, column=0, columnspan=2, sticky="ew")
        ttk.Label(header, text="Input Labels", font=("TkDefaultFont", 11, "bold")).pack(side="left")
        ttk.Button(header, text="Reload Devices", command=self._on_reload_devices).pack(side="right")
        row += 1

        if not self.input_devices:
            ttk.Label(
                self, text="Could not query audio devices (sounddevice unavailable).",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1

        self.rows_container = ttk.Frame(self)
        self.rows_container.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        row += 1

        for il in self.config_obj.input_labels:
            self._add_input_row(label=il.label, device=il.device, channel=il.channel)
        if not self.config_obj.input_labels:
            self._add_input_row()

        ttk.Button(self, text="+ Add Input", command=self._add_input_row).grid(
            row=row, column=0, sticky="w", pady=(4, 10)
        )
        row += 1
        return row

    def _add_input_row(self, label: str = "", device: str = "", channel: int = 1) -> None:
        row_widget = _InputRow(
            self.rows_container, self.input_devices, self._remove_input_row,
            label=label, device=device, channel=channel,
        )
        row_widget.pack(fill="x", pady=2)
        self._input_rows.append(row_widget)

    def _remove_input_row(self, row_widget: _InputRow) -> None:
        row_widget.destroy()
        self._input_rows.remove(row_widget)

    def _on_reload_devices(self) -> None:
        backend = self.app_state.backend

        def worker() -> None:
            try:
                devices = backend.list_audio_devices()
                error = None
            except BackendError as e:
                devices, error = [], str(e)
            self.after(0, lambda: self._on_devices_reloaded(devices, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_devices_reloaded(self, devices: list[dict], error: str | None) -> None:
        if not self.winfo_exists():
            return
        if error:
            messagebox.showerror("Reload failed", error)
            return
        self.input_devices = [d for d in devices if d["max_input_channels"] > 0]
        for row_widget in self._input_rows:
            row_widget.set_input_devices(self.input_devices)
        self.status_var.set(f"Devices reloaded ({len(self.input_devices)} inputs found).")

    def _build_instruments(self, row: int) -> int:
        ttk.Separator(self, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1

        ttk.Label(self, text="Instruments", font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        input_label_names = self._current_input_label_names()
        if not input_label_names:
            ttk.Label(
                self, text="Add an input label above before assigning instruments.",
                foreground="#666666",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
            row += 1
            return row

        self.table = ttk.Frame(self)
        self.table.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        row += 1

        for col, text in enumerate(["Name", "Input", "Full name", "Musician", ""]):
            ttk.Label(self.table, text=text).grid(row=0, column=col, sticky="w", padx=(0, 6))

        for inst in self.config_obj.instruments:
            self._add_instrument_row(
                input_label_names, name=inst.name, input_label=inst.input_label,
                full_name=inst.full_name, musician=inst.musician,
            )
        if not self.config_obj.instruments:
            self._add_instrument_row(input_label_names)

        ttk.Button(
            self, text="+ Add Instrument",
            command=lambda: self._add_instrument_row(self._current_input_label_names()),
        ).grid(row=row, column=0, sticky="w", pady=(8, 10))
        row += 1
        return row

    def _current_input_label_names(self) -> list[str]:
        return [name for row in self._input_rows if (name := row.label_var.get().strip())]

    def _add_instrument_row(
        self, input_label_names: list[str], name: str = "", input_label: str = "",
        full_name: str = "", musician: str = "",
    ) -> None:
        if self.table is None:
            return
        row_index = len(self._instrument_rows) + 1  # row 0 is the header
        row = _InstrumentRow(
            self.table, row_index, input_label_names, self._remove_instrument_row,
            name=name, input_label=input_label, full_name=full_name, musician=musician,
        )
        self._instrument_rows.append(row)

    def _remove_instrument_row(self, row: _InstrumentRow) -> None:
        row.destroy()
        self._instrument_rows.remove(row)
        for idx, remaining in enumerate(self._instrument_rows, start=1):
            remaining.set_row(idx)

    # --- save ---

    def _current_vault_mode_label(self) -> str:
        for value, label in _VAULT_MODE_LABELS:
            if value == self.config_obj.session_vault_mode:
                return label
        return _VAULT_MODE_LABELS[0][1]  # "Local only" — the safe default for an unrecognized config value

    def _on_save(self) -> None:
        for attr, _label in FIELDS:
            setattr(self.config_obj, attr, self._vars[attr].get())
        for value, label in _VAULT_MODE_LABELS:
            if label == self.vault_mode_var.get():
                self.config_obj.session_vault_mode = value
                break
        self.config_obj.input_labels = [il for row in self._input_rows if (il := row.to_input_label())]
        self.config_obj.instruments = [inst for row in self._instrument_rows if (inst := row.to_instrument())]

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
