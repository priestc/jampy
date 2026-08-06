"""Remote backup & collaboration sync via rsync over SSH."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import click


def _remote_path(remote: str, project_name: str) -> str:
    """Join remote base and project name, handling host:path format."""
    if ":" in remote:
        host, path = remote.split(":", 1)
        return f"{host}:{os.path.join(path, project_name)}"
    return os.path.join(remote, project_name)


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
    dest = _remote_path(remote, vault_relative) + "/"
    cmd = ["rsync", "-avz", "--checksum", f"{local_dir}/", dest]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0
    except (FileNotFoundError, OSError):
        return False
