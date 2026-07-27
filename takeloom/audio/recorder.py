"""Streaming FLAC recorder — continuous capture to disk."""

from __future__ import annotations

import threading
from pathlib import Path
from collections import deque

import numpy as np
import soundfile as sf


class Recorder:
    """Streams mono audio input to a FLAC file on disk.

    Called from the audio callback thread via write(). Actual disk I/O
    happens on a separate writer thread to avoid blocking the callback.
    """

    def __init__(self, path: Path, sample_rate: int, channels: int = 1) -> None:
        self.path = path
        self.sample_rate = sample_rate
        self.channels = channels
        self._file: sf.SoundFile | None = None
        self._buffer: deque[np.ndarray] = deque()
        self._lock = threading.Lock()
        self._writer_thread: threading.Thread | None = None
        self._running = False
        self.frames_written: int = 0

    def start(self) -> None:
        """Open the output file and start the writer thread."""
        self._file = sf.SoundFile(
            str(self.path),
            mode="w",
            samplerate=self.sample_rate,
            channels=self.channels,
            format="FLAC",
            subtype="PCM_16",
        )
        self._running = True
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()

    def write(self, data: np.ndarray) -> None:
        """Queue audio data for writing. Called from audio callback."""
        with self._lock:
            self._buffer.append(data.copy())

    def _writer_loop(self) -> None:
        """Background thread that drains the buffer to disk."""
        while self._running or self._buffer:
            chunk = None
            with self._lock:
                if self._buffer:
                    chunk = self._buffer.popleft()
            if chunk is not None and self._file is not None:
                self._file.write(chunk)
                self.frames_written += len(chunk)
            elif self._running:
                threading.Event().wait(0.01)

    def stop(self) -> None:
        """Stop recording and close the file."""
        self._running = False
        if self._writer_thread:
            self._writer_thread.join(timeout=5.0)
        # Drain any remaining data
        while self._buffer:
            chunk = self._buffer.popleft()
            if self._file is not None:
                self._file.write(chunk)
                self.frames_written += len(chunk)
        if self._file is not None:
            self._file.close()
            self._file = None

    @property
    def elapsed_seconds(self) -> float:
        return self.frames_written / self.sample_rate if self.sample_rate else 0.0
