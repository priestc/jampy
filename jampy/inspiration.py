"""Inspiration track queries and downloads against the radioserver library."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from .config import StudioConfig
from .project import Project, TrackEntry


class InspirationError(Exception):
    """Raised when inspiration tracks can't be queried or downloaded."""


def query_inspiration_tracks(project: Project, config: StudioConfig) -> list[dict]:
    """Query tracks from radioserver matching the project's inspiration filters."""
    if not project.setlist.inspiration:
        raise InspirationError(
            'No inspiration filters in setlist.json. Add an "inspiration" key with '
            'filter sets, e.g.: "inspiration": [{"genre": "Rock"}, {"artist": "Miles Davis"}]'
        )
    if not config.inspiration_server or not config.inspiration_api_key:
        raise InspirationError(
            "inspiration_server and inspiration_api_key must be set (jampy setup-studio)."
        )

    server = config.inspiration_server.rstrip("/")
    payload = json.dumps({"filters": project.setlist.inspiration}).encode()
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

    tracks = data.get("tracks", [])
    if not tracks:
        raise InspirationError("No tracks matched the inspiration filters.")
    return tracks


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
