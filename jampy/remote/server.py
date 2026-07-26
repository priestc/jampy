"""RemoteServer: threaded TCP server exposing one LocalBackend to remote
jampy clients.

Each connection gets its own handler thread. `LocalBackend` events
(recording_status, preview_paused, preview_resumed) are broadcast to every
connected client, not just the one that triggered them, so a second viewer
stays in sync. Camera-preview frames are opt-in per connection via
subscribe_preview/unsubscribe_preview, since they're the one bandwidth-heavy
part of the protocol.
"""

from __future__ import annotations

import base64
import hmac
import json
import socketserver
import threading

from ..backend import Backend, BackendError
from .protocol import dispatch


class RemoteServer:
    def __init__(self, backend: Backend, port: int, token: str) -> None:
        self.backend = backend
        self.port = port
        self.token = token
        self._tcp_server: _ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._connections_lock = threading.Lock()
        self._connections: list[_ClientHandler] = []
        backend.on_event(self._on_backend_event)

    @property
    def is_running(self) -> bool:
        return self._tcp_server is not None

    @property
    def client_count(self) -> int:
        with self._connections_lock:
            return len(self._connections)

    def start(self) -> None:
        if self._tcp_server is not None:
            return

        def handler_factory(*args, **kwargs):
            return _ClientHandler(*args, server_owner=self, **kwargs)

        self._tcp_server = _ThreadingTCPServer(("0.0.0.0", self.port), handler_factory)
        self._thread = threading.Thread(target=self._tcp_server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._tcp_server is not None:
            self._tcp_server.shutdown()
            self._tcp_server.server_close()
            self._tcp_server = None
        self._thread = None
        with self._connections_lock:
            conns = list(self._connections)
            self._connections.clear()
        for conn in conns:
            conn.close()

    # --- connection registry, used by _ClientHandler ---

    def _register(self, handler: "_ClientHandler") -> None:
        with self._connections_lock:
            self._connections.append(handler)

    def _unregister(self, handler: "_ClientHandler") -> None:
        with self._connections_lock:
            if handler in self._connections:
                self._connections.remove(handler)

    def _broadcast(self, event: str, data: dict) -> None:
        with self._connections_lock:
            conns = list(self._connections)
        for conn in conns:
            conn.send_event(event, data)

    def _on_backend_event(self, event: str, data: dict) -> None:
        self._broadcast(event, data)


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class _ClientHandler(socketserver.StreamRequestHandler):
    def __init__(self, *args, server_owner: RemoteServer, **kwargs) -> None:
        self._owner = server_owner
        self._write_lock = threading.Lock()
        self._preview_sub = None
        super().__init__(*args, **kwargs)  # runs handle() synchronously

    def handle(self) -> None:
        try:
            hello_line = self.rfile.readline()
            if not hello_line:
                return
            try:
                hello = json.loads(hello_line.decode("utf-8"))
            except ValueError:
                return

            token = hello.get("token", "")
            if not hmac.compare_digest(token, self._owner.token):
                self._write({"kind": "hello_ack", "ok": False, "error": "Invalid token."})
                return
            self._write({
                "kind": "hello_ack", "ok": True, "hostname": self._owner.backend.hostname(),
            })

            self._owner._register(self)
            try:
                while True:
                    line = self.rfile.readline()
                    if not line:
                        break
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if msg.get("kind") == "request":
                        self._handle_request(msg)
            finally:
                if self._preview_sub is not None:
                    self._preview_sub.close()
                    self._preview_sub = None
                self._owner._unregister(self)
        except (OSError, ConnectionError):
            pass

    def _handle_request(self, msg: dict) -> None:
        req_id = msg.get("id")
        op = msg.get("op")
        args = msg.get("args") or {}
        try:
            if op == "subscribe_preview":
                if self._preview_sub is None:
                    self._preview_sub = self._owner.backend.open_camera_preview(self._on_preview_frame)
                result = {}
            elif op == "unsubscribe_preview":
                if self._preview_sub is not None:
                    self._preview_sub.close()
                    self._preview_sub = None
                result = {}
            else:
                result = dispatch(self._owner.backend, op, args)
            self._write({"kind": "response", "id": req_id, "ok": True, "result": result})
        except BackendError as e:
            self._write({"kind": "response", "id": req_id, "ok": False, "error": str(e)})
        except Exception as e:
            self._write({"kind": "response", "id": req_id, "ok": False, "error": f"Internal error: {e}"})

    def _on_preview_frame(self, jpeg: bytes) -> None:
        self.send_event("preview_frame", {"jpeg_b64": base64.b64encode(jpeg).decode("ascii")})

    def send_event(self, event: str, data: dict) -> None:
        try:
            self._write({"kind": "event", "event": event, "data": data})
        except OSError:
            pass

    def _write(self, obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode("utf-8")
        with self._write_lock:
            self.wfile.write(data)

    def close(self) -> None:
        try:
            self.connection.close()
        except OSError:
            pass
