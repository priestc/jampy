"""Audio device name resolution."""

from __future__ import annotations


def resolve_device(sd: object, device_name: str, kind: str) -> int | None:
    """Resolve a device name to its index. Returns None if not found."""
    if not device_name:
        return None
    devices = sd.query_devices()  # type: ignore[union-attr]
    for i, d in enumerate(devices):
        if d["name"] == device_name:
            return i
    # Partial match fallback
    for i, d in enumerate(devices):
        if device_name.lower() in d["name"].lower():
            return i
    return None
