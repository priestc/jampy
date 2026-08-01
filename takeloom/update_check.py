"""Checks whether a newer commit of takeloom is available on GitHub and, if
so, reinstalls via pipx and restarts the process so the new code takes
effect immediately.

Only meaningful for a non-editable, `pipx install git+https://...` install
(the laptop, per CLAUDE.md's Deployment section) — the Mac Mini's editable
install always reflects the current working copy already, so this is a
no-op there. Best-effort throughout: any failure (no network, pipx missing,
GitHub unreachable, etc.) is logged and swallowed rather than blocking
startup.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import metadata

_REPO_URL = "https://github.com/priestc/takeloom.git"
_GIT_TIMEOUT = 5.0
_INSTALL_TIMEOUT = 120.0


def _installed_commit() -> str | None:
    """The commit this install was built from, or None if that can't be
    determined — including the editable-install case, which has no
    meaningful "installed commit" to compare (see module docstring)."""
    try:
        direct_url = metadata.distribution("takeloom").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not direct_url:
        return None
    info = json.loads(direct_url)
    if info.get("dir_info", {}).get("editable"):
        return None
    vcs_info = info.get("vcs_info") or {}
    return vcs_info.get("commit_id")


def _latest_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "ls-remote", _REPO_URL, "main"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split()[0]


def check_and_restart(log=print) -> None:
    """If GitHub's main branch is ahead of the installed commit, reinstall
    and re-exec this process in place. Returns normally (does nothing) if
    already current, not applicable (editable install), or the check/update
    itself fails for any reason."""
    installed = _installed_commit()
    if installed is None:
        return
    latest = _latest_commit()
    if latest is None or latest == installed:
        return

    log(f"Update available ({installed[:8]} -> {latest[:8]}) — installing...")
    try:
        result = subprocess.run(
            ["pipx", "install", "--force", f"git+{_REPO_URL}"],
            capture_output=True, text=True, timeout=_INSTALL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"Update check: install failed ({e}), continuing with current version.")
        return
    if result.returncode != 0:
        log(f"Update check: install failed, continuing with current version.\n{result.stderr}")
        return

    log("Updated — restarting...")
    os.execv(sys.executable, [sys.executable, "-m", "takeloom"] + sys.argv[1:])
