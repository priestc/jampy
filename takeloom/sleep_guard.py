"""Keeps the *UI's own* display awake while a recording (or video check) is
in progress, so a long take doesn't get cut short by the screen locking or
the system suspending mid-recording.

Driven entirely by the Tk UI's `AppState.recording_active` — see its
setter. Deliberately not wired up anywhere headless (`takeloom server`,
the CLI's `start-session`/`inspiration` commands): a machine with nobody
watching its screen has no reason to hold its own screensaver/sleep off
just because a recording happens to be running on it — only a UI actually
being looked at does. That includes a Remote client just watching a
session recording on another machine, not only a local one — see
AppState.recording_active's docstring and record.py's
_update_recording_active, which both apply regardless of whether
app_state.backend is local or remote.

On Linux, screen-blanking needs holding off through more than one
mechanism, since no single one is reliably answered everywhere:
systemd-inhibit's idle/sleep/lid locks stop logind from actually
suspending the system, but most desktop environments (GNOME, KDE, XFCE)
run their own screen-blanking/lock timer entirely separate from logind's
idle handling — systemd-inhibit alone does nothing to stop that, which is
why the screensaver could still kick in with it active. xdg-screensaver
(part of xdg-utils, near-universal on any Linux desktop) targets that
layer, via aging X11-era screensaver protocols — see
_set_linux_screensaver_suspended. But xdg-screensaver's suspend call is a
no-op on plenty of real setups too: any Wayland compositor (and some X11
desktops with no legacy xscreensaver/gnome-screensaver process actually
running) never answers it, and it fails silently. org.freedesktop.
ScreenSaver's Inhibit/UnInhibit D-Bus calls — the same interface browsers
use to keep the screen on during video playback — is what actually gets
answered on GNOME Shell, KDE Plasma, and most other modern desktops on
both X11 and Wayland, so it's held alongside xdg-screensaver rather than
instead of it: see _set_linux_dbus_inhibited.
"""

from __future__ import annotations

import atexit
import ctypes
import os
import subprocess
import sys

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002

_process: subprocess.Popen | None = None
_screensaver_suspended = False
_dbus_inhibit_cookie: str | None = None


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


def _set_linux_dbus_inhibited(inhibited: bool) -> None:
    """Best-effort org.freedesktop.ScreenSaver Inhibit/UnInhibit — the same
    D-Bus interface browsers use to keep the screen on during video
    playback. Complements _set_linux_screensaver_suspended rather than
    replacing it: xdg-screensaver's aging X11-era protocols go unanswered
    on plenty of real setups (any Wayland compositor, or an X11 desktop
    with no legacy xscreensaver/gnome-screensaver process running) and
    fail silently, whereas this interface is what GNOME Shell, KDE Plasma,
    and most other modern desktops actually implement natively on both
    X11 and Wayland. Inhibit returns a cookie that UnInhibit must be
    called back with, unlike xdg-screensaver's stateless suspend/resume —
    hence _dbus_inhibit_cookie. Silently does nothing if dbus-send isn't
    installed or nothing answers on the session bus (e.g. a headless
    server)."""
    global _dbus_inhibit_cookie
    if inhibited:
        if _dbus_inhibit_cookie is not None:
            return
        try:
            result = subprocess.run(
                [
                    "dbus-send", "--session", "--print-reply",
                    "--dest=org.freedesktop.ScreenSaver",
                    "/org/freedesktop/ScreenSaver",
                    "org.freedesktop.ScreenSaver.Inhibit",
                    "string:takeloom", "string:Recording in progress",
                ],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        for token in result.stdout.split():
            if token.isdigit():
                _dbus_inhibit_cookie = token
                break
    else:
        if _dbus_inhibit_cookie is None:
            return
        try:
            subprocess.run(
                [
                    "dbus-send", "--session",
                    "--dest=org.freedesktop.ScreenSaver",
                    "/org/freedesktop/ScreenSaver",
                    "org.freedesktop.ScreenSaver.UnInhibit",
                    f"uint32:{_dbus_inhibit_cookie}",
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        _dbus_inhibit_cookie = None


def set_active(active: bool) -> None:
    """Prevent (True) or re-allow (False) display/system sleep. Idempotent."""
    global _process

    if sys.platform.startswith("win"):
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED if active else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]
        return

    if sys.platform.startswith("linux"):
        _set_linux_screensaver_suspended(active)
        _set_linux_dbus_inhibited(active)

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
