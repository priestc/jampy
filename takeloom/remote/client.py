"""RemoteClient: socket I/O and RPC correlation for talking to a RemoteServer.

One background thread reads lines off the socket for the lifetime of the
connection, resolving pending `call()`s (matched by request id) and
dispatching server-pushed events to a callback. `call()` is blocking and
must always be invoked from a worker thread, never the Tk main thread —
callers marshal results back via `widget.after(0, ...)`, same as the rest
of the UI's existing async patterns.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Callable

from ..backend import BackendError

DEFAULT_TIMEOUT = 5.0


class RemoteClient:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        on_event: Callable[[str, dict], None] | None = None,
        on_disconnect: Callable[[str], None] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._on_event = on_event
        self._on_disconnect = on_disconnect

        self._sock: socket.socket | None = None
        self._file = None
        self._write_lock = threading.Lock()

        self._id_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, threading.Event] = {}
        self._results: dict[int, dict] = {}

        self._reader_thread: threading.Thread | None = None
        self._intentional_close = False

        self.hostname: str = host
        self.issued_token: str | None = None

    def connect(self, timeout: float = DEFAULT_TIMEOUT) -> str:
        """Connect, authenticate, and start the reader thread. Returns the
        server's reported hostname. Raises BackendError on any failure."""
        try:
            sock = socket.create_connection((self._host, self._port), timeout=timeout)
        except OSError as e:
            raise BackendError(f"Could not connect to {self._host}:{self._port} — {e}") from e
        sock.settimeout(None)
        self._sock = sock
        self._file = sock.makefile("r", encoding="utf-8", newline="\n")

        try:
            self._write_line({"kind": "hello", "token": self._token, "client_name": socket.gethostname()})
            line = self._file.readline()
        except OSError as e:
            self.close()
            raise BackendError(f"Connection error during handshake: {e}") from e

        if not line:
            self.close()
            raise BackendError("Connection closed during handshake.")
        try:
            ack = json.loads(line)
        except ValueError as e:
            self.close()
            raise BackendError(f"Malformed handshake response: {e}") from e

        if not ack.get("ok"):
            self.close()
            raise BackendError(ack.get("error") or "Authentication failed.")

        self.hostname = ack.get("hostname") or self._host
        self.issued_token = ack.get("token")
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        return self.hostname

    def call(self, op: str, args: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
        if self._sock is None:
            raise BackendError("Not connected.")

        with self._id_lock:
            req_id = self._next_id
            self._next_id += 1

        event = threading.Event()
        self._pending[req_id] = event
        try:
            self._write_line({"kind": "request", "id": req_id, "op": op, "args": args})
        except OSError as e:
            self._pending.pop(req_id, None)
            raise BackendError(f"Connection error: {e}") from e

        if not event.wait(timeout):
            self._pending.pop(req_id, None)
            raise BackendError(f"Timed out waiting for '{op}' response.")

        response = self._results.pop(req_id, None)
        if response is None:
            raise BackendError("Connection closed before a response arrived.")
        if not response.get("ok"):
            raise BackendError(response.get("error") or "Remote error.")
        return response.get("result") or {}

    def close(self) -> None:
        self._intentional_close = True
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # --- internals ---

    def _write_line(self, obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode("utf-8")
        with self._write_lock:
            if self._sock is None:
                raise BackendError("Not connected.")
            self._sock.sendall(data)

    def _read_loop(self) -> None:
        try:
            while True:
                line = self._file.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                kind = msg.get("kind")
                if kind == "response":
                    req_id = msg.get("id")
                    self._results[req_id] = msg
                    ev = self._pending.pop(req_id, None)
                    if ev is not None:
                        ev.set()
                elif kind == "event" and self._on_event is not None:
                    try:
                        self._on_event(msg.get("event", ""), msg.get("data") or {})
                    except Exception:
                        pass
        except OSError:
            pass
        finally:
            self._fail_all_pending()
            intentional = self._intentional_close
            self._sock = None
            if not intentional and self._on_disconnect is not None:
                try:
                    self._on_disconnect("Connection to remote lost.")
                except Exception:
                    pass

    def _fail_all_pending(self) -> None:
        for req_id, ev in list(self._pending.items()):
            self._results[req_id] = {"ok": False, "error": "Connection closed."}
            ev.set()
        self._pending.clear()
