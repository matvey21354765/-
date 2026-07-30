"""Isolated and secret-safe Auto.ru transport selection."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit


def get_autoru_transport() -> dict[str, Any]:
    """Use only AUTORU_PROXY_URL when set, otherwise connect directly."""

    proxy = os.getenv("AUTORU_PROXY_URL", "").strip()
    if proxy:
        return {"mode": "proxy", "proxy_url": proxy}
    return {"mode": "direct", "proxy_url": None}


def autoru_proxies() -> dict[str, str] | None:
    proxy_url = get_autoru_transport()["proxy_url"]
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def autoru_timeout() -> float:
    try:
        return max(1.0, float(os.getenv("AUTORU_TIMEOUT_SECONDS", "15")))
    except (TypeError, ValueError):
        return 15.0


def proxy_host_safe() -> str:
    proxy_url = get_autoru_transport()["proxy_url"]
    if not proxy_url:
        return ""
    try:
        parsed = urlsplit(proxy_url)
        if not parsed.hostname:
            return ""
        return (
            f"{parsed.hostname}:{parsed.port}"
            if parsed.port
            else parsed.hostname
        )
    except (TypeError, ValueError):
        return ""


def autoru_captcha_detected(
    status_code: int | None,
    final_url: str,
    html: str,
) -> bool:
    text = (html or "").lower()
    return bool(
        status_code in {403, 429}
        or "/showcaptcha" in (final_url or "").lower()
        or "showcaptcha" in text
        or "проверка, что вы не робот" in text
        or "captcha" in text
        or "smartcaptcha" in text
    )


def autoru_items_from_result(result: Any) -> list[dict]:
    """Coerce supported Auto.ru result shapes to the production list contract."""

    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        items = result.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []
