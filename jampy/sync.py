"""Remote backup & collaboration sync via rsync over SSH."""

from __future__ import annotations

import subprocess
from pathlib import Path

import click


def sync_down(project_path: Path, remote: str) -> None:
    """Download updates from the remote backup server."""
    project_name = project_path.name
    source = f"{remote}/{project_name}/"
    click.echo(f"Syncing down from {source} ...")
    try:
        result = subprocess.run(
            ["rsync", "-avz", "--checksum", source, f"{project_path}/"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            if result.stdout.strip():
                click.echo(result.stdout)
            click.echo("Sync down complete.")
        else:
            click.echo(f"Warning: sync down failed (exit {result.returncode})")
            if result.stderr.strip():
                click.echo(result.stderr)
    except FileNotFoundError:
        click.echo("Warning: rsync not found. Skipping sync.")
    except Exception as e:
        click.echo(f"Warning: sync down error: {e}")


def sync_up(project_path: Path, remote: str) -> None:
    """Upload project to the remote backup server."""
    project_name = project_path.name
    dest = f"{remote}/{project_name}/"
    click.echo(f"Syncing up to {dest} ...")
    try:
        result = subprocess.run(
            ["rsync", "-avz", "--checksum", f"{project_path}/", dest],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            if result.stdout.strip():
                click.echo(result.stdout)
            click.echo("Sync up complete.")
        else:
            click.echo(f"Warning: sync up failed (exit {result.returncode})")
            if result.stderr.strip():
                click.echo(result.stderr)
    except FileNotFoundError:
        click.echo("Warning: rsync not found. Skipping sync.")
    except Exception as e:
        click.echo(f"Warning: sync up error: {e}")
