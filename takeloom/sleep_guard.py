"""Keeps the display awake while a recording (or video check) is in
progress, so a long take doesn't get cut short by the screen locking or the
system suspending mid-recording.

The Tk UI drives this via `AppState.recording_active` — see its setter.
CLI/headless entry points that don't go through AppState (`start-session`,
`takeloom server`) instead call `track_backend()` directly. Also what keeps
a Remote client's own display awake while it's just watching a session
recording on another machine — see AppState.recording_active's docstring
and record.py's _update_recording_active, which both apply regardless of
whether app_state.backend is local or remote.

On Linux, two independent things need holding off, not one:
systemd-inhibit's idle/sleep/lid locks stop logind from actually
suspending the system, but most desktop environments (GNOME, KDE, XFCE)
run their own screen-blanking/lock timer entirely separate from logind's
idle handling — systemd-inhibit alone does nothing to stop that, which is
why the screensaver could still kick in with it active. xdg-screensaver
(part of xdg-utils, near-universal on any Linux desktop) is what's used
for that layer instead — see _set_linux_screensaver_suspended.
"""

from __future__ import annotations

import atexit
import ctypes
import os
import subprocess
import sys

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Backend

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002

_process: subprocess.Popen | None = None
_screensaver_suspended = False


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


def _screensaver_id() -> str:
    """A stand-in "window id" for xdg-screensaver's suspend/resume — it
    only uses this to name a state file pairing a resume with its matching
    suspend, not to look up a real window (other headless/no-window
    callers, e.g. mpv, rely on the same thing), which matters since
    sleep_guard has no window of its own to hand it and runs in headless
    contexts too."""
    return f"takeloom-{os.getpid()}"


def _set_linux_screensaver_suspended(suspended: bool) -> None:
    """Best-effort toggle of the desktop's own screensaver/screen-lock via
    xdg-screensaver, independent of _spawn_inhibitor's systemd-inhibit
    process (see module docstring for why both are needed). Unlike that
    subprocess-held lock, xdg-screensaver's suspend/resume are each a
    plain one-shot call — nothing needs to be kept running between them.
    Silently does nothing if xdg-screensaver isn't installed or there's no
    desktop session (e.g. a headless server) to talk to."""
    global _screensaver_suspended
    if suspended == _screensaver_suspended:
        return
    try:
        subprocess.run(
            ["xdg-screensaver", "suspend" if suspended else "resume", _screensaver_id()],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    _screensaver_suspended = suspended


def set_active(active: bool) -> None:
    """Prevent (True) or re-allow (False) display/system sleep. Idempotent."""
    global _process

    if sys.platform.startswith("win"):
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED if active else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]
        return

    if sys.platform.startswith("linux"):
        _set_linux_screensaver_suspended(active)

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
