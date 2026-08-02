"""Single Avito provider router with explicit fallback policy."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class AvitoProviderResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    provider: str = "rest_app"
    http: int | None = None
    raw_count: int = 0
    normalized_count: int = 0
    error: str = ""
    fallback_used: bool = False


FALLBACK_ERRORS = {
    "network_error", "timeout", "invalid_json", "http_401", "http_403",
    "http_429", "http_500", "http_502", "http_503", "http_504",
}


class AvitoProviderRouter:
    def __init__(
        self,
        primary: Callable[[], AvitoProviderResult],
        fallback: Callable[[], AvitoProviderResult] | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    def search(self) -> AvitoProviderResult:
        result = self.primary()
        if result.http == 200 and result.raw_count > 0 and result.normalized_count == 0:
            result.error = "normalization_failed"
            return result
        if result.error in FALLBACK_ERRORS and self.fallback is not None:
            fallback = self.fallback()
            fallback.fallback_used = True
            return fallback
        return result
