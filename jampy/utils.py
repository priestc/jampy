"""Timestamps, take numbering, and path helpers."""

from __future__ import annotations

import re
import time
from pathlib import Path


def timestamp_now() -> float:
    """Return monotonic time for audio-accurate measurements."""
    return time.monotonic()


def wall_timestamp() -> str:
    """Return human-readable wall-clock timestamp for logging."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: float) -> str:
    """Format seconds as MM:SS."""
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def format_duration_hms(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    h, remainder = divmod(int(seconds), 3600)
    m, s = divmod(remainder, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def sanitize_filename(name: str) -> str:
    """Remove characters unsafe for filenames."""
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()


def take_filename(track_name: str, instrument: str, take_number: int, ext: str = "flac") -> str:
    """Generate take filename: 'track - instrument - takeN.ext'."""
    safe_track = sanitize_filename(track_name)
    safe_inst = sanitize_filename(instrument)
    return f"{safe_track} - {safe_inst} - take{take_number}.{ext}"


def next_take_number(completed_dir: Path, track_name: str, instrument: str) -> int:
    """Find the next available take number for a track/instrument combo."""
    prefix = f"{sanitize_filename(track_name)} - {sanitize_filename(instrument)} - take"
    max_num = 0
    if completed_dir.exists():
        for f in completed_dir.iterdir():
            if f.stem.startswith(prefix.rstrip("take")):
                match = re.search(r"take(\d+)", f.stem)
                if match:
                    max_num = max(max_num, int(match.group(1)))
    return max_num + 1


def ensure_dir(path: Path) -> Path:
    """Create directory if it doesn't exist, return the path."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def frames_to_seconds(frames: int, sample_rate: int) -> float:
    return frames / sample_rate


def seconds_to_frames(seconds: float, sample_rate: int) -> int:
    return int(seconds * sample_rate)
