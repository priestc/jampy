"""Recording Devices screen: sample rate, buffer, output device, input labels, camera.

Mirrors the fields configured by the `takeloom setup-recording-devices` CLI
command. Reads/writes whichever machine's config `app_state.backend`
currently points at, and queries that same machine's audio/camera devices.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError
from ..config import (
    InputLabel,
    StudioConfig,
    VALID_BUFFER_SIZES,
    VALID_SAMPLE_RATES,
)
from .app_state import AppState


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


class RecordingDevicesFrame(ttk.Frame):
    """Form for sample rate, buffer, output device, input labels, and camera."""

    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self.config_obj: StudioConfig | None = None
        self.devices: list[dict] = []
        self.output_devices: list[dict] = []
        self.input_devices: list[dict] = []
        self._camera_choices: list[tuple[str, str]] = []
        self._input_rows: list[_InputRow] = []

        ttk.Label(self, text="Loading...").pack(anchor="w")
        self._initial_load()

    # --- loading ---

    def _initial_load(self) -> None:
        backend = self.app_state.backend

        def worker() -> None:
            try:
                config = backend.get_config()
                devices = backend.list_audio_devices()
                cameras = backend.list_cameras()
                error = None
            except BackendError as e:
                config, devices, cameras, error = None, [], [], str(e)
            self.after(0, lambda: self._on_initial_loaded(config, devices, cameras, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_initial_loaded(
        self, config: StudioConfig | None, devices: list[dict], cameras: list[tuple[str, str]], error: str | None,
    ) -> None:
        if not self.winfo_exists():
            return
        for child in self.winfo_children():
            child.destroy()
        if error or config is None:
            ttk.Label(self, text=error or "Could not load configuration.", foreground="#b00020").pack(anchor="w")
            return
        self.config_obj = config
        self._set_devices(devices, cameras)
        self._input_rows = []
        self._build()

    def _set_devices(self, devices: list[dict], cameras: list[tuple[str, str]]) -> None:
        self.devices = devices
        self.output_devices = [d for d in devices if d["max_output_channels"] > 0]
        self.input_devices = [d for d in devices if d["max_input_channels"] > 0]
        self._camera_choices = [("", "None")] + cameras

    def _build(self) -> None:
        row = 0
        self.reload_button = ttk.Button(self, text="Reload Devices", command=self._on_reload_devices)
        self.reload_button.grid(row=row, column=0, sticky="w", pady=(0, 10))
        row += 1

        if not self.devices:
            ttk.Label(
                self, text="Could not query audio devices (sounddevice unavailable).",
                foreground="#b00020",
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 8))
            row += 1

        ttk.Label(self, text="Sample rate").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.sample_rate_var = tk.StringVar(value=str(self.config_obj.sample_rate))
        ttk.Combobox(
            self, textvariable=self.sample_rate_var,
            values=[str(r) for r in VALID_SAMPLE_RATES], state="readonly", width=10,
        ).grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Label(self, text="Buffer size").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.buffer_size_var = tk.StringVar(value=str(self.config_obj.buffer_size))
        ttk.Combobox(
            self, textvariable=self.buffer_size_var,
            values=[str(b) for b in VALID_BUFFER_SIZES], state="readonly", width=10,
        ).grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Label(self, text="Output device").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.output_device_var = tk.StringVar(value=self.config_obj.output_device)
        self.output_device_box = ttk.Combobox(self, textvariable=self.output_device_var, width=34)
        self.output_device_box.grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Label(self, text="Output channels").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        self.output_channels_var = tk.IntVar(value=self.config_obj.output_channels)
        ttk.Spinbox(self, from_=1, to=32, textvariable=self.output_channels_var, width=6).grid(
            row=row, column=1, sticky="w"
        )
        row += 1

        ttk.Label(self, text="Latency compensation (ms)").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        default_comp = self.config_obj.latency_compensation_ms
        if default_comp == 0.0:
            default_comp = round(self.config_obj.buffer_size / self.config_obj.sample_rate * 1000, 1)
        self.latency_var = tk.StringVar(value=str(default_comp))
        ttk.Entry(self, textvariable=self.latency_var, width=10).grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Separator(self, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1

        ttk.Label(self, text="Input Labels", font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
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

        ttk.Separator(self, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1

        ttk.Label(self, text="Camera", font=("TkDefaultFont", 11, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        ttk.Label(self, text="Camera device").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
        current_display = next(
            (display for dev_id, display in self._camera_choices if dev_id == self.config_obj.camera_device),
            "None",
        )
        self.camera_var = tk.StringVar(value=current_display)
        self.camera_box = ttk.Combobox(self, textvariable=self.camera_var, state="readonly", width=34)
        self.camera_box.grid(row=row, column=1, sticky="w")
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

        self._refresh_device_choices()

    def _refresh_device_choices(self) -> None:
        """Push the current device/camera lists into all dropdowns' choices."""
        output_names = [d["name"] for d in self.output_devices]
        self.output_device_box.configure(values=output_names, state="readonly" if output_names else "normal")

        for row_widget in self._input_rows:
            row_widget.set_input_devices(self.input_devices)

        camera_display = [display for _dev_id, display in self._camera_choices]
        self.camera_box.configure(values=camera_display)

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

    # --- reload devices ---

    def _on_reload_devices(self) -> None:
        """Re-query audio/camera devices without discarding unsaved edits.

        Rebuilds the form from scratch on fresh widgets rather than
        reconfiguring the existing comboboxes' values in place — ttk's
        combobox can leave its dropdown listbox stale/blank when `values`
        is changed on a widget that's already been rendered or clicked.
        """
        self._apply_form_to_config()
        self.reload_button.state(["disabled"])
        backend = self.app_state.backend

        def worker() -> None:
            try:
                devices = backend.list_audio_devices()
                cameras = backend.list_cameras()
                error = None
            except BackendError as e:
                devices, cameras, error = [], [], str(e)
            self.after(0, lambda: self._on_reloaded(devices, cameras, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_reloaded(self, devices: list[dict], cameras: list[tuple[str, str]], error: str | None) -> None:
        if not self.winfo_exists():
            return
        if error:
            messagebox.showerror("Reload failed", error)
            self.reload_button.state(["!disabled"])
            return
        self._set_devices(devices, cameras)
        for child in self.winfo_children():
            child.destroy()
        self._input_rows = []
        self._build()
        self.status_var.set(f"Devices reloaded ({len(self.devices)} found).")

    def _apply_form_to_config(self) -> None:
        """Copy current (possibly unsaved) form values back into config_obj,
        so a device reload or tab switch doesn't discard in-progress edits."""
        try:
            self.config_obj.sample_rate = int(self.sample_rate_var.get())
        except ValueError:
            pass
        try:
            self.config_obj.buffer_size = int(self.buffer_size_var.get())
        except ValueError:
            pass
        self.config_obj.output_device = self.output_device_var.get().strip()
        try:
            self.config_obj.output_channels = int(self.output_channels_var.get())
        except (ValueError, tk.TclError):
            pass
        try:
            self.config_obj.latency_compensation_ms = float(self.latency_var.get())
        except ValueError:
            pass
        self.config_obj.input_labels = [il for row in self._input_rows if (il := row.to_input_label())]

        camera_display = self.camera_var.get()
        camera_device, camera_label = next(
            ((dev_id, display) for dev_id, display in self._camera_choices if display == camera_display),
            (self.config_obj.camera_device, self.config_obj.camera_label),
        )
        self.config_obj.camera_device = camera_device
        self.config_obj.camera_label = camera_label

    def _on_save(self) -> None:
        try:
            int(self.sample_rate_var.get())
            int(self.buffer_size_var.get())
            int(self.output_channels_var.get())
            float(self.latency_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror(
                "Invalid configuration",
                "Sample rate, buffer size, output channels, and latency must be numbers.",
            )
            return

        self._apply_form_to_config()

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
