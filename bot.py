from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from telegram import InputFile, Update
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_UPLOAD_BYTES = 49_000_000       # Keep below Telegram Bot API's ~50 MB upload limit.
TARGET_OUTPUT_BYTES = 45_000_000   # Encoding target leaves room for container overhead.
MAX_RUNTIME_SECONDS = int(os.getenv("BOT_MAX_RUNTIME_SECONDS", str(5 * 60 * 60 + 45 * 60)))
MAX_CONCURRENT_DOWNLOADS = 2

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
    "www.youtu.be",
}

MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
ACTIVE_USERS: set[int] = set()
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logger = logging.getLogger("upvid")


def youtube_url_from_text(text: str | None) -> str | None:
    """Return a valid YouTube URL from a message; reject other hosts."""
    if not text:
        return None

    candidates = re.findall(r"https?://[^\s<>]+", text, flags=re.IGNORECASE)
    for candidate in candidates:
        candidate = candidate.rstrip(".,!?;:)]}»'")
        try:
            parsed = urlsplit(candidate)
            host = (parsed.hostname or "").lower()
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and host in YOUTUBE_HOSTS:
            if parsed.path and parsed.path != "/":
                return candidate
    return None


def human_size(size: int | float | None) -> str:
    if not size:
        return "noma’lum"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def download_video(url: str, workdir: Path, progress: dict) -> tuple[dict, Path]:
    """Download the best available MP4 video+audio, falling back if needed."""
    progress["phase"] = "download"
    progress["percent"] = None

    def on_progress(data: dict) -> None:
        status = data.get("status")
        if status == "downloading":
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            progress["downloaded"] = downloaded
            progress["total"] = total
            progress["percent"] = (downloaded * 100 / total) if total else None
            progress["speed"] = data.get("speed")
            progress["eta"] = data.get("eta")
        elif status == "finished":
            progress["percent"] = 100.0
            progress["phase"] = "postprocess"

    options = {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "%(title).120B-%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [on_progress],
    }

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    if not isinstance(info, dict):
        raise RuntimeError("YouTube video ma’lumotlarini olishning imkoni bo‘lmadi.")

    candidates = [
        p for p in workdir.iterdir()
        if p.is_file()
        and p.suffix.lower() in MEDIA_EXTENSIONS
        and not p.name.endswith((".part", ".ytdl"))
    ]
    if not candidates:
        raise RuntimeError("Yuklangan video fayli topilmadi.")
    media_path = max(candidates, key=lambda p: p.stat().st_size)
    return info, media_path


def ffprobe_metadata(path: Path) -> dict:
    """Read media duration and codecs with ffprobe."""
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name",
        "-of", "json", str(path),
    ]
    result = subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=60
    )
    import json
    return json.loads(result.stdout or "{}")


def media_facts(path: Path, fallback_duration: float | None = None) -> tuple[float, str | None, str | None]:
    data = ffprobe_metadata(path)
    duration = fallback_duration or 0.0
    try:
        duration = float(data.get("format", {}).get("duration") or duration)
    except (TypeError, ValueError):
        pass

    video_codec = None
    audio_codec = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and video_codec is None:
            video_codec = stream.get("codec_name")
        elif stream.get("codec_type") == "audio" and audio_codec is None:
            audio_codec = stream.get("codec_name")
    return duration, video_codec, audio_codec


def run_ffmpeg(command: list[str], timeout: int) -> bool:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        logger.warning("ffmpeg operation failed: %s", (result.stderr or "")[-1200:])
        return False
    return True


