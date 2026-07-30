from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from marketplace_result import MarketplaceResult


class AvitoWorkerError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass
class _CacheEntry:
    created_at: float
    items: list[dict[str, Any]]


class AvitoWorkerProvider:
    def __init__(
        self,
        worker_url: str | None = None,
        worker_token: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.worker_url = (
            worker_url or os.getenv("AVITO_WORKER_URL", "")
        ).strip().rstrip("/")
        self.worker_token = (
            worker_token or os.getenv("AVITO_WORKER_TOKEN", "")
        ).strip()
        self.timeout = float(
            timeout
            or os.getenv("AVITO_WORKER_TIMEOUT_SECONDS", "190")
        )
        self.cache_ttl = 300.0
        self.stale_ttl = 1800.0
        self._cache: dict[tuple[Any, ...], _CacheEntry] = {}
        self._locks: dict[tuple[Any, ...], threading.Lock] = {}
        self._master_lock = threading.Lock()

    def _key(self, payload: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(
            payload.get(name)
            for name in (
                "city", "region", "brand", "model", "price_min",
                "price_max", "year_min", "year_max", "seller_type", "sort",
            )
        )

    def _lock(self, key: tuple[Any, ...]) -> threading.Lock:
        with self._master_lock:
            return self._locks.setdefault(key, threading.Lock())

    def search(self, **filters: Any) -> MarketplaceResult:
        payload = {
            "city": filters.get("city") or filters.get("region") or "",
            "region": filters.get("region_name"),
            "brand": filters.get("brand") or None,
            "model": filters.get("model") or None,
            "price_min": filters.get("price_min"),
            "price_max": filters.get("price_max"),
            "year_min": filters.get("year_min"),
            "year_max": filters.get("year_max"),
            "seller_type": filters.get("seller_type", "any"),
            "sort": filters.get("sort", "date"),
            "max_pages": filters.get("max_pages", 2),
            "max_items": filters.get("max_items", 50),
            "detail_mode": filters.get("detail_mode", "full"),
        }
        key = self._key(payload)
        now = time.time()
        cached = self._cache.get(key)
        if cached and now - cached.created_at <= self.cache_ttl:
            return MarketplaceResult(
                items=list(cached.items),
                status="ok",
                diagnostics={"cache_hit": True},
            )
        with self._lock(key):
            cached = self._cache.get(key)
            now = time.time()
            if cached and now - cached.created_at <= self.cache_ttl:
                return MarketplaceResult(
                    items=list(cached.items),
                    status="ok",
                    diagnostics={"cache_hit": True},
                )
            try:
                items, http = self._request(payload)
                self._cache[key] = _CacheEntry(time.time(), items)
                return MarketplaceResult(
                    items=items,
                    status="ok" if items else "empty",
                    http=http,
                    diagnostics={"cache_hit": False},
                )
            except AvitoWorkerError as exc:
                if cached and now - cached.created_at <= self.stale_ttl:
                    return MarketplaceResult(
                        items=list(cached.items),
                        status="ok",
                        error_class=exc.error_type,
                        diagnostics={"cache_hit": True, "stale": True},
                    )
                return MarketplaceResult(
                    status=exc.error_type,
                    error_class=exc.error_type,
                    diagnostics={"cache_hit": False},
                )

    def _request(
        self, payload: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], int]:
        if not self.worker_url or not self.worker_token:
            raise AvitoWorkerError(
                "configuration", "Avito worker is not configured"
            )
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.post(
                f"{self.worker_url}/v1/avito/search",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.worker_token}",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise AvitoWorkerError("worker_timeout", "worker timeout") from exc
        except requests.RequestException as exc:
            raise AvitoWorkerError(
                "worker_unavailable", "worker unavailable"
            ) from exc
        finally:
            session.close()
        data = response.json() if response.content else {}
        if response.status_code != 200:
            error_type = str(data.get("error_type") or "worker_unavailable")
            raise AvitoWorkerError(error_type, f"worker HTTP {response.status_code}")
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            raise AvitoWorkerError("invalid_response", "items must be a list")
        return [self._normalize(item) for item in raw_items], response.status_code

    @staticmethod
    def _normalize(item: dict[str, Any]) -> dict[str, Any]:
        price = item.get("price")
        photos = item.get("photo_urls") or []
        attributes = item.get("attributes") or {}
        return {
            "source": "avito",
            "source_id": str(item.get("source_id") or ""),
            "_source_id": str(item.get("source_id") or ""),
            "title": str(item.get("title") or ""),
            "price": (
                f"{int(price):,} ₽".replace(",", " ")
                if isinstance(price, (int, float))
                else ""
            ),
            "_price_int": int(price) if isinstance(price, (int, float)) else 0,
            "url": item.get("url"),
            "location": item.get("location") or "",
            "description": item.get("description") or "",
            "seller": item.get("seller_name") or "",
            "seller_type": item.get("seller_type") or "unknown",
            "published_at": item.get("published_at"),
            "date": str(item.get("published_at") or "")[:10],
            "year": item.get("year") or 0,
            "mileage": item.get("mileage"),
            "specs": attributes,
            "_photo_url": photos[0] if photos else "",
            "_photos": len(photos),
            "_adspower_worker": True,
            "_under_order": is_under_order(item),
        }


def is_under_order(item: dict[str, Any]) -> bool:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("title", "description")
    ).lower()
    return any(marker in text for marker in (
        "под заказ",
        "привезём",
        "привезем",
        "авто из японии",
        "авто из кореи",
        "авто из китая",
        "аукцион",
        "растамож",
        "доставка автомобиля",
    ))
