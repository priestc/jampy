"""Remote tab: connect this jampy instance to another one's hardware/config,
and/or turn this instance's own remote server on/off so others can connect
to it.

The "Connect to a Remote Instance" section swaps `app_state.backend` to a
`RemoteBackend`, which every other tab is already wired to use. The "Remote
Server" section is deliberately independent of that — it always reads/writes
`app_state.local_backend` (this machine's own config/hardware), since
whether *this* machine offers itself as a server has nothing to do with
whatever remote you might currently be connected to as a client.

Auth is a pairing-request flow, not a shared secret: connecting to a remote
for the first time sends no token, which prompts the *other* machine's user
to approve or deny the connection (see `_AuthorizeDialog`, and
`RemoteServer.request_authorization` in ../remote/server.py). On approval a
token is minted for that client and stored under a `KnownRemote` entry here;
subsequent connections reuse it silently. One `KnownRemote` can be flagged
"always connect" for startup auto-connect (see app.py's `run()`).
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..backend import BackendError
from ..config import AuthorizedClient, KnownRemote, StudioConfig
from ..remote.backend import RemoteBackend
from ..remote.client import RemoteClient
from ..remote.server import RemoteServer
from .app_state import AppState


def connect_async(widget, app_state: AppState, host: str, port: int, token: str, on_done=None, on_error=None) -> None:
    """Connect to a remote jampy instance and swap it into app_state.backend.
    Runs on a background thread; safe to call from the Tk main thread. Shared
    by the Remote tab's Connect button and `jampy ui --remote=IP`.

    `on_done`, if given, is called with the connected `RemoteClient` — check
    `client.issued_token` to see whether this was a first-time pairing that
    minted a new token."""

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
                on_done(client)

        widget.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


def remember_remote_token(app_state: AppState, host: str, port: int, client: RemoteClient) -> None:
    """After a successful connect, persist a newly issued token (if any) onto
    the matching KnownRemote entry in local config, creating one if this was
    a first-time pairing initiated some other way (e.g. `--remote=IP`)."""
    if not client.issued_token:
        return
    backend = app_state.local_backend
    try:
        config = backend.get_config()
    except BackendError:
        return
    remote = next((r for r in config.known_remotes if r.host == host and r.port == port), None)
    if remote is None:
        remote = KnownRemote(host=host, port=port)
        config.known_remotes.append(remote)
    remote.token = client.issued_token
    if not remote.label:
        remote.label = client.hostname
    try:
        backend.save_config(config)
    except BackendError:
        pass


def _handle_disconnect(app_state: AppState, reason: str) -> None:
    if not app_state.backend.is_remote():
        return  # already reset by an explicit Disconnect click
    app_state.set_backend(app_state.local_backend)
    messagebox.showwarning("Disconnected", reason)


class _AddRemoteDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, on_add) -> None:
        super().__init__(master)
        self.title("Add Remote")
        self.resizable(False, False)
        self.transient(master)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="IP address / host").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.host_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.host_var, width=28).grid(row=0, column=1, sticky="w")

        ttk.Label(frame, text="Port").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        self.port_var = tk.StringVar(value="51823")
        ttk.Entry(frame, textvariable=self.port_var, width=10).grid(row=1, column=1, sticky="w")

        ttk.Label(frame, text="Label (optional)").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        self.label_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.label_var, width=28).grid(row=2, column=1, sticky="w")

        button_row = ttk.Frame(frame)
        button_row.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(button_row, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Add", command=lambda: self._submit(on_add)).pack(side="left")

    def _submit(self, on_add) -> None:
        host = self.host_var.get().strip()
        if not host:
            messagebox.showerror("Cannot add remote", "Enter an IP address or hostname.", parent=self)
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror("Cannot add remote", "Port must be a number.", parent=self)
            return
        on_add(host, port, self.label_var.get().strip())
        self.destroy()


class _AuthorizeDialog(tk.Toplevel):
    """Pops up on the server side when an unrecognized client connects.
    Auto-denies after TIMEOUT_MS if nobody responds."""

    TIMEOUT_MS = 120_000

    def __init__(self, master: tk.Misc, ip: str, client_name: str, on_result) -> None:
        super().__init__(master)
        self.title("Remote connection request")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self._on_result = on_result
        self._done = False

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=f'"{client_name}" ({ip}) wants to connect to this Jam.py instance.',
            wraplength=320, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        button_row = ttk.Frame(frame)
        button_row.pack(anchor="e")
        ttk.Button(button_row, text="Deny", command=self._deny).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Approve", command=self._approve).pack(side="left")

        self.protocol("WM_DELETE_WINDOW", self._deny)
        self._timeout_id = self.after(self.TIMEOUT_MS, self._deny)

    def _approve(self) -> None:
        self._finish(True)

    def _deny(self) -> None:
        self._finish(False)

    def _finish(self, approved: bool) -> None:
        if self._done:
            return
        self._done = True
        self.after_cancel(self._timeout_id)
        self._on_result(approved)
        self.destroy()


class RemoteFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, app_state: AppState) -> None:
        super().__init__(master)
        self.app_state = app_state
        self._local_config: StudioConfig | None = None
        self._remote_items: dict[str, KnownRemote] = {}
        self._client_items: dict[str, AuthorizedClient] = {}
        self._poll_after_id: str | None = None

        self._build_connect_section()
        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=16)
        self._build_server_section()

        self.app_state.add_listener(self._on_app_state_changed)
        self.bind("<Destroy>", self._on_destroy)

        self._refresh_connect_status()
        self._load_local_config()
        self._poll_server_status()

    def _on_destroy(self, _event: object) -> None:
        self.app_state.remove_listener(self._on_app_state_changed)
        if self._poll_after_id is not None:
            self.after_cancel(self._poll_after_id)
            self._poll_after_id = None

    def _on_app_state_changed(self) -> None:
        self._refresh_connect_status()

    def _save_local_config(self) -> None:
        if self._local_config is None:
            return
        try:
            self.app_state.local_backend.save_config(self._local_config)
        except BackendError:
            pass

    # --- connect section ---

    def _build_connect_section(self) -> None:
        frame = ttk.Frame(self)
        frame.pack(fill="x", anchor="w")

        ttk.Label(frame, text="Connect to a Remote Instance", font=("TkDefaultFont", 12, "bold")).pack(
            anchor="w", pady=(0, 8)
        )

        self.remotes_tree = ttk.Treeview(
            frame, columns=("target", "always"), show="tree headings", height=5, selectmode="browse",
        )
        self.remotes_tree.heading("#0", text="Label")
        self.remotes_tree.heading("target", text="Host : Port")
        self.remotes_tree.heading("always", text="Always Connect")
        self.remotes_tree.column("#0", width=160)
        self.remotes_tree.column("target", width=180)
        self.remotes_tree.column("always", width=110, anchor="center")
        self.remotes_tree.pack(fill="x")
        self.remotes_tree.bind("<<TreeviewSelect>>", lambda e: self._refresh_connect_status())

        button_row = ttk.Frame(frame)
        button_row.pack(anchor="w", pady=(8, 0))
        ttk.Button(button_row, text="Add Remote", command=self._on_add_remote).pack(side="left")
        self.connect_button = ttk.Button(button_row, text="Connect", command=self._on_connect)
        self.connect_button.pack(side="left", padx=(8, 0))
        self.disconnect_button = ttk.Button(button_row, text="Disconnect", command=self._on_disconnect_click)
        self.disconnect_button.pack(side="left", padx=(8, 0))
        ttk.Button(button_row, text="Forget", command=self._on_forget_remote).pack(side="left", padx=(8, 0))

        self.always_connect_var = tk.BooleanVar(value=False)
        self.always_connect_check = ttk.Checkbutton(
            frame, text="Always connect to selected remote on startup",
            variable=self.always_connect_var, command=self._on_toggle_always_connect,
        )
        self.always_connect_check.pack(anchor="w", pady=(8, 0))

        self.connect_status_var = tk.StringVar(value="Not connected.")
        ttk.Label(frame, textvariable=self.connect_status_var, foreground="#2a6db0").pack(anchor="w", pady=(8, 0))

    def _selected_remote(self) -> KnownRemote | None:
        sel = self.remotes_tree.selection()
        if not sel:
            return None
        return self._remote_items.get(sel[0])

    def _reselect(self, remote: KnownRemote | None) -> None:
        if remote is None:
            return
        iid = f"{remote.host}:{remote.port}"
        if self.remotes_tree.exists(iid):
            self.remotes_tree.selection_set(iid)

    def _refresh_known_remotes_list(self) -> None:
        previously_selected = self._selected_remote()
        self.remotes_tree.delete(*self.remotes_tree.get_children())
        self._remote_items = {}
        if self._local_config is None:
            self._refresh_connect_status()
            return
        for remote in self._local_config.known_remotes:
            iid = f"{remote.host}:{remote.port}"
            self.remotes_tree.insert(
                "", "end", iid=iid, text=remote.label or "(unpaired)",
                values=(f"{remote.host}:{remote.port}", "✓" if remote.always_connect else ""),
            )
            self._remote_items[iid] = remote
        # Reload of local config replaces every KnownRemote instance, so the
        # previous selection (by object identity) no longer resolves — reselect
        # by host:port instead, so a Connect click doesn't silently lose the
        # row the user had selected (e.g. mid "Always connect" toggle).
        self._reselect(previously_selected)
        self._refresh_connect_status()

    def _refresh_connect_status(self) -> None:
        remote = self._selected_remote()
        if self.app_state.backend.is_remote():
            self.connect_status_var.set(f"Connected: {self.app_state.remote_name}")
            self.connect_button.state(["disabled"])
            self.disconnect_button.state(["!disabled"])
        else:
            self.connect_status_var.set("Not connected.")
            self.disconnect_button.state(["disabled"])
            self.connect_button.state(["!disabled"] if remote is not None else ["disabled"])
        self.always_connect_var.set(bool(remote and remote.always_connect))
        self.always_connect_check.state(["!disabled"] if remote is not None else ["disabled"])

    def _on_add_remote(self) -> None:
        def on_add(host: str, port: int, label: str) -> None:
            if self._local_config is None:
                return
            self._local_config.known_remotes.append(KnownRemote(host=host, port=port, label=label))
            self._save_local_config()
            self._refresh_known_remotes_list()

        _AddRemoteDialog(self, on_add)

    def _on_forget_remote(self) -> None:
        remote = self._selected_remote()
        if remote is None or self._local_config is None:
            return
        if self.app_state.backend.is_remote() and self.app_state.remote_name and remote.label == self.app_state.remote_name:
            if not messagebox.askyesno("Forget remote", "This is the currently connected remote. Forget it anyway?"):
                return
        self._local_config.known_remotes.remove(remote)
        self._save_local_config()
        self._refresh_known_remotes_list()

    def _on_toggle_always_connect(self) -> None:
        remote = self._selected_remote()
        if remote is None or self._local_config is None:
            self.always_connect_var.set(False)
            return
        new_value = self.always_connect_var.get()
        for r in self._local_config.known_remotes:
            r.always_connect = False
        remote.always_connect = new_value
        self._save_local_config()
        self._refresh_known_remotes_list()

    def _on_connect(self) -> None:
        if self.app_state.recording_active:
            messagebox.showerror("Cannot connect", "Stop the current recording first.")
            return
        remote = self._selected_remote()
        if remote is None:
            messagebox.showerror("Cannot connect", "Select a remote to connect to, or add one first.")
            return

        self.connect_button.state(["disabled"])
        if remote.token:
            self.connect_status_var.set(f"Connecting to {remote.host}:{remote.port}...")
        else:
            self.connect_status_var.set(
                f"Connecting to {remote.host}:{remote.port} — waiting for approval on the other end..."
            )

        def on_done(client: RemoteClient) -> None:
            remember_remote_token(self.app_state, remote.host, remote.port, client)
            self._load_local_config()
            self._refresh_connect_status()

        def on_error(error: str) -> None:
            self._refresh_connect_status()
            messagebox.showerror("Connect failed", error)

        connect_async(self, self.app_state, remote.host, remote.port, remote.token, on_done=on_done, on_error=on_error)

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
        ).pack(anchor="w", pady=(0, 8))

        self.server_enabled_var = tk.BooleanVar(value=False)
        self.server_checkbox = ttk.Checkbutton(
            frame, text="Enable remote server", variable=self.server_enabled_var, command=self._on_server_toggle,
        )
        self.server_checkbox.pack(anchor="w", pady=4)

        port_row = ttk.Frame(frame)
        port_row.pack(anchor="w", pady=4)
        ttk.Label(port_row, text="Port").pack(side="left", padx=(0, 8))
        self.server_port_var = tk.StringVar(value="")
        ttk.Entry(port_row, textvariable=self.server_port_var, width=10).pack(side="left")
        self.server_save_button = ttk.Button(port_row, text="Save", command=self._on_server_save)
        self.server_save_button.pack(side="left", padx=(8, 0))

        self.server_status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.server_status_var, foreground="#2a7d2a").pack(anchor="w", pady=(8, 0))

        ttk.Label(frame, text="Authorized clients", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(12, 4))
        self.clients_tree = ttk.Treeview(
            frame, columns=("ip", "last_seen"), show="tree headings", height=4, selectmode="browse",
        )
        self.clients_tree.heading("#0", text="Label")
        self.clients_tree.heading("ip", text="Last IP")
        self.clients_tree.heading("last_seen", text="Last Seen")
        self.clients_tree.column("#0", width=160)
        self.clients_tree.column("ip", width=140)
        self.clients_tree.column("last_seen", width=180)
        self.clients_tree.pack(fill="x")

        ttk.Button(frame, text="Revoke", command=self._on_revoke_client).pack(anchor="w", pady=(6, 0))

    def _request_authorization(self, ip: str, client_name: str) -> bool:
        """Runs on a RemoteServer connection-handler thread — blocks until
        the user responds to a popup, or it times out."""
        result_event = threading.Event()
        result = {"approved": False}

        def show_dialog() -> None:
            def on_result(approved: bool) -> None:
                result["approved"] = approved
                result_event.set()

            _AuthorizeDialog(self, ip, client_name, on_result)

        self.after(0, show_dialog)
        result_event.wait(_AuthorizeDialog.TIMEOUT_MS / 1000 + 5)
        return result["approved"]

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
        self.server_port_var.set(str(config.remote_server_port))
        self.server_enabled_var.set(config.remote_server_enabled)
        self._refresh_known_remotes_list()
        self._refresh_authorized_clients_list()
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
            self.app_state.local_backend, self._local_config.remote_server_port, self._request_authorization,
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

        # Persist enabled/port so this instance auto-starts serving next launch.
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

    def _refresh_authorized_clients_list(self) -> None:
        self.clients_tree.delete(*self.clients_tree.get_children())
        self._client_items = {}
        if self._local_config is None:
            return
        for client in self._local_config.remote_authorized_clients:
            self.clients_tree.insert(
                "", "end", iid=client.token, text=client.label or "(unknown)",
                values=(client.last_ip, client.last_seen_at),
            )
            self._client_items[client.token] = client

    def _on_revoke_client(self) -> None:
        sel = self.clients_tree.selection()
        if not sel or self._local_config is None:
            return
        token = sel[0]
        self._local_config.remote_authorized_clients = [
            c for c in self._local_config.remote_authorized_clients if c.token != token
        ]
        self._save_local_config()
        if self.app_state.remote_server is not None:
            self.app_state.remote_server.disconnect_token(token)
        self._refresh_authorized_clients_list()

    def _poll_server_status(self) -> None:
        self._update_server_status()
        if self.app_state.remote_server is not None:
            self._reload_authorized_clients_from_disk()
        self._poll_after_id = self.after(2000, self._poll_server_status)

    def _reload_authorized_clients_from_disk(self) -> None:
        try:
            config = self.app_state.local_backend.get_config()
        except BackendError:
            return
        if self._local_config is not None:
            self._local_config.remote_authorized_clients = config.remote_authorized_clients
        self._refresh_authorized_clients_list()
