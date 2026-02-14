"""Mix backing track + preferred takes for playback output."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .formats import read_audio


@dataclass
class MixSource:
    """A single audio source in the mix."""
    name: str
    data: np.ndarray  # float32, shape (N, 2) stereo
    volume: float = 1.0
    active: bool = True


class Mixer:
    """Pre-loads and mixes backing track + completed takes.

    All sources are pre-loaded as float32 numpy arrays.
    read() returns summed audio at the current playback position.
    """

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.sources: list[MixSource] = []
        self._position: int = 0  # current frame position
        self._playing: bool = False

    def add_source(self, name: str, path: Path, volume: float = 1.0) -> None:
        """Load an audio file and add it as a mix source."""
        data, sr = read_audio(path, self.sample_rate)
        # Ensure stereo
        if data.ndim == 1:
            data = np.column_stack([data, data])
        elif data.shape[1] == 1:
            data = np.column_stack([data[:, 0], data[:, 0]])
        self.sources.append(MixSource(name=name, data=data, volume=volume))

    def clear(self) -> None:
        """Remove all sources."""
        self.sources.clear()
        self._position = 0

    @property
    def duration_frames(self) -> int:
        """Duration of the longest source in frames."""
        if not self.sources:
            return 0
        return max(len(s.data) for s in self.sources if s.active)

    @property
    def duration_seconds(self) -> float:
        return self.duration_frames / self.sample_rate if self.sample_rate else 0.0

    @property
    def position(self) -> int:
        return self._position

    @property
    def position_seconds(self) -> float:
        return self._position / self.sample_rate if self.sample_rate else 0.0

    def seek(self, frame: int) -> None:
        self._position = max(0, frame)

    def reset(self) -> None:
        """Reset playback to beginning."""
        self._position = 0

    def set_playing(self, playing: bool) -> None:
        self._playing = playing

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def is_finished(self) -> bool:
        """True if playback position is past all sources."""
        return self._position >= self.duration_frames and self.duration_frames > 0

    def read(self, frames: int) -> np.ndarray:
        """Read mixed audio for the next `frames` frames.

        Returns stereo float32 array of shape (frames, 2).
        If not playing, returns silence.
        """
        output = np.zeros((frames, 2), dtype=np.float32)
        if not self._playing:
            return output

        for source in self.sources:
            if not source.active:
                continue
            src_len = len(source.data)
            start = self._position
            end = start + frames
            if start >= src_len:
                continue
            actual_end = min(end, src_len)
            n = actual_end - start
            output[:n] += source.data[start:actual_end] * source.volume

        self._position += frames
        # Clip to prevent clipping distortion
        np.clip(output, -1.0, 1.0, out=output)
        return output

    def set_volume(self, name: str, volume: float) -> None:
        for source in self.sources:
            if source.name == name:
                source.volume = volume
                break
