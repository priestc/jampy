"""Synthesizes a steady click track: the reference cue for camera-based
latency calibration. Unlike the CLI's one-shot beep+HIT reference tone
(data/measure_latency.wav), this is a continuous 1-beat-per-second click
running long enough to catch several instrument hits on camera, so the
offset can be judged from more than one hit.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

DEFAULT_BPM = 60
DEFAULT_DURATION_S = 20.0
_CLICK_FREQ_HZ = 1000.0
_CLICK_MS = 30.0
_DECAY_RATE = 40.0  # envelope decay constant, keeps each click a sharp transient


def generate_metronome_wav(
    path: Path,
    sample_rate: int,
    bpm: int = DEFAULT_BPM,
    duration_s: float = DEFAULT_DURATION_S,
) -> None:
    """Write a stereo WAV of evenly spaced clicks at `bpm` beats per minute."""
    total_frames = int(duration_s * sample_rate)
    audio = np.zeros(total_frames, dtype=np.float32)

    click_frames = int(_CLICK_MS / 1000.0 * sample_rate)
    t = np.arange(click_frames) / sample_rate
    envelope = np.exp(-t * _DECAY_RATE)
    click = (np.sin(2 * np.pi * _CLICK_FREQ_HZ * t) * envelope).astype(np.float32)

    beat_interval_frames = int(sample_rate * 60.0 / bpm)
    start = 0
    while start < total_frames:
        end = min(start + click_frames, total_frames)
        audio[start:end] += click[: end - start]
        start += beat_interval_frames

    stereo = np.column_stack([audio, audio])
    sf.write(str(path), stereo, sample_rate, subtype="PCM_16")
