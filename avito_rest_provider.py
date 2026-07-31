"""Асинхронный REST-App провайдер для Avito.

Обёртка над rest_app_avito_provider.py, предоставляющая единый async интерфейс
и приводящая ответы к стандартному формату.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from rest_app_avito_provider import RestAppAvitoProvider, RestAppConfig


class AvitoRestProviderError(RuntimeError):
    pass


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
