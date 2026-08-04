"""Live streaming a session to YouTube over RTMP.

This piggybacks on the same ffmpeg process VideoRecorder already runs for a
session's local video file, rather than starting a second one — camera
capture APIs (avfoundation/v4l2/dshow) generally refuse a second concurrent
open of the same device, so a standalone streaming process can't share the
camera with the one already recording. See video/capture.py's
`_build_capture_cmd` for the extra filter_complex branch and RTMP output
this enables.

Live audio reaches that same ffmpeg process over a named pipe (FIFO):
LiveAudioFeeder is fed the exact full production mix AudioEngine already
writes to session_mix.flac (see AudioEngine.set_stream_sink) — same audio,
just also piped live instead of only ever landing on disk.
"""

from __future__ import annotations

import os
import queue
import select
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

YOUTUBE_RTMP_BASE = "rtmp://a.rtmp.youtube.com/live2"


def youtube_rtmp_url(stream_key: str) -> str:
    return f"{YOUTUBE_RTMP_BASE}/{stream_key.strip()}"


def fifo_supported() -> bool:
    """Named pipes aren't available on Windows. Streaming only ever runs on
    the machine with the camera/audio interface attached (see CLAUDE.md's
    testing setup — that's the Mac Mini today), but this keeps a Windows
    install from trying and hanging instead of just skipping streaming."""
    return hasattr(os, "mkfifo")


@dataclass
class StreamTarget:
    """Everything VideoRecorder needs to add a live RTMP branch onto its
    ffmpeg process, alongside the local-file recording it already does."""
    rtmp_url: str
    audio_fifo: Path
    sample_rate: int
    channels: int
    width: int = 1280  # streamed video is downscaled to this; the local recording is untouched


class LiveAudioFeeder:
    """Feeds AudioEngine's live mix into a FIFO that the streaming ffmpeg
    process reads as its second input.

    push() runs on the realtime audio callback thread and must never block
    it, so it only ever enqueues — a background writer thread owns the
    actual FIFO write, opened non-blocking (see _open_for_write) so it can
    neither hang waiting for ffmpeg to attach nor stall on a full pipe;
    either way it just drops audio blocks instead of blocking the writer
    thread indefinitely.
    """

    def __init__(self, fifo_path: Path, sample_rate: int, channels: int, buffer_seconds: float = 2.0) -> None:
        self.fifo_path = fifo_path
        self.sample_rate = sample_rate
        self.channels = channels
        blocks_per_second = 20  # generous upper bound; actual block size varies with buffer_size
        self._queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=max(8, int(blocks_per_second * buffer_seconds)))
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self.fifo_path.unlink(missing_ok=True)
        os.mkfifo(self.fifo_path)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def push(self, block: np.ndarray) -> None:
        try:
            self._queue.put_nowait(np.ascontiguousarray(block, dtype=np.float32))
        except queue.Full:
            pass  # ffmpeg isn't keeping up; drop rather than stall the audio callback

    def _run(self) -> None:
        fd = self._open_for_write()
        if fd is None:
            return
        try:
            while not self._stop_event.is_set():
                try:
                    block = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                self._write_all(fd, block.tobytes())
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _open_for_write(self, timeout: float = 10.0) -> int | None:
        """Open the FIFO write-only, non-blocking, retrying until ffmpeg
        attaches as its reader (or `timeout` elapses).

        A plain blocking O_WRONLY open would itself block until a reader
        shows up, which is exactly the hang this class exists to avoid if
        ffmpeg never starts — but the fix can't be "open O_RDWR instead"
        (a common trick for the *reading* side of a FIFO): that would make
        this thread a second reader alongside ffmpeg's own, and a FIFO
        splits each byte between whichever readers are attached rather than
        duplicating it, silently stealing half the audio into a reader
        nobody drains. O_NONBLOCK is the correct fix instead — it raises
        ENXIO immediately when no reader is attached yet rather than
        blocking, so this can retry on a short poll until ffmpeg opens its
        end, and never blocks after that either (see _write_all)."""
        deadline = time.monotonic() + timeout
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            try:
                return os.open(str(self.fifo_path), os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                self._stop_event.wait(0.05)
        return None

    def _write_all(self, fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view and not self._stop_event.is_set():
            try:
                _, writable, _ = select.select([], [fd], [], 0.5)
            except OSError:
                return
            if not writable:
                continue  # ffmpeg isn't draining the pipe right now; keep waiting on this block
            try:
                n = os.write(fd, view)
            except BlockingIOError:
                continue  # lost the race between select() and write(); just retry
            except OSError:
                return  # reader (ffmpeg) gone
            view = view[n:]

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self.fifo_path.unlink(missing_ok=True)
