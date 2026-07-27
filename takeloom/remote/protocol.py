"""Wire format for remote mode: newline-delimited JSON over a persistent TCP
socket, one JSON object per line.

    {"kind": "hello", "token": "...", "client_name": "..."}             client -> server, first line
    {"kind": "hello_ack", "ok": true, "hostname": "...", "token": "..."} server -> client, only on first-time pairing
    {"kind": "hello_ack", "ok": true, "hostname": "..."}                 server -> client, known token
    {"kind": "hello_ack", "ok": false, "error": "..."}                   server -> client, then closes

    {"kind": "request",  "id": 17, "op": "get_config", "args": {}}       client -> server
    {"kind": "response", "id": 17, "ok": true,  "result": {...}}         server -> client
    {"kind": "response", "id": 17, "ok": false, "error": "message"}      server -> client

    {"kind": "event", "event": "recording_status", "data": {...}}       server -> client, unsolicited
    {"kind": "event", "event": "preview_frame", "data": {"jpeg_b64": "..."}}

No TLS. A client with no token (or an unrecognized one) triggers an
interactive pairing request on the server — the local user approves or
denies it, and on approval the server mints a token just for that client
and returns it once in `hello_ack`. Recognized tokens are compared with
`hmac.compare_digest`. This is a LAN-trust feature (like the existing
rsync-based backup_server), not meant for untrusted networks.

`dispatch()` is the server-side op table: every `Backend` method that's safe
to expose directly over the wire. `subscribe_preview`/`unsubscribe_preview`
aren't Backend methods — they manage a per-connection camera-preview
subscription and are handled directly by the server's connection handler.
"""

from __future__ import annotations

from ..backend import Backend, BackendError, StartRecordingRequest
from ..config import StudioConfig


def dispatch(backend: Backend, op: str, args: dict) -> dict:
    """Run one RPC op against a local backend, returning its JSON-able result."""
    if op == "hostname":
        return {"hostname": backend.hostname()}
    if op == "get_config":
        return {"config": backend.get_config().to_dict()}
    if op == "save_config":
        backend.save_config(StudioConfig.from_dict(args["config"]))
        return {}
    if op == "list_audio_devices":
        return {"devices": backend.list_audio_devices()}
    if op == "list_cameras":
        return {"cameras": [list(c) for c in backend.list_cameras()]}
    if op == "refresh_devices":
        backend.refresh_devices()
        return {}
    if op == "list_projects":
        return {"projects": backend.list_projects()}
    if op == "get_setlist":
        return {"setlist": backend.get_setlist(args["project_name"])}
    if op == "save_setlist":
        backend.save_setlist(args["project_name"], args["setlist"])
        return {}
    if op == "create_project":
        return {"name": backend.create_project(args["name"])}
    if op == "add_local_backing_track":
        return backend.add_local_backing_track(args["project_name"], args["source_path"], args.get("track_name"))
    if op == "add_youtube_backing_track":
        return backend.add_youtube_backing_track(args["project_name"], args["url"])
    if op == "query_inspiration_tracks":
        return {"tracks": backend.query_inspiration_tracks(args["project_name"])}
    if op == "start_recording":
        req = StartRecordingRequest(
            project_name=args["project_name"],
            instrument_name=args["instrument_name"],
            track_source=args["track_source"],
            track_index=args.get("track_index"),
            inspiration_info=args.get("inspiration_info"),
        )
        backend.start_recording(req)
        return {}
    if op == "unpause_recording":
        backend.unpause_recording()
        return {}
    if op == "stop_recording":
        backend.stop_recording()
        return {}
    if op == "is_recording":
        return {"recording": backend.is_recording()}
    if op == "adjust_backing_volume":
        backend.adjust_backing_volume(args["delta"])
        return {}
    if op == "adjust_takes_volume":
        backend.adjust_takes_volume(args["delta"])
        return {}
    raise BackendError(f"Unknown operation: {op}")
