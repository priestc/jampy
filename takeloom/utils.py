"""Timestamps, take numbering, and path helpers."""

from __future__ import annotations

import os
import re
import tempfile
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


def take_filename(
    track_name: str, instrument: str, take_number: int, source: str, backing_track: str, ext: str = "flac",
) -> str:
    """Generate take filename: 'track - instrument - takeN [source: backing
    track].ext'. `source` is TrackEntry.source_label() — "upload",
    "youtube", or "inspiration" — and `backing_track` its backing_track
    filename (the exact file this take was recorded against); backing
    tracks are shared vault-wide now (see vault.py), so two entries can
    share a display name while pointing at different audio, and the
    filename should make which one — and where it came from — explicit
    rather than relying on the take being found next to the right
    backing track by chance."""
    safe_track = sanitize_filename(track_name)
    safe_inst = sanitize_filename(instrument)
    safe_backing = sanitize_filename(Path(backing_track).stem) if backing_track else "unknown"
    return f"{safe_track} - {safe_inst} - take{take_number} [{source}: {safe_backing}].{ext}"


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


def atomic_write_text(path: Path, text: str) -> None:
    """Write text via temp-file-then-rename, so a concurrent reader on
    another thread/process never observes a truncated/partial file (as a
    plain path.write_text() — which truncates before writing — can produce).
    Uses mkstemp for a guaranteed-unique name, so concurrent writer threads
    in the same process can't collide on the same temp file either."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def frames_to_seconds(frames: int, sample_rate: int) -> float:
    return frames / sample_rate


def seconds_to_frames(seconds: float, sample_rate: int) -> int:
    return int(seconds * sample_rate)
