"""Post-session processing: parse log, splice completed takes, update setlist."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from ..project import Project, TakeInfo
from ..utils import (
    take_filename,
    next_take_number,
    seconds_to_frames,
    ensure_dir,
)
from ..audio.formats import write_flac


@dataclass
class CompletedTake:
    """A completed take identified from the session log."""
    track_index: int
    track_name: str
    start_frame: int
    end_frame: int


def parse_session_log(log_path: Path) -> tuple[str, list[CompletedTake]]:
    """Parse a session log and identify completed takes.

    A completed take is a sequence: record_start → song_end
    with NO back_to_start events in between.

    Returns (instrument, list of completed takes).
    """
    data = json.loads(log_path.read_text())
    instrument = data["instrument"]
    events = data["events"]

    completed: list[CompletedTake] = []
    current_start_frame: int | None = None
    current_track_index: int = 0
    current_track_name: str = ""
    had_restart = False

    for event in events:
        etype = event["event_type"]
        track_idx = event.get("track_index", 0)
        track_name = event.get("track_name", "")
        details = event.get("details", "")

        if etype == "record_start":
            # Parse frame from details
            frame = _parse_frame(details)
            current_start_frame = frame
            current_track_index = track_idx
            current_track_name = track_name
            had_restart = False

        elif etype == "back_to_start":
            had_restart = True
            # Update start frame to the new recording position
            frame = _parse_frame(details)
            current_start_frame = frame

        elif etype == "song_end":
            frame = _parse_frame(details)
            if current_start_frame is not None and not had_restart:
                completed.append(CompletedTake(
                    track_index=current_track_index,
                    track_name=current_track_name,
                    start_frame=current_start_frame,
                    end_frame=frame,
                ))
            current_start_frame = None
            had_restart = False

    return instrument, completed


def _parse_frame(details: str) -> int:
    """Extract frame number from event details string."""
    for part in details.split(","):
        part = part.strip()
        if part.startswith("frame="):
            return int(part.split("=")[1])
    return 0


def splice_takes(
    project: Project,
    session_dir: Path,
    raw_recording_path: Path,
) -> list[Path]:
    """Splice completed takes from the raw recording and save them.

    Returns list of paths to saved take files.
    """
    log_path = session_dir / "session_log.json"
    if not log_path.exists():
        return []

    instrument, completed = parse_session_log(log_path)
    if not completed:
        return []

    # Read the raw recording
    raw_data, sr = sf.read(str(raw_recording_path), dtype="float32", always_2d=True)
    total_frames = len(raw_data)

    saved_paths: list[Path] = []
    completed_dir = ensure_dir(project.completed_takes_dir)

    for take in completed:
        start = max(0, take.start_frame)
        end = min(take.end_frame, total_frames)
        if end <= start:
            continue

        segment = raw_data[start:end]

        # Determine take number
        take_num = next_take_number(completed_dir, take.track_name, instrument)
        filename = take_filename(take.track_name, instrument, take_num)
        out_path = completed_dir / filename

        write_flac(out_path, segment, sr)
        saved_paths.append(out_path)

        # Update setlist with new preferred take
        if 0 <= take.track_index < len(project.setlist.tracks):
            track_entry = project.setlist.tracks[take.track_index]
            take_info = TakeInfo(
                instrument=instrument,
                take_number=take_num,
                filename=filename,
            )
            track_entry.set_preferred_take(instrument, take_info)

    project.save_setlist()
    return saved_paths
