"""AudioEngine: sd.Stream callback orchestration for simultaneous record + playback."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

from .recorder import Recorder
from .mixer import Mixer


class AudioEngine:
    """Manages the sounddevice Stream for simultaneous recording and playback.

    The audio callback captures mono input to the Recorder and reads
    mixed stereo output from the Mixer.
    """

    def __init__(
        self,
        sample_rate: int = 48000,
        buffer_size: int = 512,
        input_device: int | None = None,
        output_device: int | None = None,
        input_channels: int = 1,
        output_channels: int = 2,
    ) -> None:
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.input_device = input_device
        self.output_device = output_device
        self.input_channels = input_channels
        self.output_channels = output_channels

        self.recorder: Recorder | None = None
        self.mixer = Mixer(sample_rate)
        self._stream: sd.Stream | None = None
        self._running = False
        self._peak_level: float = 0.0
        self._on_song_end: Callable[[], None] | None = None

    @property
    def peak_level(self) -> float:
        return self._peak_level

    def set_on_song_end(self, callback: Callable[[], None] | None) -> None:
        """Set callback invoked when mixer reaches end of backing track."""
        self._on_song_end = callback

    def start_recording(self, output_path: Path) -> None:
        """Create and start the recorder."""
        self.recorder = Recorder(output_path, self.sample_rate, self.input_channels)
        self.recorder.start()

    def stop_recording(self) -> None:
        """Stop the recorder."""
        if self.recorder:
            self.recorder.stop()
            self.recorder = None

    def _callback(
        self,
        indata: np.ndarray,
        outdata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        """Audio stream callback — runs in real-time audio thread."""
        # Capture mono input
        if indata.shape[1] > 1:
            mono = indata[:, 0:1].copy()
        else:
            mono = indata.copy()

        # Record input to disk
        if self.recorder:
            self.recorder.write(mono)

        # Update peak level for VU meter
        self._peak_level = float(np.max(np.abs(mono)))

        # Playback output: mix backing track + input monitoring
        mix = self.mixer.read(frames)
        if self.output_channels == 2:
            # Add mono input to both stereo channels
            outdata[:] = mix
            outdata[:, 0] += mono[:, 0]
            outdata[:, 1] += mono[:, 0]
        else:
            outdata[:, 0] = mix[:, 0] + mono[:, 0]

        np.clip(outdata, -1.0, 1.0, out=outdata)

        # Check if song ended
        if self.mixer.is_playing and self.mixer.is_finished:
            self.mixer.set_playing(False)
            if self._on_song_end:
                # Fire callback from a separate thread to avoid blocking audio
                threading.Thread(target=self._on_song_end, daemon=True).start()

    def start(self) -> None:
        """Start the audio stream."""
        if self._running:
            return
        # Compute latency from buffer size for minimal delay
        latency = self.buffer_size / self.sample_rate
        self._stream = sd.Stream(
            samplerate=self.sample_rate,
            blocksize=self.buffer_size,
            device=(self.input_device, self.output_device),
            channels=(self.input_channels, self.output_channels),
            dtype="float32",
            latency=latency,
            callback=self._callback,
        )
        self._stream.start()
        self._running = True

    def stop(self) -> None:
        """Stop the audio stream and recorder."""
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self.stop_recording()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def recording_elapsed(self) -> float:
        if self.recorder:
            return self.recorder.elapsed_seconds
        return 0.0
