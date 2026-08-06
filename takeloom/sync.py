"""Remote backup & collaboration sync via rsync over SSH."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import click


def _remote_path(remote: str, project_name: str) -> str:
    """Join remote base and project name, handling host:path format.

    Backslash-escapes spaces in the path portion (never the host). rsync
    forwards this argument to the remote shell over ssh, and the rsync
    client bundled with macOS (an openrsync build that reports itself as
    "rsync version 2.6.9 compatible" and doesn't support --protect-args)
    does not shell-quote it — an unescaped space gets split into
    separate arguments on the remote end, silently truncating the
    destination path (e.g. ".../Takeloom Studio Vault/sessions/..."
    becomes just ".../Takeloom", with everything after the space lost —
    this is exactly what happened before this fix, more than once)."""
    if ":" in remote:
        host, path = remote.split(":", 1)
        joined = os.path.join(path, project_name)
        return f"{host}:{joined.replace(' ', chr(92) + ' ')}"
    return os.path.join(remote, project_name)


def _ensure_remote_dir(remote: str, project_name: str) -> None:
    """mkdir -p the destination directory on the remote host before an
    upload. This rsync client doesn't support --mkpath and won't create
    more than one missing intermediate directory level on its own, so a
    nested vault destination (e.g. "sessions/Album1/2026-.../") fails
    outright unless something else creates "sessions/Album1/" first.
    Best-effort: if this fails, the rsync call right after it will
    surface the real error."""
    if ":" not in remote:
        return
    host, path = remote.split(":", 1)
    remote_dir = os.path.join(path, project_name)
    try:
        subprocess.run(
            ["ssh", host, f"mkdir -p {shlex.quote(remote_dir)}"],
            capture_output=True, timeout=15,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass


def sync_down(project_path: Path, remote: str) -> None:
    """Download updates from the remote backup server."""
    project_name = project_path.name
    source = _remote_path(remote, project_name) + "/"
    dest = f"{project_path}/"
    cmd = ["rsync", "-avz", "--checksum", source, dest]
    click.echo(f"$ {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd)
        if result.returncode != 0:
            click.echo(f"Warning: sync down failed (exit {result.returncode})")
        else:
            click.echo("Sync down complete.")
    except FileNotFoundError:
        click.echo("Warning: rsync not found. Skipping sync.")
    except Exception as e:
        click.echo(f"Warning: sync down error: {e}")


def sync_up(project_path: Path, remote: str) -> None:
    """Upload project to the remote backup server."""
    project_name = project_path.name
    source = f"{project_path}/"
    _ensure_remote_dir(remote, project_name)
    dest = _remote_path(remote, project_name) + "/"
    cmd = ["rsync", "-avz", "--checksum", source, dest]
    click.echo(f"$ {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd)
        if result.returncode != 0:
            click.echo(f"Warning: sync up failed (exit {result.returncode})")
        else:
            click.echo("Sync up complete.")
    except FileNotFoundError:
        click.echo("Warning: rsync not found. Skipping sync.")
    except Exception as e:
        click.echo(f"Warning: sync up error: {e}")


def sync_vault_session_up(local_dir: Path, vault_relative: str, remote: str) -> bool:
    """Upload one Studio Session Vault session's directory to the remote
    backup server, preserving its path relative to the vault root (e.g.
    "My Album/2026-08-05_14-30-00_bass") rather than just its basename —
    unlike sync_up/sync_down, which are keyed by a project's own bare
    directory name. Returns whether it succeeded rather than only
    click.echoing a warning: the vault's "remote" mode (see
    takeloom/vault.py) needs a real success signal to decide whether it's
    safe to delete the local copy afterward, and this is called from
    backend.py's background processing thread — not a CLI context — so it
    reports through its own return value instead of stdout."""
    _ensure_remote_dir(remote, vault_relative)
    dest = _remote_path(remote, vault_relative) + "/"
    cmd = ["rsync", "-avz", "--checksum", f"{local_dir}/", dest]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def sync_vault_file_down(remote: str, vault_relative: str, local_path: Path) -> bool:
    """Download one file from the remote backup server into the vault,
    at `vault_relative`'s path relative to the vault root (e.g.
    "backing_tracks/a1b2c3d4_song1.mp3") — used by
    vault.ensure_setlist_files_local to pull back a file "remote" vault
    mode already pruned locally. Returns whether it succeeded (and the
    file now actually exists locally), same success-signal reasoning as
    sync_vault_session_up."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    source = _remote_path(remote, vault_relative)
    cmd = ["rsync", "-avz", "--checksum", source, str(local_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0 and local_path.exists()
    except (FileNotFoundError, OSError):
        return False
