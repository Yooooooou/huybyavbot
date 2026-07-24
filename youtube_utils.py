"""YouTube side: find meme videos by search (no API key) and download audio.

Discovery is done with yt-dlp's built-in ``ytsearch`` — it scrapes YouTube
search results directly, so no YOUTUBE_API_KEY / quota is needed. Keywords
live in queries.json. An optional sources.json (curated video ids) is used as
a fallback if search yields nothing.
"""
from __future__ import annotations

import json
import logging
import os
import random
import tempfile
from pathlib import Path

import yt_dlp

log = logging.getLogger(__name__)

QUERIES_FILE = Path(__file__).with_name("queries.json")
SOURCES_FILE = Path(__file__).with_name("sources.json")

# Skip results longer than this (seconds) to avoid huge downloads / livestreams.
MAX_VIDEO_SECONDS = 20 * 60
# How many results to pull per search.
SEARCH_LIMIT = 25

# YouTube blocks datacenter IPs with a "confirm you're not a bot" check.
# Spoofing the player client sometimes gets around it without cookies; a
# cookies file is the reliable fallback. Both are configurable via env.
PLAYER_CLIENTS = [
    c.strip()
    for c in os.getenv("YTDLP_PLAYER_CLIENT", "tv,mweb,web_safari,android_vr").split(",")
    if c.strip()
]
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()

# Easiest cookies path for Railway: paste the whole cookies.txt (Netscape
# format) into the YOUTUBE_COOKIES env var. We write it to a temp file at
# startup and hand that to yt-dlp. An explicit COOKIES_FILE path wins if set.
_COOKIES_ENV = os.getenv("YOUTUBE_COOKIES", "")
if not COOKIES_FILE and _COOKIES_ENV.strip():
    _cookie_path = Path(tempfile.gettempdir()) / "yt_cookies.txt"
    try:
        _cookie_path.write_text(_COOKIES_ENV)
        COOKIES_FILE = str(_cookie_path)
        log.info("using YouTube cookies from YOUTUBE_COOKIES env")
    except OSError as exc:
        log.warning("could not write cookies from env: %s", exc)


def _base_ydl_opts() -> dict:
    """Common yt-dlp options, incl. anti-bot-check tweaks and optional cookies."""
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": PLAYER_CLIENTS}},
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


class DownloadError(RuntimeError):
    """Raised when yt-dlp fails to produce an audio file."""


# --- Discovery ------------------------------------------------------------
def load_queries(path: str | Path = QUERIES_FILE) -> list[str]:
    """Read the meme search keywords from queries.json."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    qs = data.get("queries", []) if isinstance(data, dict) else data
    return [str(q).strip() for q in qs if str(q).strip()]


def load_source_ids(path: str | Path = SOURCES_FILE) -> list[str]:
    """Read the optional curated fallback list of video ids from sources.json."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    ids = data.get("video_ids", []) if isinstance(data, dict) else data
    return [str(v).strip() for v in ids if str(v).strip()]


def search_video_ids(query: str, limit: int = SEARCH_LIMIT) -> list[str]:
    """Search YouTube for ``query`` via yt-dlp and return candidate video ids.

    Uses a flat extraction (metadata only, no download). Filters out overly
    long videos and livestreams when that info is available.
    """
    opts = _base_ydl_opts()
    opts.update({"extract_flat": True, "skip_download": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    except Exception as exc:
        log.warning("search failed for %r: %s", query, exc)
        return []

    ids: list[str] = []
    for entry in (info or {}).get("entries", []) or []:
        if not entry:
            continue
        vid = entry.get("id")
        if not vid:
            continue
        if entry.get("live_status") in {"is_live", "is_upcoming"}:
            continue
        dur = entry.get("duration")
        if isinstance(dur, (int, float)) and dur > MAX_VIDEO_SECONDS:
            continue
        ids.append(vid)
    return ids


def find_video_id(exclude: set[str] | None = None) -> str:
    """Find one meme video id: search a random keyword, fall back to sources.json.

    Raises DownloadError if nothing usable is found.
    """
    exclude = exclude or set()

    queries = load_queries()
    random.shuffle(queries)
    for query in queries:
        ids = search_video_ids(query)
        random.shuffle(ids)
        fresh = [v for v in ids if v not in exclude]
        if fresh:
            log.info("found %d candidates for %r, picking one", len(fresh), query)
            return random.choice(fresh)

    # Fallback: curated list, if present.
    fallback = [v for v in load_source_ids() if v not in exclude]
    if fallback:
        log.info("search empty; using sources.json fallback")
        return random.choice(fallback)

    raise DownloadError("no video found via search or sources.json")


# --- Download -------------------------------------------------------------
def download_audio(video_id: str, dst_dir: str | Path) -> Path:
    """Download the audio track of ``video_id`` as an mp3 into ``dst_dir``.

    Returns the path to the downloaded file. Raises DownloadError on failure.
    """
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(dst_dir / f"{video_id}.%(ext)s")

    ydl_opts = _base_ydl_opts()
    ydl_opts.update(
        {
            "format": "bestaudio/best",
            "outtmpl": out_template,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "128",
                }
            ],
        }
    )

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
