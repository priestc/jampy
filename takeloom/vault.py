"""Studio Session Vault: centralized storage for continuous session
recordings (session.flac, session_video.mp4, session_log.json, ...),
kept separate from a project's own folder (setlist.json, backing_tracks/,
completed_takes/ — the actual song-level outputs a project is really made
of). See StudioConfig.session_vault_mode/session_vault_path.

A session's vault directory is always written locally first — a live
recording needs a fast local disk, not a network share — then, depending
on session_vault_mode, optionally pushed to the remote backup server
after the session's takes have already been spliced out into their
project (see backend.py's _process_session, the only other caller of
sync_and_maybe_prune besides migrate_projects_to_vault below).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from .config import StudioConfig
from .project import Project

LogFn = Callable[[str], None]


def vault_root(config: StudioConfig) -> Path:
    return Path(config.session_vault_path)


def vault_session_dir(config: StudioConfig, project_name: str, session_name: str) -> Path:
    return vault_root(config) / project_name / session_name


def sync_and_maybe_prune(config: StudioConfig, session_dir: Path, log: LogFn | None = None) -> None:
    """Best-effort: push `session_dir` (already inside the vault) to the
    remote backup server if session_vault_mode is "remote" or "both", and
    — only for "remote" mode, and only once that push is verified
    successful — delete the local copy, so the remote ends up as the sole
    copy. Does nothing for "local" mode, or if no backup_server is
    configured (there's nowhere to push to). `log`, if given, reports
    progress — an emitted event from a live session's background
    processing thread, or plain stdout from the migration CLI command;
    either way this function never raises, matching this codebase's other
    best-effort sync/hardware paths."""
    log = log or (lambda msg: None)
    mode = config.session_vault_mode
    if mode not in ("remote", "both"):
        return
    if not config.backup_server:
        log(f"Vault sync skipped for {session_dir.name}: no backup server configured.")
        return

    from .sync import sync_vault_session_up
    relative = session_dir.relative_to(vault_root(config))
    log(f"Syncing '{relative}' to {config.backup_server}...")
    ok = sync_vault_session_up(session_dir, str(relative), config.backup_server)
    if not ok:
        log(f"Vault sync failed for '{relative}' — kept locally.")
        return
    log(f"Synced '{relative}'.")
    if mode == "remote":
        shutil.rmtree(session_dir, ignore_errors=True)
        log(f"Removed local copy of '{relative}' (remote-only vault mode).")


def _verify_copy(source: Path, dest: Path) -> bool:
    """Every file under `source` exists at the same relative path under
    `dest` with a matching size — good enough confidence to remove the
    original without a byte-for-byte checksum pass over what can be many
    GB of session video."""
    for src_file in source.rglob("*"):
        if src_file.is_dir():
            continue
        dest_file = dest / src_file.relative_to(source)
        if not dest_file.exists() or dest_file.stat().st_size != src_file.stat().st_size:
            return False
    return True


def migrate_projects_to_vault(config: StudioConfig, log: LogFn) -> tuple[int, int]:
    """One-time migration: move every existing project's sessions/
    subfolder contents into the vault. Each session is copied, verified
    intact (_verify_copy), then removed from the project's old sessions/
    folder — a clean cutover, not a permanent dual-write. Applies the same
    sync_and_maybe_prune remote behavior a live session's ending already
    does, so a migrated session ends up in exactly the state it would
    have if it had been recorded under the new vault paradigm from the
    start.

    Safe to re-run: a session already present at its vault destination is
    skipped rather than re-copied (idempotent, so an interrupted run can
    just be started again), and a session whose copy fails verification
    is left in place at the old location (with whatever partial copy
    landed in the vault kept too, for inspection) rather than risking data
    loss by deleting an unverified original.

    Returns (sessions_migrated, projects_touched)."""
    projects_dir = Path(config.projects_dir)
    migrated = 0
    projects_touched = 0
    for project_path in Project.list_projects(projects_dir):
        project = Project.open(project_path)
        if not project.sessions_dir.exists():
            continue
        session_dirs = [d for d in sorted(project.sessions_dir.iterdir()) if d.is_dir()]
        if not session_dirs:
            continue
        projects_touched += 1
        for session_dir in session_dirs:
            dest = vault_session_dir(config, project.name, session_dir.name)
            if dest.exists():
                log(f"Skipping '{project.name}/{session_dir.name}' — already in the vault.")
                continue
            log(f"Moving '{project.name}/{session_dir.name}' -> {dest}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(session_dir, dest)
            if _verify_copy(session_dir, dest):
                shutil.rmtree(session_dir)
                migrated += 1
                sync_and_maybe_prune(config, dest, log=log)
            else:
                log(
                    f"WARNING: verification failed for '{project.name}/{session_dir.name}' — "
                    "original left in place; the (possibly incomplete) vault copy was kept for inspection."
                )
    return migrated, projects_touched
