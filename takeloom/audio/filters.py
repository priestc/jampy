"""Audio filters applied to the instrument input signal, inside AudioEngine's
real-time callback — see engine.py's use of Compressor. Kept dependency-free
(numpy only, no scipy) as it must run inline with the sd.Stream callback."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class CompressorSettings:
    """Feed-forward dynamic-range compressor settings, in the classic
    threshold/ratio/attack/release/makeup-gain shape."""
    enabled: bool = False
    threshold_db: float = -24.0
    ratio: float = 4.0       # 1.0 = no compression, higher = more
    attack_ms: float = 10.0
    release_ms: float = 150.0
    makeup_gain_db: float = 0.0


def _time_constant_coeff(time_ms: float, sample_rate: int) -> float:
    """One-pole envelope-follower coefficient for a given attack/release time."""
    if time_ms <= 0:
        return 0.0
    return math.exp(-1.0 / (sample_rate * (time_ms / 1000.0)))


class Compressor:
    """Mono feed-forward compressor with a one-pole attack/release envelope
    follower, processed one block at a time inside the audio callback.

    Runs sample-by-sample (the envelope at sample N depends on sample N-1),
    which is inherently sequential — but blocks are small (the configured
    audio buffer_size, at most a couple thousand samples) so this stays well
    within the callback's real-time deadline.
    """

    def __init__(self, sample_rate: int, settings: CompressorSettings | None = None) -> None:
        self.sample_rate = sample_rate
        self.settings = settings or CompressorSettings()
        self._envelope_db = -120.0  # runs continuously across blocks for a smooth envelope

    def process(self, mono: np.ndarray) -> np.ndarray:
        """mono: float32 array of shape (frames, 1). Returns a same-shaped array."""
        s = self.settings
        if not s.enabled:
            return mono

        attack_coeff = _time_constant_coeff(s.attack_ms, self.sample_rate)
        release_coeff = _time_constant_coeff(s.release_ms, self.sample_rate)
        makeup = 10.0 ** (s.makeup_gain_db / 20.0)
        threshold_db = s.threshold_db
        ratio = max(s.ratio, 1.0)

        out = np.empty_like(mono)
        envelope_db = self._envelope_db
        for i in range(mono.shape[0]):
            sample = float(mono[i, 0])
            level_db = 20.0 * math.log10(max(abs(sample), 1e-9))
            coeff = attack_coeff if level_db > envelope_db else release_coeff
            envelope_db = coeff * envelope_db + (1.0 - coeff) * level_db
            if envelope_db > threshold_db:
                gain_db = (threshold_db - envelope_db) * (1.0 - 1.0 / ratio)
            else:
                gain_db = 0.0
            out[i, 0] = sample * (10.0 ** (gain_db / 20.0)) * makeup
        self._envelope_db = envelope_db
        # Makeup gain can push a sample past 0dBFS even after gain reduction —
        # clip rather than let it overflow into the recorded file/monitor mix.
        np.clip(out, -1.0, 1.0, out=out)
        return out
