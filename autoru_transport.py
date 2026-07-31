"""Isolated and secret-safe Auto.ru transport selection."""

from __future__ import annotations

import os
import inspect
from typing import Any, TypedDict
from urllib.parse import urlsplit


class AutoRuResult(TypedDict):
    items: list[dict]
    error: str | None
    meta: dict[str, Any]


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


def normalize_autoru_result(
    result: Any,
    *,
    default_meta: dict[str, Any] | None = None,
) -> AutoRuResult:
    """Normalize current and legacy Auto.ru values to one strict contract."""

    meta = dict(default_meta or {})
    error: str | None = None
    items_value: Any = None

    if isinstance(result, dict):
        items_value = result.get("items", result.get("results"))
        raw_meta = result.get("meta")
        if isinstance(raw_meta, dict):
            meta.update(raw_meta)
        raw_error = result.get("error")
        error = str(raw_error) if raw_error else None
    elif isinstance(result, list):
        items_value = result
    elif isinstance(result, tuple):
        first = result[0] if result else None
        if isinstance(first, dict):
            return normalize_autoru_result(first, default_meta=meta)
        items_value = first
    elif result is None:
        error = "no_result"
    else:
        error = f"unsupported_result_type:{type(result).__name__}"

    if isinstance(items_value, list):
        items = [item for item in items_value if isinstance(item, dict)]
    else:
        items = []
        if error is None:
            error = f"unsupported_items_type:{type(items_value).__name__}"

    return {"items": items, "error": error, "meta": meta}


async def await_autoru_result(result: Any) -> AutoRuResult:
    """Await an accidental coroutine before applying the strict contract."""

    if inspect.isawaitable(result):
        result = await result
    return normalize_autoru_result(result)


def autoru_items_from_result(result: Any) -> list[dict]:
    """Compatibility accessor for callers that only need listings."""

    return normalize_autoru_result(result)["items"]
