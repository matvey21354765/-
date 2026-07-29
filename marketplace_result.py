from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MarketplaceResult:
    items: list[dict] = field(default_factory=list)
    status: str = "empty"
    http: int | None = None
    error_class: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


STATUS_TEXT = {
    "proxy_auth": "ошибка прокси 407",
    "proxy_denied": "прокси отклонил соединение",
    "blocked": "доступ ограничен",
    "rate_limited": "временное ограничение",
    "network_error": "прокси недоступен",
    "parse_error": "ошибка разбора",
}


def classify_network_error(exc: BaseException) -> tuple[str, str]:
    text = str(exc).lower()
    if "407" in text or "proxy authentication" in text:
        return "proxy_auth", "ProxyAuthenticationError"
    if "403" in text and ("connect" in text or "tunnel" in text or "proxy" in text):
        return "proxy_denied", "ProxyAccessDeniedError"
    return "network_error", "ProxyConnectionError"
