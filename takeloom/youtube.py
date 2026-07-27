"""YouTube audio download for backing tracks, via the yt-dlp CLI binary."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .utils import sanitize_filename

YOUTUBE_URL_RE = re.compile(r"(youtube\.com/|youtu\.be/)", re.IGNORECASE)


class YouTubeDownloadError(Exception):
    """Raised when a YouTube URL can't be resolved or downloaded via yt-dlp."""


def is_youtube_url(text: str) -> bool:
    return bool(YOUTUBE_URL_RE.search(text.strip()))


def download_youtube_audio(url: str, dest_dir: Path, audio_format: str = "flac") -> tuple[Path, str, float]:
    """Download a YouTube video's audio into dest_dir via yt-dlp.

    Returns (file_path, title, duration_seconds).
    """
    if shutil.which("yt-dlp") is None:
        raise YouTubeDownloadError(
            "yt-dlp isn't installed. Install it (e.g. `brew install yt-dlp` or "
            "`pip install yt-dlp`) to add YouTube backing tracks."
        )

    try:
        info_result = subprocess.run(
            ["yt-dlp", "-j", "--no-playlist", url],
            capture_output=True, text=True, check=True,
        )
        info = json.loads(info_result.stdout)
    except subprocess.CalledProcessError as e:
        raise YouTubeDownloadError(f"Could not read video info: {e.stderr.strip() or e}") from e
    except json.JSONDecodeError as e:
        raise YouTubeDownloadError(f"Could not read video info: {e}") from e

    title = info.get("title") or info.get("id") or "Untitled"
    duration = float(info.get("duration") or 0)
    video_id = info.get("id") or "unknown"
    # yt-dlp's own %(title)s can contain characters unsafe on disk; build our
    # own filename from the sanitized title plus the video ID for uniqueness.
    safe_name = sanitize_filename(title) or "track"
    dest_stem = f"{safe_name}_{video_id}"
    output_template = str(dest_dir / f"{dest_stem}.%(ext)s")

    try:
        subprocess.run(
            [
                "yt-dlp", "--no-playlist", "-x", "--audio-format", audio_format,
                "-o", output_template, url,
            ],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        raise YouTubeDownloadError(f"Download failed: {e.stderr.strip() or e}") from e

    dest_path = dest_dir / f"{dest_stem}.{audio_format}"
    if not dest_path.exists():
        raise YouTubeDownloadError("yt-dlp finished but the expected output file is missing.")
    return dest_path, title, duration
