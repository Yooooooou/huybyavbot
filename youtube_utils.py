"""YouTube side: load the source list and download an audio track with yt-dlp."""
from __future__ import annotations

import json
import logging
import random
from pathlib import Path

import yt_dlp

log = logging.getLogger(__name__)

SOURCES_FILE = Path(__file__).with_name("sources.json")


class DownloadError(RuntimeError):
    """Raised when yt-dlp fails to produce an audio file."""


def load_video_ids(path: str | Path = SOURCES_FILE) -> list[str]:
    """Read the curated list of YouTube video IDs from sources.json."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    ids = data.get("video_ids", []) if isinstance(data, dict) else data
    ids = [str(v).strip() for v in ids if str(v).strip()]
    if not ids:
        raise DownloadError(f"no video ids found in {path}")
    return ids


def pick_video_id(exclude: set[str] | None = None) -> str:
    """Return a random video id, avoiding those in ``exclude`` when possible."""
    ids = load_video_ids()
    exclude = exclude or set()
    candidates = [v for v in ids if v not in exclude] or ids
    return random.choice(candidates)


def download_audio(video_id: str, dst_dir: str | Path) -> Path:
    """Download the audio track of ``video_id`` as an mp3 into ``dst_dir``.

    Returns the path to the downloaded file. Raises DownloadError on failure.
    """
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(dst_dir / f"{video_id}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
    }

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # yt-dlp raises many exception types
        raise DownloadError(f"yt-dlp failed for {video_id}: {exc}") from exc

    result = dst_dir / f"{video_id}.mp3"
    if not result.exists() or result.stat().st_size == 0:
        raise DownloadError(f"downloaded file missing/empty for {video_id}")
    return result
