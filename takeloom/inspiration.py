"""Inspiration track queries and downloads against the radioserver library."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from .config import StudioConfig
from .project import Project, TrackEntry

ProgressCallback = Callable[[float | None, str], None]


class InspirationError(Exception):
    """Raised when inspiration tracks can't be queried or downloaded."""


def _post_track_query(config: StudioConfig, filters: list[dict]) -> list[dict]:
    if not config.inspiration_server or not config.inspiration_api_key:
        raise InspirationError(
            "inspiration_server and inspiration_api_key must be set (takeloom setup-studio)."
        )

    server = config.inspiration_server.rstrip("/")
    payload = json.dumps({"filters": filters}).encode()
    req = urllib.request.Request(
        f"{server}/library/api/tracks/",
        data=payload,
        headers={"Authorization": f"Bearer {config.inspiration_api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise InspirationError(f"Error contacting server: {e}") from e
    return data.get("tracks", [])


def query_inspiration_tracks(project: Project, config: StudioConfig) -> list[dict]:
    """Query tracks from radioserver matching the project's inspiration filters."""
    if not project.setlist.inspiration:
        raise InspirationError(
            'No inspiration filters in setlist.json. Add an "inspiration" key with '
            'filter sets, e.g.: "inspiration": [{"genre": "Rock"}, {"artist": "Miles Davis"}]'
        )
    tracks = _post_track_query(config, project.setlist.inspiration)
    if not tracks:
        raise InspirationError("No tracks matched the inspiration filters.")
    return tracks


def search_inspiration_tracks(config: StudioConfig, artist: str = "", title: str = "") -> list[dict]:
    """Query radioserver directly by artist and/or title, independent of a
    project's own configured inspiration filters — backs the Add to
    Playlist dialog's "Add from Inspiration" search, as opposed to
    query_inspiration_tracks()'s per-project filtered browsing."""
    filters = {k: v for k, v in {"artist": artist.strip(), "title": title.strip()}.items() if v}
    if not filters:
        raise InspirationError("Enter an artist and/or title to search.")
    tracks = _post_track_query(config, [filters])
    if not tracks:
        raise InspirationError(f"No match found for {_describe(artist, title)}.")
    return tracks


def _describe(artist: str, title: str) -> str:
    if artist and title:
        return f'"{artist} - {title}"'
    return f'"{artist or title}"'


def select_best_match(tracks: list[dict], artist: str, title: str) -> dict:
    """Pick the track that actually matches what was searched for, out of
    whatever /library/api/tracks/'s filter search returned. That endpoint
    is built for broad library-browsing filters (see
    query_inspiration_tracks) rather than a precise "find this one song"
    lookup, so it can return loosely-related tracks alongside — or
    instead of — an exact hit (e.g. matching just the artist and
    ignoring an unmatched title). Requiring an exact, case-insensitive
    match on whichever of artist/title was actually given — and raising
    rather than guessing when there isn't one — is what stops a search
    like "Bob Dylan" / "Are You Ready" from silently adding some other
    Bob Dylan track instead."""
    artist_norm = artist.strip().lower()
    title_norm = title.strip().lower()

    def is_exact(t: dict) -> bool:
        if artist_norm and t.get("artist", "").strip().lower() != artist_norm:
            return False
        if title_norm and t.get("title", "").strip().lower() != title_norm:
            return False
        return True

    exact = [t for t in tracks if is_exact(t)]
    if exact:
        return exact[0]
    raise InspirationError(
        f"No exact match for {_describe(artist, title)} — the server returned "
        f"{len(tracks)} similar track(s) instead. Try adjusting the artist/title."
    )


def _get_suggestions(config: StudioConfig, kind: str, params: dict) -> list:
    """GET one of the inspiration server's autocomplete endpoints (see
    docs/inspiration-server-autocomplete-api.md). Autocomplete fires on
    every keystroke and isn't a user-triggered action the way search/
    download are, so failures here are swallowed and return [] rather
    than raising InspirationError — a slow/unreachable/unconfigured
    server should just mean no suggestions, not an error popup while
    someone is mid-word."""
    if not config.inspiration_server or not config.inspiration_api_key or not params.get("q"):
        return []
    server = config.inspiration_server.rstrip("/")
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{server}/library/api/autocomplete/{kind}/?{query}",
        headers={"Authorization": f"Bearer {config.inspiration_api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, ValueError, OSError):
        return []
    return data.get("suggestions", [])


def search_artist_suggestions(config: StudioConfig, partial: str, limit: int = 10) -> list[str]:
    """Autocomplete suggestions for the Add to Playlist dialog's Artist field."""
    return _get_suggestions(config, "artists", {"q": partial.strip(), "limit": limit})


def search_title_suggestions(config: StudioConfig, partial: str, artist: str = "", limit: int = 10) -> list[dict]:
    """Autocomplete suggestions for the Add to Playlist dialog's Title
    field, optionally narrowed to a specific artist. Each result is a
    track dict (id/artist/title/year/format/duration) — see
    docs/inspiration-server-autocomplete-api.md — so selecting one can
    add that exact track directly, with no secondary by-name search
    needed. Tolerates an older server still returning bare title strings
    (normalized here to a dict with no "id"), which just means the
    caller falls back to the by-name search path for that selection."""
    params = {"q": partial.strip(), "limit": limit}
    if artist.strip():
        params["artist"] = artist.strip()
    raw = _get_suggestions(config, "titles", params)
    return [item if isinstance(item, dict) else {"title": item} for item in raw]


def find_or_add_inspiration_track(project: Project, track_info: dict) -> TrackEntry:
    """Return the setlist entry for an inspiration track, creating it if absent."""
    track_id = track_info["id"]
    for entry in project.setlist.tracks:
        if entry.inspiration_track_id == track_id:
            return entry
    artist = track_info.get("artist", "Unknown")
    title = track_info.get("title", "Unknown")
    year = track_info.get("year", "")
    fmt = track_info.get("format", "flac") or "flac"
    duration = float(track_info.get("duration") or 0)
    year_str = f" ({year})" if year else ""
    name = f"{artist} - {title}{year_str}"
    entry = TrackEntry(
        name=name,
        backing_track=f"inspiration_{track_id}.{fmt}",
        duration_seconds=duration,
        inspiration_track_id=track_id,
    )
    project.setlist.add_track(entry)
    return entry


_DOWNLOAD_CHUNK_SIZE = 65536


def download_inspiration_track(
    track: TrackEntry, backing_path: Path, config: StudioConfig, on_progress: ProgressCallback | None = None,
) -> None:
    """Download an inspiration track's audio to backing_path. Inspiration
    files are full-quality (often FLAC) and can take a while, so this
    streams in chunks and reports live progress the same way
    youtube.download_youtube_video does, rather than blocking silently
    on a single resp.read()."""
    server = config.inspiration_server.rstrip("/")
    url = f"{server}/library/api/tracks/{track.inspiration_track_id}/download/"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {config.inspiration_api_key}"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if total else None
            read = 0
            with open(backing_path, "wb") as f:
                while True:
                    chunk = resp.read(_DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    read += len(chunk)
                    if on_progress:
                        if total:
                            on_progress(read / total * 100, f"Downloading {track.name}... ({read // 1024} / {total // 1024} KB)")
                        else:
                            on_progress(None, f"Downloading {track.name}... ({read // 1024} KB)")
            if total is not None and read != total:
                # The server said how big the file was but the connection
                # dropped (or otherwise stopped) before all of it arrived —
                # resp.read() just returns b"" at that point rather than
                # raising, so without this check a truncated download would
                # silently look like a completed one.
                raise InspirationError(f"Download incomplete: got {read} of {total} bytes.")
    except urllib.error.URLError as e:
        # A partial file left behind here would look "already downloaded"
        # to add_inspiration_backing_track's exists() check next time,
        # permanently leaving a truncated/corrupt backing track in place.
        backing_path.unlink(missing_ok=True)
        raise InspirationError(f"Download failed: {e}") from e
    except Exception:
        backing_path.unlink(missing_ok=True)
        raise
