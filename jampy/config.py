"""Studio configuration: audio device, sample rate, buffer, channel settings."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / "studio_config.json"

VALID_SAMPLE_RATES = [44100, 48000, 96000]
VALID_BUFFER_SIZES = [128, 256, 512, 1024, 2048]


@dataclass
class Instrument:
    """An instrument input configuration."""
    name: str
    device: str  # device name or index
    input_number: int
    musician: str = ""


@dataclass
class StudioConfig:
    sample_rate: int = 48000
    buffer_size: int = 512
    input_device: int | None = None
    output_device: int | None = None
    input_channels: int = 1
    output_channels: int = 2
    projects_dir: str = str(Path.home() / "JamPy Projects")
    studio_musician: str = ""
    studio_name: str = ""
    studio_location: str = ""
    instruments: list[Instrument] = field(default_factory=list)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.sample_rate not in VALID_SAMPLE_RATES:
            errors.append(f"Invalid sample rate: {self.sample_rate}. Must be one of {VALID_SAMPLE_RATES}")
        if self.buffer_size not in VALID_BUFFER_SIZES:
            errors.append(f"Invalid buffer size: {self.buffer_size}. Must be one of {VALID_BUFFER_SIZES}")
        if self.input_channels < 1:
            errors.append("Input channels must be >= 1")
        if self.output_channels < 1:
            errors.append("Output channels must be >= 1")
        return errors

    def get_instrument(self, name: str) -> Instrument | None:
        for inst in self.instruments:
            if inst.name.lower() == name.lower():
                return inst
        return None

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> StudioConfig:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        instruments = [Instrument(**i) for i in data.pop("instruments", [])]
        filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**filtered, instruments=instruments)

    @classmethod
    def exists(cls, path: Path = DEFAULT_CONFIG_PATH) -> bool:
        return path.exists()
