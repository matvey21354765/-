"""Isolated and secret-safe Auto.ru transport selection."""

from __future__ import annotations

import os
from typing import Any


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
