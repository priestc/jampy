"""Remote mode: control another takeloom instance's hardware/config over the LAN.

- `protocol`: the newline-delimited-JSON wire format and server-side op dispatch.
- `server`: `RemoteServer`, a threaded TCP server exposing a `LocalBackend`.
- `client`: `RemoteClient`, the socket I/O + RPC correlation layer.
- `backend`: `RemoteBackend`, adapts `takeloom.backend.Backend` over a `RemoteClient`.
"""
