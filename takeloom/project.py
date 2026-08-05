"""Project, Setlist, and TrackEntry management."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .utils import atomic_write_text, ensure_dir, sanitize_filename


@dataclass
class TakeInfo:
    """Reference to a completed take file."""
    instrument: str
    take_number: int
    filename: str
    volume: float = 1.0
    # Derived fresh from disk by Project.open() (see _sync_has_video) rather
    # than trusted as persisted truth — self-heals if the video mux failed,
    # was never attempted, or the .mp4 was later removed by hand.
    has_video: bool = False


@dataclass
class TrackEntry:
    """A single song/track in the setlist.

    A track with is_inspiration_filter=True is a standing "slot" rather
    than a fixed song: backing_track stays "" (there's no file — this
    entry is never itself loaded for playback), and inspiration_filter
    holds the filter criteria (e.g. {"artist": "Miles Davis"} or
    {"genre": "Rock"}) a session draws an actual song from fresh each time
    this slot comes up — see backend.py's _resolve_filter_slot. The
    setlist itself never grows a new top-level entry for a drawn song;
    instead, filter_takes holds one nested TrackEntry per distinct song
    this slot has ever drawn that went on to earn a take, keyed by
    str(inspiration_track_id) — each with its own real preferred_takes,
    exactly like an ordinary track, just not a row of its own in
    Setlist.tracks. This is what lets a later session prefer redrawing a
    song other instruments have already recorded on this slot (see
    _resolve_filter_slot) instead of always drawing something brand new,
    and lets the Setlist view show something like "has takes: bass,
    electric-guitar" for the slot as a whole (see ui/record.py's
    _track_display) without the setlist growing new rows for every draw.
    preferred_takes on the filter slot's own top-level entry is always
    empty, which is also what makes it come up as "still needs a take"
    again on every future session.
    """
    name: str
    backing_track: str  # relative path to backing track file
    duration_seconds: float = 0.0
    volume: int = 100  # playback volume percentage
    takes_volume: int = 100  # playback volume for other instruments' takes
    inspiration_track_id: int = 0  # radioserver track ID (0 = local file)
    is_inspiration_filter: bool = False
    inspiration_filter: dict = field(default_factory=dict)
    filter_takes: dict = field(default_factory=dict)  # str(inspiration_track_id) -> nested TrackEntry
    preferred_takes: dict[str, TakeInfo] = field(default_factory=dict)
    # key = instrument name, value = preferred take for that instrument

    def set_preferred_take(self, instrument: str, take: TakeInfo) -> None:
        self.preferred_takes[instrument] = take

    def get_take_for_instrument(self, instrument: str) -> TakeInfo | None:
        return self.preferred_takes.get(instrument)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> TrackEntry:
        takes = {}
        for inst, take_data in data.get("preferred_takes", {}).items():
            takes[inst] = TakeInfo(**take_data)
        filter_takes = {
            key: TrackEntry.from_dict(entry_data) for key, entry_data in data.get("filter_takes", {}).items()
        }
        return cls(
            name=data["name"],
            backing_track=data["backing_track"],
            duration_seconds=data.get("duration_seconds", 0.0),
            volume=data.get("volume", 100),
            takes_volume=data.get("takes_volume", 100),
            inspiration_track_id=data.get("inspiration_track_id", 0),
            is_inspiration_filter=data.get("is_inspiration_filter", False),
            inspiration_filter=data.get("inspiration_filter", {}),
            filter_takes=filter_takes,
            preferred_takes=takes,
        )


@dataclass
class Setlist:
    """Ordered list of tracks for a project."""
    tracks: list[TrackEntry] = field(default_factory=list)
    backup_server: str = ""
    inspiration: list[dict] = field(default_factory=list)

    def add_track(self, track: TrackEntry) -> None:
        self.tracks.append(track)

    def remove_track(self, index: int) -> None:
        if 0 <= index < len(self.tracks):
            self.tracks.pop(index)

    def move_track(self, from_idx: int, to_idx: int) -> None:
        if 0 <= from_idx < len(self.tracks) and 0 <= to_idx < len(self.tracks):
            track = self.tracks.pop(from_idx)
            self.tracks.insert(to_idx, track)

    def to_dict(self) -> dict:
        d: dict = {"tracks": [t.to_dict() for t in self.tracks]}
        if self.backup_server:
            d["backup_server"] = self.backup_server
        if self.inspiration:
            d["inspiration"] = self.inspiration
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Setlist:
        tracks = [TrackEntry.from_dict(t) for t in data.get("tracks", [])]
        return cls(
            tracks=tracks,
            backup_server=data.get("backup_server", ""),
            inspiration=data.get("inspiration", []),
        )


class Project:
    """A recording project with a setlist and directory structure."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = path.name
        self.backing_tracks_dir = path / "backing_tracks"
        self.completed_takes_dir = path / "completed_takes"
        self.sessions_dir = path / "sessions"
        self.setlist_path = path / "setlist.json"
        self.setlist = Setlist()

    def create(self) -> None:
        """Create project directory structure."""
        ensure_dir(self.path)
        ensure_dir(self.backing_tracks_dir)
        ensure_dir(self.completed_takes_dir)
        ensure_dir(self.sessions_dir)
        self.save_setlist()

    @classmethod
    def open(cls, path: Path) -> Project:
        """Open an existing project."""
        proj = cls(path)
        if proj.setlist_path.exists():
            data = json.loads(proj.setlist_path.read_text())
            proj.setlist = Setlist.from_dict(data)
            proj._sync_has_video()
        return proj

    def _sync_has_video(self) -> None:
        """Set each take's has_video flag from whether its companion .mp4
        actually exists in completed_takes_dir, rather than from whatever
        was last persisted — so a take is correctly flagged "audio only"
        whether video was never recorded, muxing failed, or the file was
        since deleted."""
        for track in self.setlist.tracks:
            for take in track.preferred_takes.values():
                video_path = self.completed_takes_dir / (Path(take.filename).stem + ".mp4")
                take.has_video = video_path.exists()

    @classmethod
    def create_new(cls, parent_dir: Path, name: str) -> Project:
        """Create a new project in the given directory."""
        safe_name = sanitize_filename(name)
        proj = cls(parent_dir / safe_name)
        proj.create()
        return proj

    def save_setlist(self) -> None:
        atomic_write_text(self.setlist_path, json.dumps(self.setlist.to_dict(), indent=2))

    def load_setlist(self) -> None:
        if self.setlist_path.exists():
            data = json.loads(_strip_json_comments(self.setlist_path.read_text()))
            self.setlist = Setlist.from_dict(data)

    def add_inspiration_filter_slot(self, label: str, filter_criteria: dict) -> TrackEntry:
        """Add a standing "draw a random song from this filter each
        session" slot to the setlist — see TrackEntry's docstring. Unlike
        add_backing_track, there's no file to copy; the slot itself is
        never played directly."""
        entry = TrackEntry(
            name=label, backing_track="", is_inspiration_filter=True, inspiration_filter=dict(filter_criteria),
        )
        self.setlist.add_track(entry)
        self.save_setlist()
        return entry

    def add_backing_track(
        self, source_path: Path, track_name: str | None = None, duration_seconds: float = 0.0,
    ) -> TrackEntry:
        """Add a backing track file to the project. Copies the file into
        backing_tracks/, unless it's already sitting there (e.g. a file just
        downloaded directly into that directory)."""
        import shutil
        dest = self.backing_tracks_dir / source_path.name
        if dest.resolve() != source_path.resolve():
            shutil.copy2(source_path, dest)
        name = track_name or source_path.stem
        entry = TrackEntry(name=name, backing_track=dest.name, duration_seconds=duration_seconds)
        self.setlist.add_track(entry)
        self.save_setlist()
        return entry

    def list_projects(parent_dir: Path) -> list[Path]:
        """List existing projects in a directory."""
        if not parent_dir.exists():
            return []
        return [
            p for p in sorted(parent_dir.iterdir())
            if p.is_dir() and (p / "setlist.json").exists()
        ]
