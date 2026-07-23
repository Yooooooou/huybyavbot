"""Meme audio bot: on a random schedule, cut a short clip from a YouTube
meme-sound video and post it to a chat as a Telegram voice message.

Runs by itself on a timer; there are no user commands.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

import audio_utils
import youtube_utils

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("memebot")

load_dotenv()

SENT_LOG_FILE = Path(__file__).with_name("sent_log.json")

# --- Config ---------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

MIN_INTERVAL_MINUTES = int(os.getenv("MIN_INTERVAL_MINUTES", "60"))
MAX_INTERVAL_MINUTES = int(os.getenv("MAX_INTERVAL_MINUTES", "240"))
QUIET_START_HOUR = int(os.getenv("QUIET_START_HOUR", "22"))
QUIET_END_HOUR = int(os.getenv("QUIET_END_HOUR", "9"))
MIN_CLIP_SECONDS = float(os.getenv("MIN_CLIP_SECONDS", "5"))
MAX_CLIP_SECONDS = float(os.getenv("MAX_CLIP_SECONDS", "15"))
SEND_ON_STARTUP = os.getenv("SEND_ON_STARTUP", "false").strip().lower() == "true"

# Don't cut from the very start/end of a track (intros, outros).
EDGE_MARGIN_SECONDS = 5.0
# Two clips are "the same" if their start times are within this many seconds.
DEDUP_WINDOW_SECONDS = 3.0
# How hard to try before giving up on one scheduled post.
MAX_ATTEMPTS = 8


# --- Dedup log ------------------------------------------------------------
def load_sent_log() -> list[dict]:
    if not SENT_LOG_FILE.exists():
        return []
    try:
        with open(SENT_LOG_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read sent log (%s); starting fresh", exc)
        return []


def append_sent_log(video_id: str, start_sec: float) -> None:
    entries = load_sent_log()
    entries.append({"video_id": video_id, "start_sec": round(start_sec, 1)})
    tmp = SENT_LOG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False, indent=2)
    tmp.replace(SENT_LOG_FILE)


def already_sent(video_id: str, start_sec: float, entries: list[dict]) -> bool:
    for e in entries:
        if e.get("video_id") == video_id:
            if abs(float(e.get("start_sec", -999)) - start_sec) <= DEDUP_WINDOW_SECONDS:
                return True
    return False


# --- Quiet hours ----------------------------------------------------------
def in_quiet_hours(when: datetime) -> bool:
    """True if ``when`` falls inside the quiet window (handles overnight wrap)."""
    if QUIET_START_HOUR == QUIET_END_HOUR:
        return False
    h = when.hour
    if QUIET_START_HOUR < QUIET_END_HOUR:
        return QUIET_START_HOUR <= h < QUIET_END_HOUR
    # Wraps past midnight, e.g. 22 -> 9.
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


def next_run_time() -> datetime:
    """Pick a random future time, pushed out of quiet hours if it lands there."""
    minutes = random.randint(MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES)
    when = datetime.now() + timedelta(minutes=minutes)
    # If inside quiet hours, move to the end of the quiet window.
    guard = 0
    while in_quiet_hours(when) and guard < 48:
        when = when.replace(minute=random.randint(0, 59), second=0, microsecond=0)
        when = when.replace(hour=QUIET_END_HOUR)
        if when <= datetime.now():
            when += timedelta(days=1)
        guard += 1
    return when


# --- Core job -------------------------------------------------------------
def _choose_start(duration: float) -> float | None:
    """Random start point that keeps a full clip inside the safe region."""
    clip_len = random.uniform(MIN_CLIP_SECONDS, MAX_CLIP_SECONDS)
    latest_start = duration - EDGE_MARGIN_SECONDS - clip_len
    if latest_start <= EDGE_MARGIN_SECONDS:
        return None  # track too short for a safe clip
    start = random.uniform(EDGE_MARGIN_SECONDS, latest_start)
    return start


async def produce_and_send(bot: Bot) -> bool:
    """Run one full pipeline: download -> cut -> convert -> send. Returns success."""
    sent = load_sent_log()
    failed_videos: set[str] = set()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        video_id = youtube_utils.pick_video_id(exclude=failed_videos)
        log.info("attempt %d/%d: video %s", attempt, MAX_ATTEMPTS, video_id)

        with tempfile.TemporaryDirectory(prefix="memebot_") as tmpdir:
            tmp = Path(tmpdir)
            try:
                full = youtube_utils.download_audio(video_id, tmp)
            except youtube_utils.DownloadError as exc:
                log.warning("download failed: %s", exc)
                failed_videos.add(video_id)
                continue

            try:
                duration = audio_utils.get_duration(full)
            except audio_utils.AudioError as exc:
                log.warning("probe failed: %s", exc)
                failed_videos.add(video_id)
                continue

            # Try a few start points on this video before moving on.
            start = None
            clip_len = random.uniform(MIN_CLIP_SECONDS, MAX_CLIP_SECONDS)
            for _ in range(6):
                candidate = _choose_start(duration)
                if candidate is None:
                    break
                if not already_sent(video_id, candidate, sent):
                    start = candidate
                    break
            if start is None:
                log.info("no fresh start point on %s (dur %.1fs)", video_id, duration)
                failed_videos.add(video_id)
                continue

            clip_path = tmp / "clip.mp3"
            voice_path = tmp / "clip.ogg"
            try:
                audio_utils.cut_segment(full, clip_path, start, clip_len)
                audio_utils.to_voice(clip_path, voice_path)
            except audio_utils.AudioError as exc:
                log.warning("audio processing failed: %s", exc)
                failed_videos.add(video_id)
                continue

            try:
                await bot.send_voice(chat_id=CHAT_ID, voice=FSInputFile(voice_path))
            except Exception as exc:  # network/telegram errors
                log.error("send_voice failed: %s", exc)
                return False

            append_sent_log(video_id, start)
            log.info(
                "sent clip: video=%s start=%.1fs len=%.1fs", video_id, start, clip_len
            )
            return True  # tempdir (and all files) cleaned up on context exit

    log.warning("gave up after %d attempts", MAX_ATTEMPTS)
    return False


# --- Scheduling -----------------------------------------------------------
def schedule_next(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    run_at = next_run_time()
    scheduler.add_job(
        run_job,
        "date",
        run_date=run_at,
        args=[scheduler, bot],
        id="meme_job",
        replace_existing=True,
    )
    log.info("next post scheduled for %s", run_at.strftime("%Y-%m-%d %H:%M"))


async def run_job(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    try:
        await produce_and_send(bot)
    except Exception:  # never let a job crash kill the scheduler
        log.exception("unexpected error in job")
    finally:
        schedule_next(scheduler, bot)


async def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN and CHAT_ID must be set (see .env.example)")

    bot = Bot(token=BOT_TOKEN)
    scheduler = AsyncIOScheduler()
    scheduler.start()

    if SEND_ON_STARTUP:
        log.info("SEND_ON_STARTUP is on: posting one clip now")
        await produce_and_send(bot)

    schedule_next(scheduler, bot)
    log.info("memebot running; press Ctrl+C to stop")

    try:
        await asyncio.Event().wait()  # run forever
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
