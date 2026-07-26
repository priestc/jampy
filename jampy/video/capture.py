"""VideoRecorder: continuous camera capture to disk via an ffmpeg subprocess."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _build_capture_cmd(device: str, output_path: Path, framerate: int) -> list[str]:
    if sys.platform == "darwin":
        return [
            "ffmpeg", "-y",
            "-f", "avfoundation", "-framerate", str(framerate),
            "-i", f"{device}:none",
            "-pix_fmt", "yuv420p",
            str(output_path),
        ]
    if sys.platform.startswith("linux"):
        return [
            "ffmpeg", "-y",
            "-f", "v4l2", "-framerate", str(framerate),
            "-i", device,
            "-pix_fmt", "yuv420p",
            str(output_path),
        ]
    if sys.platform.startswith("win"):
        return [
            "ffmpeg", "-y",
            "-f", "dshow", "-framerate", str(framerate),
            "-i", f"video={device}",
            "-pix_fmt", "yuv420p",
            str(output_path),
        ]
    raise RuntimeError(f"Camera capture is not supported on platform: {sys.platform}")


class VideoRecorder:
    """Captures continuous silent video from a camera to an mp4 file.

    Runs ffmpeg as a subprocess for the lifetime of the recording. stop()
    sends 'q' on ffmpeg's stdin, which it treats as a request to finish
    up and finalize the output file cleanly (rather than leaving a
    truncated moov atom behind).
    """

    def __init__(self, device: str, output_path: Path, framerate: int = 30) -> None:
        self.device = device
        self.output_path = output_path
        self.framerate = framerate
        self._proc: subprocess.Popen | None = None

    def start(self) -> bool:
        """Launch the ffmpeg capture process. Returns False if ffmpeg is unavailable."""
        if not ffmpeg_available():
            return False
        cmd = _build_capture_cmd(self.device, self.output_path, self.framerate)
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True

    def stop(self, timeout: float = 10.0) -> None:
        """Signal ffmpeg to finish and finalize the output file."""
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
            proc.wait(timeout=timeout)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


def mux_video_audio(video_path: Path, audio_path: Path, output_path: Path) -> bool:
    """Combine a silent video file with an audio file into one output file."""
    if not ffmpeg_available():
        return False
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0
