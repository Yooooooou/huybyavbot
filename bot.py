"""Meme audio bot.

Works in every chat it's added to: it remembers each group/chat automatically,
posts a random YouTube meme clip (as a Telegram voice message) on a random
schedule to all of them, and also sends one on demand via the /meme command.

Only BOT_TOKEN is required. No CHAT_ID, no manual chat setup.
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

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, FSInputFile, Message
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
CHATS_FILE = Path(__file__).with_name("chats.json")

# --- Config ---------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

MIN_INTERVAL_MINUTES = int(os.getenv("MIN_INTERVAL_MINUTES", "60"))
MAX_INTERVAL_MINUTES = int(os.getenv("MAX_INTERVAL_MINUTES", "240"))
QUIET_START_HOUR = int(os.getenv("QUIET_START_HOUR", "22"))
QUIET_END_HOUR = int(os.getenv("QUIET_END_HOUR", "9"))
MIN_CLIP_SECONDS = float(os.getenv("MIN_CLIP_SECONDS", "5"))
MAX_CLIP_SECONDS = float(os.getenv("MAX_CLIP_SECONDS", "15"))

# Don't cut from the very start/end of a track (intros, outros).
EDGE_MARGIN_SECONDS = 5.0
# Two clips are "the same" if their start times are within this many seconds.
DEDUP_WINDOW_SECONDS = 3.0
# How hard to try before giving up on one post.
MAX_ATTEMPTS = 8

dp = Dispatcher()
# Serialize the heavy pipeline so a /meme and a scheduled run don't overlap.
PIPELINE_LOCK = asyncio.Lock()


# --- Chat registry --------------------------------------------------------
def load_chats() -> list[int]:
    if not CHATS_FILE.exists():
        return []
    try:
        with open(CHATS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return [int(c) for c in data] if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        log.warning("could not read chats file (%s); starting fresh", exc)
        return []


def _save_chats(chats: list[int]) -> None:
    tmp = CHATS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(chats, fh)
    tmp.replace(CHATS_FILE)


def add_chat(chat_id: int) -> None:
    chats = load_chats()
    if chat_id not in chats:
        chats.append(chat_id)
        _save_chats(chats)
        log.info("registered chat %s (now %d total)", chat_id, len(chats))


def remove_chat(chat_id: int) -> None:
    chats = load_chats()
    if chat_id in chats:
        chats.remove(chat_id)
        _save_chats(chats)
        log.info("removed chat %s (now %d total)", chat_id, len(chats))


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
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


def next_run_time() -> datetime:
    """Pick a random future time, pushed out of quiet hours if it lands there."""
    minutes = random.randint(MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES)
    when = datetime.now() + timedelta(minutes=minutes)
    guard = 0
    while in_quiet_hours(when) and guard < 48:
        when = when.replace(
            hour=QUIET_END_HOUR, minute=random.randint(0, 59), second=0, microsecond=0
        )
        if when <= datetime.now():
            when += timedelta(days=1)
        guard += 1
    return when


# --- Clip pipeline (blocking; run in a thread) ----------------------------
def _choose_start(duration: float) -> tuple[float, float] | None:
    """Return (start, clip_len) that keeps a full clip inside the safe region."""
    clip_len = random.uniform(MIN_CLIP_SECONDS, MAX_CLIP_SECONDS)
    latest_start = duration - EDGE_MARGIN_SECONDS - clip_len
    if latest_start <= EDGE_MARGIN_SECONDS:
        return None
    return random.uniform(EDGE_MARGIN_SECONDS, latest_start), clip_len


def _build_clip(tmp: Path) -> tuple[str, float, float, Path] | None:
    """Download -> cut -> convert. Returns (video_id, start, clip_len, voice_path).

    Blocking (subprocess + network); call via asyncio.to_thread. Returns None
    if no usable, non-duplicate clip could be produced.
    """
    sent = load_sent_log()
    failed_videos: set[str] = set()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            video_id = youtube_utils.find_video_id(exclude=failed_videos)
        except youtube_utils.DownloadError as exc:
            log.warning("could not find a video: %s", exc)
            return None
        log.info("attempt %d/%d: video %s", attempt, MAX_ATTEMPTS, video_id)

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

        start = clip_len = None
        for _ in range(6):
            chosen = _choose_start(duration)
            if chosen is None:
                break
            cand_start, cand_len = chosen
            if not already_sent(video_id, cand_start, sent):
                start, clip_len = cand_start, cand_len
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

        # Free the full download early; keep only the small voice clip.
        try:
            full.unlink(missing_ok=True)
            clip_path.unlink(missing_ok=True)
        except OSError:
            pass
        return video_id, start, clip_len, voice_path

    log.warning("gave up after %d attempts", MAX_ATTEMPTS)
    return None


async def make_and_send(bot: Bot, chat_ids: list[int]) -> bool:
    """Build one clip and send it to each chat in ``chat_ids``. Returns success.

    The same clip is broadcast to all chats; it's logged once for dedup.
    Chats that reject the bot (blocked/kicked) are dropped from the registry.
    """
    if not chat_ids:
        log.info("no chats registered; nothing to send")
        return False

    async with PIPELINE_LOCK:
        with tempfile.TemporaryDirectory(prefix="memebot_") as tmpdir:
            clip = await asyncio.to_thread(_build_clip, Path(tmpdir))
            if clip is None:
                return False
            video_id, start, clip_len, voice_path = clip

            sent_any = False
            for cid in chat_ids:
                try:
                    await bot.send_voice(chat_id=cid, voice=FSInputFile(voice_path))
                    sent_any = True
                except TelegramForbiddenError:
                    log.info("chat %s forbade the bot; removing", cid)
                    remove_chat(cid)
                except Exception as exc:
                    log.error("send_voice to %s failed: %s", cid, exc)

            if sent_any:
                append_sent_log(video_id, start)
                log.info(
                    "broadcast clip video=%s start=%.1fs len=%.1fs to %d chat(s)",
                    video_id,
                    start,
                    clip_len,
                    len(chat_ids),
                )
            return sent_any


# --- Handlers -------------------------------------------------------------
@dp.my_chat_member()
async def on_membership_change(update: ChatMemberUpdated) -> None:
    """Register/unregister a chat when the bot is added to or removed from it."""
    status = update.new_chat_member.status
    if status in {"member", "administrator", "creator"}:
        add_chat(update.chat.id)
    else:  # left, kicked, restricted
        remove_chat(update.chat.id)


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    add_chat(message.chat.id)
    await message.answer(
        "Привет! Я буду присылать мемные голосовые в этот чат по расписанию.\n"
        "Команда /meme — прислать мем прямо сейчас (проверить, что всё работает)."
    )


@dp.message(Command("meme"))
async def cmd_meme(message: Message) -> None:
    add_chat(message.chat.id)
    note = await message.answer("Секу мемчик... 🎧")
    ok = await make_and_send(message.bot, [message.chat.id])
    if not ok:
        await message.answer(
            "Не получилось достать клип (YouTube/yt-dlp могли подвести). Попробуй ещё раз."
        )
    try:
        await note.delete()
    except Exception:
        pass


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
    log.info("next broadcast scheduled for %s", run_at.strftime("%Y-%m-%d %H:%M"))


async def run_job(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    try:
        await make_and_send(bot, load_chats())
    except Exception:
        log.exception("unexpected error in job")
    finally:
        schedule_next(scheduler, bot)


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN must be set (see .env.example)")

    bot = Bot(token=BOT_TOKEN)
    scheduler = AsyncIOScheduler()
    scheduler.start()
    schedule_next(scheduler, bot)
    log.info("memebot running; add me to a chat and/or send /meme")

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
