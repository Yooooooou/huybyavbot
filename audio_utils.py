"""ffmpeg/ffprobe helpers: probe duration, cut a segment, convert to Telegram voice."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class AudioError(RuntimeError):
    """Raised when ffmpeg/ffprobe fails or produces unusable output."""


def get_duration(path: str | Path) -> float:
    """Return the duration of an audio file in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AudioError(f"ffprobe failed: {result.stderr.strip()}")
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise AudioError(f"could not parse ffprobe output: {exc}") from exc
    if duration <= 0:
        raise AudioError(f"non-positive duration reported: {duration}")
    return duration


def cut_segment(
    src: str | Path,
    dst: str | Path,
    start_sec: float,
    duration_sec: float,
) -> Path:
    """Cut [start_sec, start_sec + duration_sec] out of ``src`` into ``dst``.

    Re-encodes (rather than stream-copy) so the cut is frame-accurate and the
    resulting file is always independently decodable.
    """
    dst = Path(dst)
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start_sec:.3f}",
            "-t",
            f"{duration_sec:.3f}",
            "-i",
            str(src),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(dst),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AudioError(f"ffmpeg cut failed: {result.stderr.strip()[-500:]}")
    if not dst.exists() or dst.stat().st_size == 0:
        raise AudioError("ffmpeg cut produced an empty file")
    return dst


def to_voice(src: str | Path, dst: str | Path) -> Path:
    """Convert an audio file into Telegram voice format: mono OGG/Opus, 48 kHz."""
    dst = Path(dst)
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            str(dst),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AudioError(f"ffmpeg opus convert failed: {result.stderr.strip()[-500:]}")
    if not dst.exists() or dst.stat().st_size == 0:
        raise AudioError("ffmpeg opus convert produced an empty file")
    return dst
