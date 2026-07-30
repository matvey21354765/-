from __future__ import annotations

import os
from dataclasses import dataclass

from .exceptions import ConfigurationError


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass(frozen=True)
class WorkerConfig:
    adspower_api_url: str = "http://127.0.0.1:50325"
    adspower_profile_id: str = ""
    adspower_api_key: str = ""
    require_auth: bool = True
    stop_profile_on_exit: bool = False
    max_pages: int = 2
    max_items: int = 50
    detail_mode: str = "full"
    detail_concurrency: int = 1
    profile_start_timeout: float = 30
    cdp_connect_timeout: float = 15
    navigation_timeout: float = 25
    locator_timeout: float = 15
    listing_timeout: float = 20
    search_deadline: float = 180
    retry_attempts: int = 2
    delay_min_seconds: float = 1.2
    delay_max_seconds: float = 3.5
    worker_host: str = "127.0.0.1"
    worker_port: int = 8081
    worker_token: str = ""

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        cfg = cls(
            adspower_api_url=os.getenv(
                "ADSPOWER_API_URL", "http://127.0.0.1:50325"
            ).strip().rstrip("/"),
            adspower_profile_id=os.getenv("ADSPOWER_PROFILE_ID", "").strip(),
            adspower_api_key=os.getenv("ADSPOWER_API_KEY", "").strip(),
            require_auth=_bool("AVITO_REQUIRE_AUTH", "true"),
            stop_profile_on_exit=_bool(
                "AVITO_STOP_PROFILE_ON_EXIT", "false"
            ),
            max_pages=int(os.getenv("AVITO_MAX_PAGES", "2")),
            max_items=int(os.getenv("AVITO_MAX_ITEMS", "50")),
            detail_mode=os.getenv("AVITO_DETAIL_MODE", "full").strip(),
            detail_concurrency=int(
                os.getenv("AVITO_DETAIL_CONCURRENCY", "1")
            ),
            profile_start_timeout=float(
                os.getenv("AVITO_PROFILE_START_TIMEOUT", "30")
            ),
            cdp_connect_timeout=float(
                os.getenv("AVITO_CDP_CONNECT_TIMEOUT", "15")
            ),
            navigation_timeout=float(
                os.getenv("AVITO_NAVIGATION_TIMEOUT", "25")
            ),
            locator_timeout=float(
                os.getenv("AVITO_LOCATOR_TIMEOUT", "15")
            ),
            listing_timeout=float(
                os.getenv("AVITO_LISTING_TIMEOUT", "20")
            ),
            search_deadline=float(
                os.getenv("AVITO_SEARCH_DEADLINE", "180")
            ),
            retry_attempts=int(os.getenv("AVITO_RETRY_ATTEMPTS", "2")),
            delay_min_seconds=float(
                os.getenv("AVITO_DELAY_MIN_SECONDS", "1.2")
            ),
            delay_max_seconds=float(
                os.getenv("AVITO_DELAY_MAX_SECONDS", "3.5")
            ),
            worker_host=os.getenv(
                "AVITO_WORKER_HOST", "127.0.0.1"
            ).strip(),
            worker_port=int(os.getenv("AVITO_WORKER_PORT", "8081")),
            worker_token=os.getenv("AVITO_WORKER_TOKEN", "").strip(),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.adspower_profile_id:
            raise ConfigurationError("ADSPOWER_PROFILE_ID is required")
        if self.detail_mode not in {"search_only", "full"}:
            raise ConfigurationError("invalid AVITO_DETAIL_MODE")
        if self.max_pages < 1 or self.max_items < 1:
            raise ConfigurationError("page and item limits must be positive")
        if self.detail_concurrency != 1:
            raise ConfigurationError(
                "AVITO_DETAIL_CONCURRENCY must be 1 for one profile"
            )
        if self.delay_min_seconds > self.delay_max_seconds:
            raise ConfigurationError("invalid delay range")
