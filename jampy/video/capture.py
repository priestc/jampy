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


def format_watermark_text(musician: str, instrument: str, when: str, backing_track: str) -> str:
    """Build the small watermark line burned into the bottom of the video."""
    parts = [p for p in (musician, instrument, when, backing_track) if p]
    return "   •   ".join(parts)


def probe_video_size(path: Path) -> tuple[int, int]:
    """Return (width, height) of a video file's first video stream."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
            str(path),
        ],
        capture_output=True, text=True,
    )
    width_str, height_str = result.stdout.strip().split("x")
    return int(width_str), int(height_str)


def _load_watermark_font(size: int):
    """Find a usable TrueType font across platforms, falling back to PIL's built-in bitmap font."""
    from PIL import ImageFont

    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def render_watermark_image(text: str, video_width: int, video_height: int, out_path: Path) -> None:
    """Render a small translucent watermark bar, sized to the video's width."""
    from PIL import Image, ImageDraw

    bar_height = max(28, video_height // 24)
    font_size = max(13, bar_height - 12)
    font = _load_watermark_font(font_size)

    image = Image.new("RGBA", (video_width, bar_height), (0, 0, 0, 110))
    draw = ImageDraw.Draw(image)
    draw.text((14, bar_height // 2), text, font=font, fill=(255, 255, 255, 235), anchor="lm")
    image.save(out_path)


def mux_video_audio(
    video_path: Path,
    mix_audio_path: Path,
    instrument_audio_path: Path,
    output_path: Path,
    watermark_text: str | None = None,
) -> bool:
    """Combine a silent video file with two audio tracks: a compressed mix
    (backing track + instrument, for easy playback/sharing) and a lossless
    instrument-only track (for anyone who wants to remix or isolate it).

    If watermark_text is given, burns it into a small translucent bar along
    the bottom of the video. Rendered as a PNG overlay via Pillow rather than
    ffmpeg's drawtext filter, since drawtext requires ffmpeg to be built with
    libfreetype, which many default/minimal ffmpeg builds omit.
    """
    if not ffmpeg_available():
        return False

    watermark_png: Path | None = None
    if watermark_text:
        try:
            width, height = probe_video_size(video_path)
            watermark_png = output_path.with_name(output_path.stem + "_watermark.png")
            render_watermark_image(watermark_text, width, height, watermark_png)
        except Exception:
            watermark_png = None

    cmd = ["ffmpeg", "-y", "-i", str(video_path), "-i", str(mix_audio_path), "-i", str(instrument_audio_path)]
    if watermark_png is not None:
        cmd += [
            "-i", str(watermark_png),
            "-filter_complex", "[0:v][3:v]overlay=0:main_h-overlay_h[outv]",
            "-map", "[outv]", "-map", "1:a", "-map", "2:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        ]
    else:
        cmd += ["-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "copy"]
    cmd += [
        "-c:a:0", "aac",
        "-c:a:1", "flac",
        "-metadata:s:a:0", "title=Mix",
        "-metadata:s:a:1", "title=Instrument Only",
        "-shortest",
        "-movflags", "+faststart",
        str(output_path),
    ]

    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if watermark_png is not None:
        watermark_png.unlink(missing_ok=True)
    return result.returncode == 0
