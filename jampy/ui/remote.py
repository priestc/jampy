"""Remote tab: connect this jampy instance to another one's hardware/config,
and/or turn this instance's own remote server on/off so others can connect
to it.

The "Connect to a Remote Instance" section swaps `app_state.backend` to a
`RemoteBackend`, which every other tab is already wired to use. The "Remote
Server" section is deliberately independent of that — it always reads/writes
`app_state.local_backend` (this machine's own config/hardware), since
whether *this* machine offers itself as a server has nothing to do with
whatever remote you might currently be connected to as a client.
"""

from __future__ import annotations

import secrets
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError, LocalBackend
from ..config import StudioConfig
from ..remote.backend import RemoteBackend
from ..remote.client import RemoteClient
from ..remote.server import RemoteServer
from .app_state import AppState


def connect_async(widget, app_state: AppState, host: str, port: int, token: str, on_done=None, on_error=None) -> None:
    """Connect to a remote jampy instance and swap it into app_state.backend.
    Runs on a background thread; safe to call from the Tk main thread. Shared
    by the Remote tab's Connect button and `jampy ui --remote=IP`."""

    def on_disconnect(reason: str) -> None:
        widget.after(0, lambda: _handle_disconnect(app_state, reason))

    def worker() -> None:
        client = RemoteClient(host, port, token, on_disconnect=on_disconnect)
        try:
            client.connect(timeout=6.0)
            remote_backend = RemoteBackend(client)
            error = None
        except BackendError as e:
            remote_backend = None
            error = str(e)

        def finish() -> None:
            if error:
                if on_error:
                    on_error(error)
                return
            app_state.set_backend(remote_backend, remote_name=remote_backend.hostname())
            if on_done:
                on_done()

        widget.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


def _handle_disconnect(app_state: AppState, reason: str) -> None:
    if not app_state.backend.is_remote():
        return  # already reset by an explicit Disconnect click
    app_state.set_backend(app_state.local_backend)
    messagebox.showwarning("Disconnected", reason)


class RemoteFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self._local_config: StudioConfig | None = None

        self._build_connect_section()
        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=16)
        self._build_server_section()

        self.app_state.add_listener(self._on_app_state_changed)
        self.bind("<Destroy>", self._on_destroy)

        self._refresh_connect_status()
        self._load_local_config()

    def _on_destroy(self, _event: object) -> None:
        self.app_state.remove_listener(self._on_app_state_changed)

    def _on_app_state_changed(self) -> None:
        self._refresh_connect_status()

    # --- connect section ---

    def _build_connect_section(self) -> None:
        frame = ttk.Frame(self)
        frame.pack(fill="x", anchor="w")

        ttk.Label(frame, text="Connect to a Remote Instance", font=("TkDefaultFont", 12, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )

        ttk.Label(frame, text="IP address / host").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.host_var = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.host_var, width=28).grid(row=1, column=1, sticky="w")

        ttk.Label(frame, text="Port").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        self.connect_port_var = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.connect_port_var, width=10).grid(row=2, column=1, sticky="w")

        ttk.Label(frame, text="Token").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        self.connect_token_var = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.connect_token_var, width=28, show="*").grid(row=3, column=1, sticky="w")

        button_row = ttk.Frame(frame)
        button_row.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.connect_button = ttk.Button(button_row, text="Connect", command=self._on_connect)
        self.connect_button.pack(side="left")
        self.disconnect_button = ttk.Button(button_row, text="Disconnect", command=self._on_disconnect_click)
        self.disconnect_button.pack(side="left", padx=(8, 0))

        self.connect_status_var = tk.StringVar(value="Not connected.")
        ttk.Label(frame, textvariable=self.connect_status_var, foreground="#2a6db0").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )

    def _refresh_connect_status(self) -> None:
        if self.app_state.backend.is_remote():
            self.connect_status_var.set(f"Connected: {self.app_state.remote_name}")
            self.connect_button.state(["disabled"])
            self.disconnect_button.state(["!disabled"])
        else:
            self.connect_status_var.set("Not connected.")
            self.connect_button.state(["!disabled"])
            self.disconnect_button.state(["disabled"])

    def _on_connect(self) -> None:
        if self.app_state.recording_active:
            messagebox.showerror("Cannot connect", "Stop the current recording first.")
            return
        host = self.host_var.get().strip()
        if not host:
            messagebox.showerror("Cannot connect", "Enter an IP address or hostname.")
            return
        try:
            port = int(self.connect_port_var.get())
        except ValueError:
            messagebox.showerror("Cannot connect", "Port must be a number.")
            return
        token = self.connect_token_var.get()

        self.connect_button.state(["disabled"])
        self.connect_status_var.set(f"Connecting to {host}:{port}...")

        def on_done() -> None:
            self._refresh_connect_status()
            self._remember_host(host)

        def on_error(error: str) -> None:
            self._refresh_connect_status()
            messagebox.showerror("Connect failed", error)

        connect_async(self, self.app_state, host, port, token, on_done=on_done, on_error=on_error)

    def _remember_host(self, host: str) -> None:
        if self._local_config is None:
            return
        self._local_config.remote_last_host = host
        try:
            self.app_state.local_backend.save_config(self._local_config)
        except BackendError:
            pass

    def _on_disconnect_click(self) -> None:
        if self.app_state.recording_active:
            messagebox.showerror("Cannot disconnect", "Stop the current recording first.")
            return
        if not self.app_state.backend.is_remote():
            return
        self.app_state.set_backend(self.app_state.local_backend)
        self._refresh_connect_status()

    # --- server section (always local_backend, never app_state.backend) ---

    def _build_server_section(self) -> None:
        frame = ttk.Frame(self)
        frame.pack(fill="x", anchor="w")

        ttk.Label(
            frame, text="Remote Server (let others connect to this machine)",
            font=("TkDefaultFont", 12, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self.server_enabled_var = tk.BooleanVar(value=False)
        self.server_checkbox = ttk.Checkbutton(
            frame, text="Enable remote server", variable=self.server_enabled_var, command=self._on_server_toggle,
        )
        self.server_checkbox.grid(row=1, column=0, columnspan=3, sticky="w", pady=4)

        ttk.Label(frame, text="Port").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        self.server_port_var = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.server_port_var, width=10).grid(row=2, column=1, sticky="w")

        ttk.Label(frame, text="Token").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        self.server_token_var = tk.StringVar(value="")
        ttk.Entry(frame, textvariable=self.server_token_var, width=28, show="*").grid(row=3, column=1, sticky="w")
        ttk.Button(frame, text="Generate", command=self._on_generate_token).grid(row=3, column=2, sticky="w", padx=(6, 0))

        self.server_status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.server_status_var, foreground="#2a7d2a").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )

        self.server_save_button = ttk.Button(frame, text="Save", command=self._on_server_save)
        self.server_save_button.grid(row=5, column=0, sticky="w", pady=(8, 0))

    def _on_generate_token(self) -> None:
        self.server_token_var.set(secrets.token_hex(16))

    def _load_local_config(self) -> None:
        backend = self.app_state.local_backend

        def worker() -> None:
            try:
                config, error = backend.get_config(), None
            except BackendError as e:
                config, error = None, str(e)
            self.after(0, lambda: self._on_local_config_loaded(config, error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_local_config_loaded(self, config: StudioConfig | None, error: str | None) -> None:
        if error or config is None:
            self.server_status_var.set(error or "Could not load local configuration.")
            return
        self._local_config = config
        self.host_var.set(config.remote_last_host)
        self.connect_port_var.set(str(config.remote_server_port))
        self.connect_token_var.set(config.remote_token)
        self.server_port_var.set(str(config.remote_server_port))
        self.server_token_var.set(config.remote_token)
        self.server_enabled_var.set(config.remote_server_enabled)
        if config.remote_server_enabled:
            self._start_server()

    def _on_server_save(self) -> None:
        if not self._apply_server_fields_to_config():
            return
        self.server_save_button.state(["disabled"])
        backend = self.app_state.local_backend
        config = self._local_config

        def worker() -> None:
            try:
                backend.save_config(config)
                error = None
            except BackendError as e:
                error = str(e)
            self.after(0, lambda: self._on_server_saved(error))

        threading.Thread(target=worker, daemon=True).start()

    def _on_server_saved(self, error: str | None) -> None:
        self.server_save_button.state(["!disabled"])
        if error:
            messagebox.showerror("Save failed", error)
            return
        self._update_server_status()

    def _apply_server_fields_to_config(self) -> bool:
        if self._local_config is None:
            return False
        try:
            port = int(self.server_port_var.get())
        except ValueError:
            messagebox.showerror("Invalid configuration", "Port must be a number.")
            return False
        self._local_config.remote_server_port = port
        self._local_config.remote_token = self.server_token_var.get()
        return True

    def _on_server_toggle(self) -> None:
        if self.app_state.recording_active:
            self.server_enabled_var.set(not self.server_enabled_var.get())  # revert the click
            messagebox.showerror("Cannot change", "Stop the current recording first.")
            return
        if self.server_enabled_var.get():
            self._start_server()
        else:
            self._stop_server()

    def _start_server(self) -> None:
        if self.app_state.remote_server is not None:
            self._update_server_status()
            return
        if not self._apply_server_fields_to_config():
            self.server_enabled_var.set(False)
            return
        self._local_config.remote_server_enabled = True

        server = RemoteServer(
            self.app_state.local_backend, self._local_config.remote_server_port, self._local_config.remote_token,
        )
        try:
            server.start()
        except OSError as e:
            messagebox.showerror("Could not start server", str(e))
            self.server_enabled_var.set(False)
            return

        self.app_state.remote_server = server
        self.server_enabled_var.set(True)
        self._update_server_status()

        # Persist enabled/port/token so this instance auto-starts serving next launch.
        backend = self.app_state.local_backend
        config = self._local_config
        threading.Thread(target=lambda: backend.save_config(config), daemon=True).start()

    def _stop_server(self) -> None:
        if self.app_state.remote_server is not None:
            self.app_state.remote_server.stop()
            self.app_state.remote_server = None
        self.server_enabled_var.set(False)
        self.server_status_var.set("Stopped.")

        if self._local_config is not None:
            self._local_config.remote_server_enabled = False
            backend = self.app_state.local_backend
            config = self._local_config
            threading.Thread(target=lambda: backend.save_config(config), daemon=True).start()

    def _update_server_status(self) -> None:
        server = self.app_state.remote_server
        if server is None:
            self.server_status_var.set("Stopped.")
            return
        self.server_status_var.set(f"Serving on port {server.port} — {server.client_count} client(s) connected.")
