"""Keeps the display awake while a recording (or video check) is in
progress, so a long take doesn't get cut short by the screen locking or the
system suspending mid-recording.

The Tk UI drives this via `AppState.recording_active` — see its setter.
CLI/headless entry points that don't go through AppState (`start-session`,
`takeloom server`) instead call `track_backend()` directly.
"""

from __future__ import annotations

import atexit
import ctypes
import subprocess
import sys

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Backend

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002

_process: subprocess.Popen | None = None


def _spawn_inhibitor() -> subprocess.Popen | None:
    if sys.platform == "darwin":
        return subprocess.Popen(["caffeinate", "-d", "-i"])
    if sys.platform.startswith("linux"):
        return subprocess.Popen(
            [
                "systemd-inhibit",
                "--what=idle:sleep:handle-lid-switch",
                "--who=takeloom",
                "--why=Recording in progress",
                "sleep", "infinity",
            ]
        )
    return None


def set_active(active: bool) -> None:
    """Prevent (True) or re-allow (False) display/system sleep. Idempotent."""
    global _process

    if sys.platform.startswith("win"):
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED if active else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]
        return

    if active:
        if _process is not None and _process.poll() is None:
            return  # already running
        try:
            _process = _spawn_inhibitor()
        except OSError:
            _process = None
    elif _process is not None:
        _process.terminate()
        _process = None


@atexit.register
def _cleanup() -> None:
    set_active(False)


def track_backend(backend: "Backend") -> None:
    """Subscribe to `backend`'s events and keep the display awake for as
    long as a take or video check is in progress on it — same "waiting" (armed/
    counting in) or "recording" phase, or an in-progress video check, that
    `ui/record.py`'s `_update_recording_active` treats as active. For
    CLI/headless callers (`start-session`, `takeloom server`) that have no
    AppState to drive `set_active()` for them.
    """
    phases = {"recording_status": None, "video_check_status": None}

    def _on_event(event: str, data: dict) -> None:
        phase = data.get("phase")
        if phase is None or event not in phases:
            return
        phases[event] = phase
        set_active(
            phases["recording_status"] in ("waiting", "recording")
            or phases["video_check_status"] == "recording"
        )

    backend.on_event(_on_event)
