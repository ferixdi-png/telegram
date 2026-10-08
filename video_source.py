"""Download the unmodified video asset from a public GitHub Release once per Render instance."""
import asyncio
import os
import tempfile
import urllib.request
from pathlib import Path

DEFAULT_VIDEO_URL = (
    "https://github.com/ferixdi-png/telegram/releases/download/"
    "video-v1/ferixdi-process.mp4"
)
VIDEO_SOURCE_URL = os.getenv("VIDEO_SOURCE_URL", DEFAULT_VIDEO_URL)
FILE_PATH = Path(tempfile.gettempdir()) / "ferixdi-process-original.mp4"
DOWNLOAD_LOCK = asyncio.Lock()
MAX_VIDEO_BYTES = 49_000_000


def _download_video_sync() -> Path:
    if FILE_PATH.is_file() and 1_000_000 < FILE_PATH.stat().st_size < MAX_VIDEO_BYTES:
        return FILE_PATH

    tmp = FILE_PATH.with_suffix(".partial")
    try:
        req = urllib.request.Request(
            VIDEO_SOURCE_URL, headers={"User-Agent": "Ferixdi-Telegram-Bot/1.0"}
        )
        total = 0
        with urllib.request.urlopen(req, timeout=90) as response, tmp.open("wb") as dst:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise ValueError("Video exceeds standard Telegram Bot API upload limit")
                dst.write(chunk)
        if total < 1_000_000:
            raise ValueError("GitHub Release video is missing or too small")
        with tmp.open("rb") as f:
            header = f.read(12)
        if header[4:8] != b"ftyp":
            raise ValueError("Downloaded asset is not an MP4 video")
        os.replace(tmp, FILE_PATH)
        return FILE_PATH
    finally:
        tmp.unlink(missing_ok=True)


async def get_original_video() -> Path:
    async with DOWNLOAD_LOCK:
        return await asyncio.to_thread(_download_video_sync)
