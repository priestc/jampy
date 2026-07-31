"""Keeps the display awake while a recording (or video check) is in
progress, so a long take doesn't get cut short by the screen locking or the
system suspending mid-recording.

Driven by `AppState.recording_active` — see its setter.
"""

from __future__ import annotations

import atexit
import ctypes
import subprocess
import sys

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
