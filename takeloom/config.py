"""Studio configuration: audio device, sample rate, buffer, channel settings."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .utils import atomic_write_text

DEFAULT_CONFIG_PATH = Path.home() / "studio_config.json"

VALID_SAMPLE_RATES = [44100, 48000, 96000]
VALID_BUFFER_SIZES = [128, 256, 512, 1024, 2048]


@dataclass
class InputLabel:
    """A labeled audio input: maps a human-friendly name to a device + channel."""
    label: str        # e.g. "Mic 1", "Guitar DI"
    device: str       # device name
    channel: int      # 1-based channel number on that device


@dataclass
class Instrument:
    """An instrument input configuration."""
    name: str
    input_label: str   # references an InputLabel.label
    full_name: str = ""  # manufacturer and model, e.g. "Fender American Stratocaster"
    musician: str = ""


@dataclass
class KnownRemote:
    """A remote takeloom instance this machine can connect to as a client.

    No port field: every takeloom instance listens on the same fixed
    remote.protocol.REMOTE_SERVER_PORT, so there's nothing per-remote to
    remember or drift out of sync."""
    host: str
    token: str = ""  # "" until the first successful pairing/authorization
    label: str = ""  # defaults to the server's reported hostname once paired
    always_connect: bool = False  # auto-connect to this remote on startup


def _dedupe_known_remotes(remotes: list[KnownRemote]) -> list[KnownRemote]:
    """Collapse multiple entries for the same host into one, keeping whichever
    fields are actually set. Guards against (and self-heals) a duplicate
    left behind by an older "Add Remote" that didn't check for an existing
    entry first — if "Always connect" ever ended up on the token-less
    duplicate, the paired token lived on the *other* entry and every launch
    re-triggered pairing, since it was never that entry's token that got
    updated on approval."""
    merged: dict[str, KnownRemote] = {}
    order: list[str] = []
    for r in remotes:
        existing = merged.get(r.host)
        if existing is None:
            merged[r.host] = r
            order.append(r.host)
        else:
            existing.token = existing.token or r.token
            existing.label = existing.label or r.label
            existing.always_connect = existing.always_connect or r.always_connect
    return [merged[h] for h in order]


@dataclass
class AuthorizedClient:
    """A remote takeloom instance authorized to connect to this machine's server."""
    token: str
    label: str  # client-reported machine name at authorization time
    last_ip: str = ""
    authorized_at: str = ""  # ISO timestamp
    last_seen_at: str = ""


@dataclass
class StudioConfig:
    sample_rate: int = 48000
    buffer_size: int = 512
    output_device: str = ""
    output_channels: int = 2
    projects_dir: str = str(Path.home() / "Takeloom Projects")
    backup_server: str = ""  # e.g. "user@host:/path/to/backups"
    inspiration_server: str = ""  # e.g. "http://myserver:8000"
    inspiration_api_key: str = ""
    inspiration_volume: float = 1.0
    last_backing_volume: int = 70  # remembered mixer level, seeded onto every newly loaded track
    last_takes_volume: int = 100
    last_instrument_volume: int = 100  # remembered live-monitor gain for the instrument input
    last_selected_project: str = ""  # prefilled on the Record tab at startup
    last_selected_instrument: str = ""
    latency_compensation_ms: float = 0.0  # ms to trim from start of takes during playback
    video_latency_compensation_ms: float = 0.0  # ms to shift video relative to audio when muxing recordings
    studio_musician: str = ""
    studio_name: str = ""
    studio_location: str = ""
    camera_device: str = ""  # platform-specific ffmpeg device id, e.g. avfoundation index or /dev/video0
    camera_label: str = ""   # human-friendly camera name, for display only
    streamdeck_id: str = ""     # serial number of the selected physical Stream Deck; empty = don't connect to one
    streamdeck_label: str = ""  # human-friendly Stream Deck name, for display only
    compressor_enabled: bool = False
    compressor_threshold_db: float = -24.0
    compressor_ratio: float = 4.0
    compressor_attack_ms: float = 10.0
    compressor_release_ms: float = 150.0
    compressor_makeup_db: float = 0.0
    streaming_enabled: bool = False  # stream every session live while it records; see takeloom/streaming.py
    youtube_stream_key: str = ""  # from YouTube Studio's "Go Live" stream settings
    # Matches STREAM_QUALITY_PRESETS[0] in takeloom/streaming.py — the
    # Streaming tab's quality dropdown writes both together as one unit.
    streaming_video_width: int = 1280  # 0 means "stream at the source resolution, unscaled"
    streaming_bitrate_kbps: int = 3000
    window_width: int = 1100  # remembered main window size, so it reopens as it was left
    window_height: int = 650
    known_remotes: list[KnownRemote] = field(default_factory=list)  # remotes this machine can connect to
    remote_authorized_clients: list[AuthorizedClient] = field(default_factory=list)  # clients allowed to connect in
    input_labels: list[InputLabel] = field(default_factory=list)
    instruments: list[Instrument] = field(default_factory=list)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.sample_rate not in VALID_SAMPLE_RATES:
            errors.append(f"Invalid sample rate: {self.sample_rate}. Must be one of {VALID_SAMPLE_RATES}")
        if self.buffer_size not in VALID_BUFFER_SIZES:
            errors.append(f"Invalid buffer size: {self.buffer_size}. Must be one of {VALID_BUFFER_SIZES}")
        if self.output_channels < 1:
            errors.append("Output channels must be >= 1")
        if self.streaming_enabled and not self.youtube_stream_key.strip():
            errors.append("Streaming is enabled but no YouTube stream key is set.")
        return errors

    def get_instrument(self, name: str) -> Instrument | None:
        for inst in self.instruments:
            if inst.name.lower() == name.lower():
                return inst
        return None

    def resolve_input(self, label: str) -> InputLabel | None:
        """Find the InputLabel for a given label string."""
        for il in self.input_labels:
            if il.label.lower() == label.lower():
                return il
        return None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> StudioConfig:
        def _filtered(d: dict, dc: type) -> dict:
            # Drops now-removed keys from configs saved by an older version
            # (e.g. a known_remote's once-editable "port") instead of the
            # dataclass constructor rejecting them outright.
            return {k: v for k, v in d.items() if k in dc.__dataclass_fields__}

        data = dict(data)
        input_labels = [InputLabel(**_filtered(il, InputLabel)) for il in data.pop("input_labels", [])]
        instruments = [Instrument(**_filtered(i, Instrument)) for i in data.pop("instruments", [])]
        known_remotes_raw = [KnownRemote(**_filtered(r, KnownRemote)) for r in data.pop("known_remotes", [])]
        known_remotes = _dedupe_known_remotes(known_remotes_raw)
        remote_authorized_clients = [
            AuthorizedClient(**_filtered(c, AuthorizedClient)) for c in data.pop("remote_authorized_clients", [])
        ]
        filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(
            **filtered,
            input_labels=input_labels,
            instruments=instruments,
            known_remotes=known_remotes,
            remote_authorized_clients=remote_authorized_clients,
        )

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        atomic_write_text(path, json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> StudioConfig:
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text()))

    @classmethod
    def exists(cls, path: Path = DEFAULT_CONFIG_PATH) -> bool:
        return path.exists()
