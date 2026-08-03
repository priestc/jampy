"""Inspiration track queries and downloads against the radioserver library."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import StudioConfig
from .project import Project, TrackEntry


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


def _get_suggestions(config: StudioConfig, kind: str, params: dict) -> list[str]:
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


def search_title_suggestions(config: StudioConfig, partial: str, artist: str = "", limit: int = 10) -> list[str]:
    """Autocomplete suggestions for the Add to Playlist dialog's Title
    field, optionally narrowed to a specific artist."""
    params = {"q": partial.strip(), "limit": limit}
    if artist.strip():
        params["artist"] = artist.strip()
    return _get_suggestions(config, "titles", params)


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


def download_inspiration_track(track: TrackEntry, backing_path: Path, config: StudioConfig) -> None:
    """Download an inspiration track's audio to backing_path."""
    server = config.inspiration_server.rstrip("/")
    url = f"{server}/library/api/tracks/{track.inspiration_track_id}/download/"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {config.inspiration_api_key}"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            backing_path.write_bytes(resp.read())
    except urllib.error.URLError as e:
        raise InspirationError(f"Download failed: {e}") from e