def prepare_for_telegram(
    source: Path, workdir: Path, duration_hint: float | None
) -> tuple[Path | None, bool]:
    """
    Preserve original quality whenever it fits. Otherwise make a compatible
    MP4 only when it can fit without reducing the bitrate to an unusable level.
    """
    source_size = source.stat().st_size
    duration, video_codec, audio_codec = media_facts(source, duration_hint)
    compatible = video_codec == "h264" and audio_codec in {None, "aac"}

    if source.suffix.lower() == ".mp4" and compatible and source_size <= MAX_UPLOAD_BYTES:
        return source, False

    # Remuxing H.264/AAC does not re-encode and therefore preserves quality.
    if compatible and source_size <= MAX_UPLOAD_BYTES:
        remuxed = workdir / "upvid-ready.mp4"
        remux_command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
            "-c", "copy", "-sn", "-dn", "-movflags", "+faststart",
            str(remuxed),
        ]
        if run_ffmpeg(remux_command, timeout=max(180, int((duration or 60) * 2))):
            if remuxed.exists() and remuxed.stat().st_size <= MAX_UPLOAD_BYTES:
                return remuxed, False

    if not duration or duration <= 0:
        logger.info("Cannot safely fit oversized video: duration unavailable.")
        return None, False

    total_kbps = int((TARGET_OUTPUT_BYTES * 8 / 1000 / duration) * 0.90)
    audio_kbps = 96 if total_kbps >= 650 else 64
    video_kbps = total_kbps - audio_kbps - 24  # reserve container/overhead margin

    # Avoid returning a very long video with severely degraded quality.
    if video_kbps < 300:
        return None, False

    max_side = 480 if video_kbps < 550 else 720 if video_kbps < 1200 else 1920
    output = workdir / "upvid-optimized.mp4"
    timeout = max(300, min(10_800, int(duration * 3 + 180)))

    for attempt in range(3):
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf",
            f"scale=w='min({max_side},iw)':h='min({max_side},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{video_kbps}k",
            "-maxrate", f"{int(video_kbps * 1.12)}k",
            "-bufsize", f"{video_kbps * 2}k",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "2",
            "-sn", "-dn", "-movflags", "+faststart",
            str(output),
        ]
        output.unlink(missing_ok=True)
        if not run_ffmpeg(command, timeout=timeout):
            return None, False
        if not output.exists():
            return None, False
        output_size = output.stat().st_size
        if output_size <= MAX_UPLOAD_BYTES:
            return output, True

        # Retry at a lower bitrate if the actual output exceeded the target.
        ratio = TARGET_OUTPUT_BYTES / max(output_size, 1)
        video_kbps = int(video_kbps * ratio * 0.90)
        if video_kbps < 300:
            break

    return None, False


async def edit_status(message, text: str) -> None:
    try:
        await message.edit_text(text, disable_web_page_preview=True)
    except (BadRequest, NetworkError, TimedOut):
        pass


async def show_progress(task: asyncio.Task, message, progress: dict) -> None:
    previous = None
    while not task.done():
        phase = progress.get("phase", "start")
        if phase == "download":
            percent = progress.get("percent")
            if percent is not None:
                text = f"Video yuklanmoqda: {percent:.1f}%"
            else:
                downloaded = human_size(progress.get("downloaded"))
                total = human_size(progress.get("total"))
                text = f"Video yuklanmoqda: {downloaded} / {total}"
        elif phase == "postprocess":
            text = "Video fayli yig‘ilmoqda..."
        elif phase == "prepare":
            text = "Video Telegram uchun tayyorlanmoqda..."
        else:
            text = "Havola tekshirilmoqda..."
        if text != previous:
            await edit_status(message, text)
            previous = text
        await asyncio.sleep(5)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        "Upvid — YouTube video yuklovchi bot.\n\n"
        "YouTube videosining havolasini shu chatga yuboring. "
        "Bot mavjud eng yuqori sifatni tanlaydi.\n\n"
        "Eslatma: Telegram Bot API fayl yuborishda taxminan 50 MB limitga ega. "
        "Video shu hajmga sifatni keskin tushirmasdan sig‘masa, bot asl havolani qaytaradi.\n\n"
        "Faqat yuklab olishga ruxsatingiz bor videolardan foydalaning."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        "Ishlatish: YouTube video havolasini yuboring.\n"
        "YouTube Shorts havolalari ham qo‘llab-quvvatlanadi. "
        "Pleylistlar birma-bir yuklanadi — butun pleylist avtomatik olinmaydi.\n"
        "Mavjud eng yaxshi sifat tanlanadi; Telegram hajm limiti sabab kerak bo‘lsa "
        "video moslashtiriladi."
    )


