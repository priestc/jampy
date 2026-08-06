"""Studio Session Vault: centralized, shared storage for everything a
project used to keep to itself — recorded sessions (session.flac,
session_video.mp4, session_log.json, ...), backing tracks, and completed
takes. A project (see project.py) is just a setlist file now; every
project reads and writes into these same three vault subfolders:

    <vault>/
    ├── sessions/<project_name>/<session_name>/...
    ├── backing_tracks/<filename>
    └── completed_takes/<filename>

Sharing backing_tracks/completed_takes across every project is what lets
two different projects reference the same inspiration-server song without
downloading or recording it twice — see inspiration_takes.json below,
the index that makes an already-recorded take on a given song findable
from any project, not just the one that originally recorded it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from .config import StudioConfig
from .project import Project, TakeInfo, TrackEntry
from .utils import atomic_write_text, ensure_dir

LogFn = Callable[[str], None]


def vault_root(config: StudioConfig) -> Path:
    return Path(config.session_vault_path)


def vault_backing_tracks_dir(config: StudioConfig) -> Path:
    return vault_root(config) / "backing_tracks"


def vault_completed_takes_dir(config: StudioConfig) -> Path:
    return vault_root(config) / "completed_takes"


def vault_session_dir(config: StudioConfig, project_name: str, session_name: str) -> Path:
    return vault_root(config) / "sessions" / project_name / session_name


# --- shared inspiration-take index ---
#
# A project's own setlist.json only knows about takes recorded while that
# project's own TrackEntry was loaded. Since backing_tracks/completed_takes
# are shared vault-wide now, this index is what makes a take findable by
# *any* project referencing the same inspiration_track_id — keyed by
# str(track_id) rather than by which project or session happened to record
# it, each entry shaped exactly like an ordinary TrackEntry (name,
# backing_track, duration_seconds, preferred_takes) so it can be treated
# as one wherever a track is needed.

_INDEX_FILENAME = "inspiration_takes.json"


def _index_path(root: Path) -> Path:
    return root / _INDEX_FILENAME


def load_inspiration_index(root: Path) -> dict[str, TrackEntry]:
    """`root` is a vault root (see vault_root()) — taken directly rather
    than a StudioConfig so this is callable from splicer.py, which has a
    bare vault path but no full config object."""
    path = _index_path(root)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {key: TrackEntry.from_dict(entry) for key, entry in data.items()}


def save_inspiration_index(root: Path, index: dict[str, TrackEntry]) -> None:
    ensure_dir(root)
    atomic_write_text(_index_path(root), json.dumps({k: v.to_dict() for k, v in index.items()}, indent=2))


def get_inspiration_entry(root: Path, track_id: int) -> TrackEntry | None:
    """The shared entry for `track_id` (name, backing_track, and every
    instrument's take recorded against it by any project so far), or None
    if nothing's ever been recorded for it yet."""
    if not track_id:
        return None
    return load_inspiration_index(root).get(str(track_id))


def record_inspiration_take(
    root: Path, track_id: int, name: str, backing_track: str, duration_seconds: float,
    instrument: str, take: TakeInfo,
) -> None:
    """Record a completed take against `track_id` in the shared index —
    called from splicer.py alongside (not instead of) the normal
    TrackEntry.set_preferred_take() on whichever project's setlist
    actually holds this track, so every *other* project referencing the
    same song can find this take too (see _resolve_filter_slot and
    _load_track_locked's "other instruments' takes" lookup in backend.py)."""
    if not track_id:
        return
    index = load_inspiration_index(root)
    key = str(track_id)
    entry = index.get(key)
    if entry is None:
        entry = TrackEntry(
            name=name, backing_track=backing_track, duration_seconds=duration_seconds, inspiration_track_id=track_id,
        )
        index[key] = entry
    entry.set_preferred_take(instrument, take)
    save_inspiration_index(root, index)


# --- syncing session dirs and individual vault files to/from the remote ---


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


def _files_used_by_track(project: Project, track: TrackEntry, config: StudioConfig) -> set[tuple[Path, str]]:
    """(local_path, vault-relative-path-string) pairs a track needs on
    local disk to be playable/recordable — its own backing track and
    every instrument's take on it, plus (for an inspiration-sourced
    track) whatever the shared index knows about it, since that's where
    an *other* project's take on the same song would be recorded."""
    needed: set[tuple[Path, str]] = set()

    def add_backing(name: str) -> None:
        if name:
            needed.add((project.backing_tracks_dir / name, f"backing_tracks/{name}"))

    def add_take(take: TakeInfo) -> None:
        needed.add((project.completed_takes_dir / take.filename, f"completed_takes/{take.filename}"))
        if take.has_video:
            video_name = Path(take.filename).stem + ".mp4"
            needed.add((project.completed_takes_dir / video_name, f"completed_takes/{video_name}"))

    add_backing(track.backing_track)
    for take in track.preferred_takes.values():
        add_take(take)

    if not track.is_inspiration_filter and track.inspiration_track_id:
        shared = get_inspiration_entry(vault_root(config), track.inspiration_track_id)
        if shared is not None:
            add_backing(shared.backing_track)
            for take in shared.preferred_takes.values():
                add_take(take)

    return needed


def ensure_setlist_files_local(config: StudioConfig, project: Project, log: LogFn | None = None) -> None:
    """Before a session starts, make sure every file its setlist could
    need — every track's backing track, and every instrument's take
    already recorded on it (including, for inspiration-sourced tracks,
    whatever the shared index knows from *other* projects) — actually
    exists on local disk, downloading from the remote backup server for
    whatever's missing. Only matters in "remote" vault mode, where a file
    can legitimately have been pruned locally after its own session
    synced; in "local"/"both" modes everything's already local, so this
    is a fast no-op scan. Best-effort: a download failure is logged, not
    raised — the session still starts, just possibly missing that one
    backing track or layered take."""
    log = log or (lambda msg: None)
    if not config.backup_server:
        return
    from .sync import sync_vault_file_down
    needed = set()
    for track in project.setlist.tracks:
        needed |= _files_used_by_track(project, track, config)
    for local_path, relative in sorted(needed, key=lambda pair: pair[1]):
        if local_path.exists():
            continue
        log(f"Downloading '{relative}' from vault remote...")
        if not sync_vault_file_down(config.backup_server, relative, local_path):
            log(f"Could not download '{relative}' — it may not exist on the remote either.")


# --- one-time migration from the old per-project folder layout ---


def _unique_dest(dest_dir: Path, filename: str) -> Path:
    """`filename` in `dest_dir`, or — if something *different* already
    sits there (a genuine collision, not the same file already migrated)
    — a version of it with a short random prefix added, so migrating two
    unrelated projects' same-named local uploads never clobbers either
    one."""
    import secrets
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    return dest_dir / f"{secrets.token_hex(4)}_{filename}"


def _migrate_track_files(
    track: TrackEntry, old_backing_dir: Path, old_takes_dir: Path,
    new_backing_dir: Path, new_takes_dir: Path, log: LogFn,
) -> None:
    """Move one track's backing file and every instrument's take file
    from their old per-project location into the shared vault
    directories, renaming on the way only if a genuine name collision
    with unrelated content requires it — and rewriting the in-memory
    TrackEntry's filenames to match, since splicing/playback resolve
    files by exactly the name stored here."""
    if track.backing_track:
        src = old_backing_dir / track.backing_track
        if src.exists():
            dest = _unique_dest(new_backing_dir, track.backing_track)
            ensure_dir(new_backing_dir)
            shutil.move(str(src), str(dest))
            if dest.name != track.backing_track:
                log(f"  renamed backing track '{track.backing_track}' -> '{dest.name}' (name collision)")
            track.backing_track = dest.name

    for take in track.preferred_takes.values():
        for filename in (take.filename, Path(take.filename).stem + ".mp4"):
            src = old_takes_dir / filename
            if not src.exists():
                continue
            dest = _unique_dest(new_takes_dir, filename)
            ensure_dir(new_takes_dir)
            shutil.move(str(src), str(dest))
            if dest.name != filename and filename == take.filename:
                log(f"  renamed take '{filename}' -> '{dest.name}' (name collision)")
                take.filename = dest.name


def _prune_unmigratable_files(session_dir: Path, log: LogFn) -> None:
    """Remove any named pipe/socket/device file under session_dir —
    shutil.copytree can't copy these (and would raise partway through,
    aborting the whole migration run), and the only thing that leaves
    one behind is a streaming session's scratch audio FIFO (see
    streaming.py) that didn't get cleaned up, e.g. because the process
    crashed or was killed mid-session — never real session content worth
    preserving."""
    for path in session_dir.rglob("*"):
        if path.is_file() or path.is_dir() or path.is_symlink():
            continue
        log(f"    removing leftover '{path.relative_to(session_dir)}' (not a regular file, can't migrate it)")
        path.unlink()


def _copy_session_to_vault(session_dir: Path, dest: Path, log: LogFn) -> bool:
    """Ensure `dest` holds a verified copy of `session_dir`, resuming
    cleanly if a previous run got partway through and crashed (e.g. hit
    an uncopyable file) — a `dest` that already exists but doesn't
    verify is a prior attempt's leftovers, not something migrated
    already, so it's discarded and redone rather than trusted as-is.
    Returns whether `dest` ends up verified."""
    if dest.exists() and _verify_copy(session_dir, dest):
        log(f"  session '{session_dir.name}' already copied to vault.")
        return True
    if dest.exists():
        log(f"  removing incomplete previous copy of '{session_dir.name}' and retrying.")
        shutil.rmtree(dest, ignore_errors=True)
    log(f"  moving session '{session_dir.name}' -> {dest}")
    _prune_unmigratable_files(session_dir, log)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(session_dir, dest)
    return _verify_copy(session_dir, dest)


def migrate_projects_to_vault(config: StudioConfig, log: LogFn) -> tuple[int, int]:
    """One-time migration from the old per-project folder layout
    (<projects_dir>/<name>/{setlist.json, backing_tracks/,
    completed_takes/, sessions/}) to the current one: a flat
    <projects_dir>/<name>.json setlist file, with backing tracks,
    completed takes, and sessions all moved into the shared vault.
    Inspiration-sourced tracks' takes are also recorded into the shared
    index (record_inspiration_take) so they're immediately reusable from
    any other project.

    Safe to re-run, including resuming a run that crashed partway
    through one project's sessions: the setlist/track-file migration
    step (which isn't safely re-runnable — see _migrate_track_files)
    only happens once, guarded by the flat .json not existing yet: but
    unlike a full "skip the whole project" check, an interrupted run
    that already wrote the flat .json still has its remaining sessions
    picked back up, and the old folder is only removed once every
    session under it has been verified in the vault.

    Returns (projects_migrated, sessions_migrated)."""
    projects_dir = Path(config.projects_dir)
    if not projects_dir.exists():
        return 0, 0

    new_backing_dir = vault_backing_tracks_dir(config)
    new_takes_dir = vault_completed_takes_dir(config)
    root = vault_root(config)

    projects_migrated = 0
    sessions_migrated = 0
    for old_dir in sorted(projects_dir.iterdir()):
        if not old_dir.is_dir():
            continue
        old_setlist_path = old_dir / "setlist.json"
        if not old_setlist_path.exists():
            continue
        new_setlist_path = projects_dir / f"{old_dir.name}.json"

        if new_setlist_path.exists():
            log(f"'{old_dir.name}': setlist already migrated — resuming any unfinished sessions.")
        else:
            log(f"Migrating project '{old_dir.name}'...")
            from .project import Setlist
            setlist = Setlist.from_dict(json.loads(old_setlist_path.read_text()))

            old_backing_dir = old_dir / "backing_tracks"
            old_takes_dir = old_dir / "completed_takes"
            for track in setlist.tracks:
                _migrate_track_files(track, old_backing_dir, old_takes_dir, new_backing_dir, new_takes_dir, log)
                if not track.is_inspiration_filter and track.inspiration_track_id:
                    for instrument, take in track.preferred_takes.items():
                        record_inspiration_take(
                            root, track.inspiration_track_id, track.name, track.backing_track,
                            track.duration_seconds, instrument, take,
                        )

            atomic_write_text(new_setlist_path, json.dumps(setlist.to_dict(), indent=2))
            projects_migrated += 1

        all_sessions_ok = True
        old_sessions_dir = old_dir / "sessions"
        if old_sessions_dir.exists():
            for session_dir in sorted(old_sessions_dir.iterdir()):
                if not session_dir.is_dir():
                    continue
                dest = vault_session_dir(config, old_dir.name, session_dir.name)
                if _copy_session_to_vault(session_dir, dest, log):
                    sessions_migrated += 1
                    sync_and_maybe_prune(config, dest, log=log)
                else:
                    log(f"  WARNING: verification failed for session '{session_dir.name}' — left both copies in place.")
                    all_sessions_ok = False

        if all_sessions_ok:
            shutil.rmtree(old_dir)
        else:
            log(f"'{old_dir.name}': left in place (old folder) — re-run once the warning above is resolved.")

    # Whatever backing tracks/takes just landed locally from every migrated
    # project get the same remote treatment a live session's files would —
    # one batch push per directory rather than per file.
    for local_dir in (new_backing_dir, new_takes_dir):
        if local_dir.exists() and any(local_dir.iterdir()):
            sync_and_maybe_prune_dir(config, local_dir, log=log)

    return projects_migrated, sessions_migrated


def sync_and_maybe_prune_dir(config: StudioConfig, local_dir: Path, log: LogFn | None = None) -> None:
    """Same as sync_and_maybe_prune, but for a whole shared vault
    directory (backing_tracks/ or completed_takes/) rather than one
    session — used once at the end of migration rather than per file."""
    log = log or (lambda msg: None)
    mode = config.session_vault_mode
    if mode not in ("remote", "both"):
        return
    if not config.backup_server:
        return
    from .sync import sync_vault_session_up
    relative = local_dir.relative_to(vault_root(config))
    log(f"Syncing '{relative}' to {config.backup_server}...")
    ok = sync_vault_session_up(local_dir, str(relative), config.backup_server)
    if not ok:
        log(f"Vault sync failed for '{relative}' — kept locally.")
        return
    log(f"Synced '{relative}'.")
    if mode == "remote":
        for child in local_dir.iterdir():
            if child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child, ignore_errors=True)
        log(f"Removed local copies under '{relative}' (remote-only vault mode).")


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
