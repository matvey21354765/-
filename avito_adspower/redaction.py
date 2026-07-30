from __future__ import annotations

import re
from typing import Any

_SECRET_KEYS = {
    "authorization",
    "adspower_api_key",
    "avito_worker_token",
    "cookie",
    "cookies",
    "localstorage",
    "cdp_endpoint",
}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if key.lower() in _SECRET_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(
            r"(?i)bearer\s+\S+",
            "Bearer ***",
            value,
        )
        value = re.sub(
            r"ws://[^/\s]+/devtools/browser/\S+",
            "ws://***/devtools/browser/***",
            value,
        )
    return value
