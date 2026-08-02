"""Асинхронный REST-App провайдер для Avito.

Обёртка над rest_app_avito_provider.py, предоставляющая единый async интерфейс
и приводящая ответы к стандартному формату.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from rest_app_avito_provider import RestAppAvitoProvider, RestAppConfig


class AvitoRestProviderError(RuntimeError):
    pass


class UnexpectedRestAppPayload(AvitoRestProviderError):
    """REST-App returned HTTP 200 with an unknown JSON shape."""


def extract_rest_app_items(payload: Any) -> list[dict[str, Any]]:
    """Extract ads without confusing object keys with listing count."""
    candidates: list[tuple[str, Any]] = []
    if isinstance(payload, list):
        candidates.append(("$", payload))
    elif isinstance(payload, dict):
        for key in ("ads", "items", "data", "result", "results"):
            candidates.append((key, payload.get(key)))
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.append(("data.items", data.get("items")))
    for _path, value in candidates:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def describe_rest_app_payload(payload: Any) -> dict[str, Any]:
    items = extract_rest_app_items(payload)
    path = ""
    candidates = [("$", payload)] if isinstance(payload, list) else []
    if isinstance(payload, dict):
        candidates.extend((key, payload.get(key)) for key in (
            "ads", "items", "data", "result", "results"
        ))
        if isinstance(payload.get("data"), dict):
            candidates.append(("data.items", payload["data"].get("items")))
    for candidate_path, value in candidates:
        if isinstance(value, list):
            path = candidate_path
            break
    return {
        "top_level_type": type(payload).__name__,
        "top_level_keys": sorted(payload) if isinstance(payload, dict) else [],
        "raw_count": len(items),
        "first_item_keys": sorted(items[0]) if items else [],
        "nested_items_path": path,
    }


def save_safe_rest_app_sample(
    payload: Any, path: str | Path = "logs/rest_app_sample.json"
) -> None:
    """Persist one redacted response sample for production diagnosis."""
    target = Path(path)
    if target.exists():
        return
    secret_keys = {"token", "phone", "login", "password", "seller_phone"}

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): ("[redacted]" if str(key).casefold() in secret_keys else redact(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    target.parent.mkdir(parents=True, exist_ok=True)
    document = {"diagnostics": describe_rest_app_payload(payload), "payload": redact(payload)}
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.getLogger(__name__).info(
        "[REST-APP RESPONSE] top_level_type=%s top_level_keys=%s raw_items_count=%d "
        "first_item_keys=%s nested_items_path=%s",
        document["diagnostics"]["top_level_type"],
        document["diagnostics"]["top_level_keys"],
        document["diagnostics"]["raw_count"],
        document["diagnostics"]["first_item_keys"],
        document["diagnostics"]["nested_items_path"],
    )


class AvitoRestProvider:
    """Клиент REST-App API для Avito."""

    def __init__(self, login: str | None = None, token: str | None = None, timeout: float | None = None) -> None:
        self._login = (login or os.getenv("REST_APP_LOGIN", "")).strip()
        self._token = (token or os.getenv("REST_APP_TOKEN", "")).strip()
        self._timeout = float(timeout or os.getenv("REST_APP_TIMEOUT", "30") or 30)
        self._provider: RestAppAvitoProvider | None = None

    def _get_provider(self) -> RestAppAvitoProvider:
        if self._provider is None:
            if not self._login or not self._token:
                raise AvitoRestProviderError("REST_APP_LOGIN/REST_APP_TOKEN не настроены")
            cfg = RestAppConfig(login=self._login, token=self._token, timeout=self._timeout)
            self._provider = RestAppAvitoProvider(config=cfg)
        return self._provider

    @staticmethod
    def _to_unified(item: dict[str, Any]) -> dict[str, Any]:
        """Приводит REST-App item к единому формату."""
        raw_price = item.get("price")
        price = int(raw_price) if isinstance(raw_price, (int, float)) and raw_price > 0 else 0

        images = item.get("images") or []
        if isinstance(images, str):
            images = [u.strip() for u in images.split(",") if u.strip()]
        if not images and item.get("image"):
            images = [item["image"]]

        specs = item.get("specs") or {}
        location = item.get("location") or ""
        city = location.split(",")[-1].strip() if location else ""

        return {
            "id": str(item.get("source_id") or item.get("id") or ""),
            "title": str(item.get("title") or "").strip(),
            "price": price,
            "city": city,
            "region": str(item.get("region") or "").strip(),
            "description": str(item.get("description") or "").strip(),
            "phone": str(item.get("phone") or "").strip(),
            "seller": str(item.get("seller") or "").strip(),
            "seller_type": str(item.get("seller_type") or "private").strip(),
            "images": images,
            "url": str(item.get("url") or "").strip() or None,
            "date": str(item.get("published_at") or "").strip(),
            "year": int(item.get("year") or 0) or None,
            "mileage": str(specs.get("mileage") or "").strip() or None,
            "brand": str(item.get("marka") or "").strip(),
            "model": str(item.get("model") or "").strip(),
            "specs": specs,
            "source": "rest_app",
            "raw": item,
        }

    async def search_ads(
        self,
        *,
        q: str | None = None,
        city: str | None = None,
        region: str | None = None,
        price1: int | None = None,
        price2: int | None = None,
        last_m: int | None = None,
        last_s: int | None = None,
        category: str | None = None,
        brand: str = "",
        model: str = "",
        year: int = 0,
        private_only: bool = True,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Ищет объявления через REST-App API."""
        provider = self._get_provider()
        loop = asyncio.get_event_loop()

        region_name = region or city or ""
        city_name = city or ""

        # last_m / last_s → lookback_minutes
        lookback_minutes = 1440
        if last_m:
            lookback_minutes = last_m
        elif last_s:
            lookback_minutes = max(1, last_s // 60)

        # REST-App API поддерживает q как объединённый запрос
        query = q or " ".join(filter(None, [brand, model])).strip()

        raw_items = await loop.run_in_executor(
            None,
            lambda: provider.search(
                region_name=region_name,
                city_name=city_name,
                price_min=int(price1 or 0),
                price_max=int(price2 or 99_000_000),
                brand=brand,
                model=model,
                year=int(year or 0),
                private_only=private_only,
                limit=int(limit or 1000),
                lookback_minutes=lookback_minutes,
            ),
        )
        return [self._to_unified(it) for it in raw_items]

    async def get_ad_by_id(self, ad_id: str) -> dict[str, Any] | None:
        """Получает объявление по ID. REST-App не даёт детальный endpoint,
        поэтому ищем в локальной базе провайдера."""
        # REST-App public API не имеет endpoint для одного объявления
        return None

    async def get_regions(self) -> list[dict[str, Any]]:
        """Возвращает список регионов из REST-App."""
        provider = self._get_provider()
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(None, provider.regions)
        return [
            {
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or "").strip(),
            }
            for row in rows
            if isinstance(row, dict)
        ]

    async def get_categories(self) -> list[dict[str, Any]]:
        """Возвращает список категорий из REST-App."""
        provider = self._get_provider()
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(None, provider.categories)
        return [
            {
                "id": str(row.get("id") or ""),
                "name": str(row.get("name") or "").strip(),
            }
            for row in rows
            if isinstance(row, dict)
        ]

    def last_diagnostics(self) -> dict[str, Any]:
        if self._provider is None:
            return {}
        return dict(self._provider.last_diagnostics)
