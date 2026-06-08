"""
TikTok OAuth 2.0 + automatic token refresh.

Flow:
  1. Admin calls /tiktok_setup in bot
  2. Bot sends authorization URL
  3. Admin pastes the redirect URL (contains `code=...`)
  4. Bot exchanges code → access_token + refresh_token → saved to tokens.json
  5. Before every upload the token is refreshed if it expires within 1 hour
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_TOKEN_FILE = Path(os.environ.get("TIKTOK_TOKEN_FILE", "/tmp/tiktok_tokens.json"))
_BASE = "https://open.tiktokapis.com"
_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Scopes required for video posting
_SCOPES = "user.info.basic,video.publish,video.upload"


# ── token persistence ─────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        if _TOKEN_FILE.exists():
            return json.loads(_TOKEN_FILE.read_text())
    except Exception as e:
        logger.warning(f"Failed to read token file: {e}")
    return {}


def _save(data: dict) -> None:
    try:
        _TOKEN_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.error(f"Failed to save token file: {e}")


# ── public helpers ────────────────────────────────────────────────────────────

def get_access_token() -> Optional[str]:
    return _load().get("access_token")


def get_refresh_token() -> Optional[str]:
    return _load().get("refresh_token")


def get_open_id() -> Optional[str]:
    return _load().get("open_id")


def is_token_valid() -> bool:
    data = _load()
    exp = data.get("expires_at", 0)
    return bool(data.get("access_token")) and time.time() < exp - 300


def store_tokens(access_token: str, refresh_token: str, open_id: str,
                 expires_in: int = 86400) -> None:
    _save({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "open_id": open_id,
        "expires_at": int(time.time()) + expires_in,
    })
    logger.info("TikTok tokens saved")


# ── OAuth step 1: build URL ───────────────────────────────────────────────────

def build_auth_url(client_key: str, redirect_uri: str, state: str = "tiktok") -> str:
    params = {
        "client_key": client_key,
        "scope": _SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(params)


# ── OAuth step 2: exchange code for tokens ────────────────────────────────────

async def exchange_code(client_key: str, client_secret: str,
                        code: str, redirect_uri: str) -> bool:
    payload = {
        "client_key": client_key,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.post(f"{_BASE}/v2/oauth/token/", data=payload) as resp:
                data = await resp.json()
        if data.get("error"):
            logger.error(f"TikTok OAuth error: {data}")
            return False
        store_tokens(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            open_id=data["open_id"],
            expires_in=data.get("expires_in", 86400),
        )
        return True
    except Exception as e:
        logger.error(f"TikTok code exchange failed: {e}")
        return False


# ── automatic token refresh ───────────────────────────────────────────────────

async def refresh_token_if_needed(client_key: str, client_secret: str) -> bool:
    """Refresh access token if it expires within 1 hour. Returns True if valid."""
    if is_token_valid():
        return True
    rt = get_refresh_token()
    if not rt:
        logger.warning("No TikTok refresh token available")
        return False
    payload = {
        "client_key": client_key,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": rt,
    }
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.post(f"{_BASE}/v2/oauth/token/", data=payload) as resp:
                data = await resp.json()
        if data.get("error"):
            logger.error(f"TikTok token refresh error: {data}")
            return False
        store_tokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", rt),
            open_id=data.get("open_id", get_open_id() or ""),
            expires_in=data.get("expires_in", 86400),
        )
        logger.info("TikTok access token refreshed")
        return True
    except Exception as e:
        logger.error(f"TikTok refresh request failed: {e}")
        return False
