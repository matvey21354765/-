"""TikTok Content Posting API v2 — full upload pipeline with auto token refresh."""
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
_CHUNK = 10 * 1024 * 1024   # 10 MB
_TIMEOUT = aiohttp.ClientTimeout(total=120)


# ── token-aware header builder ────────────────────────────────────────────────

async def _headers() -> Optional[dict]:
    """Return auth headers, refreshing the token if needed. None = not configured."""
    if not settings.TIKTOK_ENABLED:
        return None
    from app.services.tiktok_auth import refresh_token_if_needed, get_access_token
    ok = await refresh_token_if_needed(settings.TIKTOK_CLIENT_KEY, settings.TIKTOK_CLIENT_SECRET)
    if not ok:
        logger.warning("TikTok token unavailable")
        return None
    token = get_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
    }


# ── upload helpers ─────────────────────────────────────────────────────────────

async def _init_upload(file_size: int, title: str, hdrs: dict,
                       direct_post: bool = False) -> Optional[dict]:
    if direct_post:
        # Production: public direct post (requires video.publish scope)
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
                "chunk_size": _CHUNK,
                "total_chunk_count": math.ceil(file_size / _CHUNK),
            },
        }
        endpoint = f"{_BASE}/post/publish/video/init/"
    else:
        # Sandbox / draft inbox (requires only video.upload scope)
        payload = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": file_size,
                "total_chunk_count": 1,
            },
        }
        endpoint = f"{_BASE}/post/publish/inbox/video/init/"

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.post(endpoint, headers=hdrs, json=payload) as r:
                data = await r.json()
        if r.status != 200 or data.get("error", {}).get("code") != "ok":
            logger.error(f"TikTok init error {r.status}: {data}")
            return None
        return data.get("data", {})
    except Exception as e:
        logger.error(f"TikTok init request failed: {e}")
        return None


async def _upload_chunks(upload_url: str, path: str, file_size: int,
                         single: bool = False) -> bool:
    chunk_size = file_size if single else _CHUNK
    chunks = 1 if single else math.ceil(file_size / _CHUNK)
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300)) as s:
            with open(path, "rb") as f:
                for idx in range(chunks):
                    chunk = f.read(chunk_size)
                    start = idx * chunk_size
                    end = start + len(chunk) - 1
                    hdrs = {
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(chunk)),
                    }
                    async with s.put(upload_url, data=chunk, headers=hdrs) as r:
                        if r.status not in (200, 201, 206):
                            logger.error(f"Chunk {idx} failed {r.status}: {await r.text()}")
                            return False
                        logger.debug(f"Chunk {idx+1}/{chunks} OK")
        return True
    except Exception as e:
        logger.error(f"TikTok chunk upload error: {e}")
        return False


async def _poll_status(publish_id: str, hdrs: dict, max_wait: int = 180) -> bool:
    for _ in range(max_wait // 10):
        await asyncio.sleep(10)
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
                async with s.post(
                        f"{_BASE}/post/publish/status/fetch/",
                        headers=hdrs,
                        json={"publish_id": publish_id}) as r:
                    data = await r.json()
            status = data.get("data", {}).get("status", "UNKNOWN")
            logger.info(f"TikTok publish status: {status}")
            if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                return True
            if "FAIL" in status or "SPAM" in status or "BANNED" in status:
                logger.error(f"TikTok publish failed: {status}")
                return False
        except Exception as e:
            logger.warning(f"TikTok status poll error: {e}")
    logger.warning(f"TikTok publish timed out: {publish_id}")
    return False


# ── main upload entry point ───────────────────────────────────────────────────

async def upload_video(video_path: str, title: str) -> bool:
    hdrs = await _headers()
    if not hdrs:
        return False

    file_size = os.path.getsize(video_path)
    direct = settings.TIKTOK_DIRECT_POST
    logger.info(f"TikTok upload: {os.path.basename(video_path)} ({file_size/1024/1024:.1f} MB) direct={direct}")

    init = await _init_upload(file_size, title, hdrs, direct_post=direct)
    if not init:
        return False

    publish_id = init.get("publish_id")
    upload_url = init.get("upload_url")
    if not publish_id or not upload_url:
        logger.error(f"Bad TikTok init response: {init}")
        return False

    # Inbox upload: single chunk = full file
    if not direct:
        ok = await _upload_chunks(upload_url, video_path, file_size, single=True)
    else:
        ok = await _upload_chunks(upload_url, video_path, file_size)
    if not ok:
        return False

    success = await _poll_status(publish_id, hdrs)
    if success:
        logger.info(f"✅ TikTok video published (publish_id={publish_id})")
    return success


# ── high-level helpers ────────────────────────────────────────────────────────

def _news_title(items: list[dict]) -> str:
    first = items[0].get("title", "Крипто-новости")[:80] if items else "Крипто-новости"
    return f"📰 {first} | #crypto #bitcoin #btc #крипта #новости"


def _signal_title(signal) -> str:
    coin = getattr(signal, "coin", None) or signal.get("coin", "BTC")
    direction = getattr(signal, "direction", None) or signal.get("direction", "LONG")
    price = getattr(signal, "entry_price", None) or signal.get("entry_price", 0)
    emoji = "🟢" if direction == "LONG" else "🔴"
    return (
        f"{emoji} {coin}/USDT {direction} сигнал | вход ${price:,.0f} "
        f"#crypto #futures #{coin.lower()} #трейдинг #фьючерсы"
    )


def _term_title(title: str) -> str:
    short = title.replace("📖 Термин дня: ", "")
    return f"📖 {short} — что это такое? #крипта #обучение #трейдинг #криптовалюта"


async def post_news_video(news_items: list[dict]) -> bool:
    if not news_items:
        return False
    from app.services.video_service import generate_news_video
    path = await generate_news_video(news_items)
    if not path:
        logger.warning("News video generation failed")
        return False
    try:
        return await upload_video(path, _news_title(news_items))
    finally:
        try: os.remove(path)
        except Exception: pass


async def post_signal_video(signal) -> bool:
    from app.services.video_service import generate_signal_video
    path = await generate_signal_video(signal)
    if not path:
        logger.warning("Signal video generation failed")
        return False
    try:
        return await upload_video(path, _signal_title(signal))
    finally:
        try: os.remove(path)
        except Exception: pass


async def post_term_video(title: str, body: str) -> bool:
    from app.services.video_service import generate_term_video
    path = await generate_term_video(title, body)
    if not path:
        logger.warning("Term video generation failed")
        return False
    try:
        return await upload_video(path, _term_title(title))
    finally:
        try: os.remove(path)
        except Exception: pass
