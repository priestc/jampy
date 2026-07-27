"""Studio configuration: audio device, sample rate, buffer, channel settings."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

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
    """A remote jampy instance this machine can connect to as a client."""
    host: str
    port: int
    token: str = ""  # "" until the first successful pairing/authorization
    label: str = ""  # defaults to the server's reported hostname once paired
    always_connect: bool = False  # auto-connect to this remote on startup


@dataclass
class AuthorizedClient:
    """A remote jampy instance authorized to connect to this machine's server."""
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
    projects_dir: str = str(Path.home() / "JamPy Projects")
    backup_server: str = ""  # e.g. "user@host:/path/to/backups"
    inspiration_server: str = ""  # e.g. "http://myserver:8000"
    inspiration_api_key: str = ""
    inspiration_volume: float = 1.0
    last_backing_volume: int = 70  # remembered mixer level, seeded onto every newly loaded track
    last_takes_volume: int = 100
    last_selected_project: str = ""  # prefilled on the Record tab at startup
    last_selected_instrument: str = ""
    latency_compensation_ms: float = 0.0  # ms to trim from start of takes during playback
    video_latency_compensation_ms: float = 0.0  # ms to shift video relative to audio when muxing recordings
    studio_musician: str = ""
    studio_name: str = ""
    studio_location: str = ""
    camera_device: str = ""  # platform-specific ffmpeg device id, e.g. avfoundation index or /dev/video0
    camera_label: str = ""   # human-friendly camera name, for display only
    remote_server_enabled: bool = False  # accept incoming remote-control connections
    remote_server_port: int = 51823
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
        data = dict(data)
        input_labels = [InputLabel(**il) for il in data.pop("input_labels", [])]
        instruments = [Instrument(**i) for i in data.pop("instruments", [])]
        known_remotes = [KnownRemote(**r) for r in data.pop("known_remotes", [])]
        remote_authorized_clients = [AuthorizedClient(**c) for c in data.pop("remote_authorized_clients", [])]
        filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(
            **filtered,
            input_labels=input_labels,
            instruments=instruments,
            known_remotes=known_remotes,
            remote_authorized_clients=remote_authorized_clients,
        )

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> StudioConfig:
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text()))

    @classmethod
    def exists(cls, path: Path = DEFAULT_CONFIG_PATH) -> bool:
        return path.exists()
