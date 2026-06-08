"""
Upload videos to TikTok using browser automation (sessionid cookie).
Works without TikTok API approval.
"""
from __future__ import annotations
import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

SESSIONID = os.environ.get("TIKTOK_SESSIONID", "")


def _upload_sync(video_path: str, description: str) -> bool:
    try:
        from tiktok_uploader.upload import upload_video
        results = upload_video(
            video_path,
            description=description[:2200],
            sessionid=SESSIONID,
            headless=True,
            browser="chrome",
            browser_agent=None,
        )
        logger.info(f"tiktok-uploader result: {results}")
        return bool(results)
    except Exception as e:
        logger.error(f"tiktok-uploader error: {e}")
        return False


async def browser_upload(video_path: str, description: str) -> bool:
    if not SESSIONID:
        logger.warning("TIKTOK_SESSIONID not set")
        return False
    return await asyncio.to_thread(_upload_sync, video_path, description)
