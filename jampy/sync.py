"""Remote backup & collaboration sync via rsync over SSH."""

from __future__ import annotations

import subprocess
from pathlib import Path

import click


def sync_down(project_path: Path, remote: str) -> None:
    """Download updates from the remote backup server."""
    project_name = project_path.name
    source = f"{remote}/{project_name}/"
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
    dest = f"{remote}/{project_name}/"
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