async def handle_youtube_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return

    url = youtube_url_from_text(message.text)
    if not url:
        # Ignore ordinary conversation; respond only when the user sent a URL.
        if re.search(r"https?://", message.text, flags=re.IGNORECASE):
            await message.reply_text("YouTube havolasini yuboring. Boshqa saytlar qo‘llab-quvvatlanmaydi.")
        return

    if user.id in ACTIVE_USERS:
        await message.reply_text("Sizning oldingi videongiz hali qayta ishlanmoqda. Iltimos, tugashini kuting.")
        return

    ACTIVE_USERS.add(user.id)
    status_message = await message.reply_text("YouTube havolasi tekshirilmoqda...")
    progress: dict = {"phase": "start", "percent": None}

    try:
        async with DOWNLOAD_SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="upvid-") as directory:
                workdir = Path(directory)

                download_task = asyncio.create_task(
                    asyncio.to_thread(download_video, url, workdir, progress)
                )
                progress_task = asyncio.create_task(show_progress(download_task, status_message, progress))
                try:
                    info, source = await download_task
                finally:
                    progress_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await progress_task

                title = str(info.get("title") or "YouTube video").strip()[:180]
                duration = info.get("duration")
                try:
                    duration = float(duration) if duration is not None else None
                except (TypeError, ValueError):
                    duration = None

                progress["phase"] = "prepare"
                prepare_task = asyncio.create_task(
                    asyncio.to_thread(prepare_for_telegram, source, workdir, duration)
                )
                progress_task = asyncio.create_task(show_progress(prepare_task, status_message, progress))
                try:
                    ready_path, optimized = await prepare_task
                finally:
                    progress_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await progress_task

                if ready_path is None:
                    await edit_status(
                        status_message,
                        "Video Telegram’ning fayl hajmi limitiga sifatni keskin buzmasdan sig‘madi. "
                        "Juda past sifatda yuborish o‘rniga asl havola qoldirildi:\n" + url,
                    )
                    return

                if ready_path.stat().st_size > MAX_UPLOAD_BYTES:
                    await edit_status(status_message, "Tayyor video Telegram limitidan katta chiqdi; asl havola: " + url)
                    return

                safe_title = re.sub(r'[\\/:*?"<>|\r\n]+', "_", title).strip(" ._")[:90] or "youtube-video"
                caption = f"{title}\nYuklab olish yakunlandi."
                if optimized:
                    caption += "\nTelegram hajmiga moslash uchun sifat/hajm optimallashtirildi."
                caption = caption[:1000]

                await edit_status(status_message, "Video yuborilmoqda...")
                try:
                    with ready_path.open("rb") as file_handle:
                        await message.reply_video(
                            video=InputFile(file_handle, filename=f"{safe_title}.mp4"),
                            caption=caption,
                            supports_streaming=True,
                            read_timeout=180,
                            write_timeout=240,
                            connect_timeout=30,
                            pool_timeout=30,
                        )
                except BadRequest:
                    # Some Telegram clients/API paths reject video metadata; send as a file instead.
                    with ready_path.open("rb") as file_handle:
                        await message.reply_document(
                            document=InputFile(file_handle, filename=f"{safe_title}.mp4"),
                            caption=caption[:1000],
                            read_timeout=180,
                            write_timeout=240,
                            connect_timeout=30,
                            pool_timeout=30,
                        )
                await edit_status(status_message, f"Tayyor: {title}\nHajmi: {human_size(ready_path.stat().st_size)}")
                logger.info("Delivered video to user_id=%s size=%s optimized=%s", user.id, ready_path.stat().st_size, optimized)

    except DownloadError as exc:
        logger.warning("yt-dlp download failed: %s", str(exc)[-600:])
        await edit_status(
            status_message,
            "Videoni olish muvaffaqiyatsiz tugadi. Havola ochiqligini tekshiring va qayta urinib ko‘ring. "
            "Yosh cheklovi, pullik yoki maxsus kirish talab qiladigan videolar yuklanmasligi mumkin.",
        )
    except (TimedOut, NetworkError) as exc:
        logger.warning("Network/Telegram issue: %s", type(exc).__name__)
        await edit_status(status_message, "Tarmoqda vaqtinchalik xatolik. Bir ozdan keyin qayta urinib ko‘ring.")
    except TelegramError as exc:
        logger.exception("Telegram API error")
        await edit_status(status_message, "Telegram faylni yubora olmadi. Hajm limiti yoki tarmoq muammosi bo‘lishi mumkin.")
    except Exception:
        logger.exception("Unexpected video processing failure")
        await edit_status(status_message, "Video qayta ishlanmadi. Actions bo‘limidagi logni tekshiring.")
    finally:
        ACTIVE_USERS.discard(user.id)


async def stop_after_runtime(app: Application) -> None:
    await asyncio.sleep(MAX_RUNTIME_SECONDS)
    logger.info("Stopping cleanly before the GitHub Actions job limit.")
    if app.running:
        app.stop_running()


async def post_init(app: Application) -> None:
    app.create_task(stop_after_runtime(app), name="upvid-runtime-shutdown")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN topilmadi. GitHub Settings → Secrets and variables → Actions bo‘limida "
            "BOT_TOKEN secret yarating."
        )
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("ffmpeg/ffprobe topilmadi; workflow ularni o‘rnatishi kerak.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_youtube_link))

    logger.info("Upvid bot is starting.")
    app.run_polling(
        poll_interval=1,
        timeout=30,
        bootstrap_retries=-1,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
