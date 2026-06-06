"""TikTok Content Posting API v2 integration."""
from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Optional

import aiohttp

from config.settings import settings

logger = logging.getLogger(__name__)

_BASE = "https://open.tiktokapis.com/v2"
_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB per chunk
_TIMEOUT = aiohttp.ClientTimeout(total=120)


# ── helpers ────────────────────────────────────────────────────────────────────

def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.TIKTOK_ACCESS_TOKEN}",
        "Content-Type": "application/json; charset=UTF-8",
    }


async def _init_upload(file_size: int, title: str) -> Optional[dict]:
    """
    Call /post/publish/video/init/ and return the response body or None.
    Docs: https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
    """
    chunk_count = math.ceil(file_size / _CHUNK_SIZE)
    payload = {
        "post_info": {
            "title": title[:150],
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": _CHUNK_SIZE,
            "total_chunk_count": chunk_count,
        },
    }
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                f"{_BASE}/post/publish/video/init/",
                headers=_auth_headers(),
                json=payload,
            ) as resp:
                data = await resp.json()
                if resp.status != 200 or data.get("error", {}).get("code") != "ok":
                    logger.error(f"TikTok init error {resp.status}: {data}")
                    return None
                return data.get("data", {})
    except Exception as e:
        logger.error(f"TikTok init request failed: {e}")
        return None


async def _upload_chunks(upload_url: str, video_path: str, file_size: int) -> bool:
    """Upload video in chunks via PUT requests."""
    chunk_count = math.ceil(file_size / _CHUNK_SIZE)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
            with open(video_path, "rb") as f:
                for idx in range(chunk_count):
                    chunk = f.read(_CHUNK_SIZE)
                    start = idx * _CHUNK_SIZE
                    end = start + len(chunk) - 1
                    headers = {
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(chunk)),
                    }
                    async with session.put(upload_url, data=chunk, headers=headers) as resp:
                        if resp.status not in (200, 201, 206):
                            body = await resp.text()
                            logger.error(f"Chunk {idx} upload failed {resp.status}: {body}")
                            return False
                        logger.debug(f"Chunk {idx+1}/{chunk_count} uploaded")
        return True
    except Exception as e:
        logger.error(f"TikTok chunk upload error: {e}")
        return False


async def _check_status(publish_id: str) -> str:
    """Poll publish status. Returns 'PUBLISH_COMPLETE', 'FAILED', or 'PROCESSING'."""
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                f"{_BASE}/post/publish/status/fetch/",
                headers=_auth_headers(),
                json={"publish_id": publish_id},
            ) as resp:
                data = await resp.json()
                status = data.get("data", {}).get("status", "UNKNOWN")
                return status
    except Exception as e:
        logger.error(f"TikTok status check failed: {e}")
        return "UNKNOWN"


# ── public API ─────────────────────────────────────────────────────────────────

async def upload_video_to_tiktok(video_path: str, title: str) -> bool:
    """
    Full upload flow: init → chunk upload → poll status.
    Returns True on success.
    """
    if not settings.TIKTOK_ENABLED:
        logger.info("TikTok upload skipped (TIKTOK_ENABLED=false)")
        return False
    if not settings.TIKTOK_ACCESS_TOKEN:
        logger.warning("TIKTOK_ACCESS_TOKEN not set")
        return False

    file_size = os.path.getsize(video_path)
    logger.info(f"TikTok upload: {video_path} ({file_size/1024/1024:.1f} MB)")

    init_data = await _init_upload(file_size, title)
    if not init_data:
        return False

    publish_id = init_data.get("publish_id")
    upload_url = init_data.get("upload_url")
    if not publish_id or not upload_url:
        logger.error(f"Missing publish_id/upload_url in TikTok response: {init_data}")
        return False

    ok = await _upload_chunks(upload_url, video_path, file_size)
    if not ok:
        return False

    # Poll for completion (max 3 minutes)
    for attempt in range(18):
        await asyncio.sleep(10)
        status = await _check_status(publish_id)
        logger.info(f"TikTok publish status [{attempt+1}]: {status}")
        if status == "PUBLISH_COMPLETE":
            logger.info(f"TikTok video published! publish_id={publish_id}")
            return True
        if status in ("FAILED", "SPAM_RISK_TOO_MANY_POSTS", "SPAM_RISK_USER_BANNED_FROM_POSTING"):
            logger.error(f"TikTok publish failed with status: {status}")
            return False

    logger.warning(f"TikTok publish timed out for publish_id={publish_id}")
    return False


async def post_news_video_to_tiktok(news_items: list[dict]) -> bool:
    """Generate a news video and upload it to TikTok."""
    from app.services.video_service import generate_news_video
    if not news_items:
        return False
    video_path = await generate_news_video(news_items)
    if not video_path:
        logger.warning("News video generation failed, skipping TikTok upload")
        return False
    title = _build_news_title(news_items)
    try:
        return await upload_video_to_tiktok(video_path, title)
    finally:
        try:
            os.remove(video_path)
        except Exception:
            pass


async def post_signal_video_to_tiktok(signal) -> bool:
    """Generate a signal video and upload it to TikTok."""
    from app.services.video_service import generate_signal_video
    signal_data = {
        "coin": signal.coin,
        "direction": signal.direction,
        "entry_price": signal.entry_price or 0,
        "stop_loss": signal.stop_loss or 0,
        "take_profit_1": signal.take_profit_1 or 0,
        "confidence": signal.confidence or 0,
        "risk_reward": signal.risk_reward or 0,
        "reasons": (signal.reasons or "").split("\n")[:6],
    }
    video_path = await generate_signal_video(signal_data)
    if not video_path:
        logger.warning("Signal video generation failed, skipping TikTok upload")
        return False
    direction_emoji = "🟢" if signal.direction == "LONG" else "🔴"
    title = (
        f"{direction_emoji} {signal.coin} {signal.direction} фьючерс сигнал "
        f"| вход ${signal.entry_price:,.0f} | крипта #crypto #futures #{signal.coin.lower()}"
    )
    try:
        return await upload_video_to_tiktok(video_path, title)
    finally:
        try:
            os.remove(video_path)
        except Exception:
            pass


def _build_news_title(items: list[dict]) -> str:
    first = items[0].get("title", "Крипто новости")[:80] if items else "Крипто новости"
    return (
        f"📰 {first} | криптоновости сегодня "
        f"#crypto #bitcoin #btc #новости #крипта"
    )
