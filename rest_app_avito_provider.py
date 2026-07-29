"""Avito listings via the documented Rest-App API only."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


BASE_URL = "https://rest-app.net/api"
CAR_CATEGORY_ID = "9"


class RestAppError(RuntimeError):
    status_code: int | None = None

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class RestAppAuthenticationError(RestAppError):
    pass


class RestAppRateLimitedError(RestAppError):
    pass


class RestAppResponseError(RestAppError):
    pass


@dataclass(frozen=True)
class RestAppConfig:
    login: str
    token: str
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "RestAppConfig":
        login = os.getenv("REST_APP_LOGIN", "").strip()
        token = os.getenv("REST_APP_TOKEN", "").strip()
        if not login or not token:
            raise RestAppAuthenticationError(
                "REST_APP_LOGIN/REST_APP_TOKEN are not configured"
            )
        try:
            timeout = float(os.getenv("REST_APP_TIMEOUT", "30"))
        except ValueError:
            timeout = 30.0
        return cls(login, token, max(1.0, min(30.0, timeout)))


class RestAppAvitoProvider:
    """Thread-safe provider with request coalescing and a 120-second cache."""

    _cache: dict[tuple, tuple[float, list[dict]]] = {}
    _cache_lock = threading.Lock()
    _key_locks: dict[tuple, threading.Lock] = {}
    _cooldown_until = 0.0
    _categories_cache: list[dict] | None = None
    _regions_cache: list[dict] | None = None
    _cities_cache: dict[str, list[dict]] = {}

    def __init__(self, config: RestAppConfig | None = None, cache_ttl: int = 120):
        self.config = config or RestAppConfig.from_env()
        self.cache_ttl = max(1, int(cache_ttl))
        self.last_diagnostics: dict[str, Any] = {}

    def _post(self, endpoint: str, params: dict | None = None) -> dict:
        body = {
            "login": self.config.login,
            "token": self.config.token,
            "format": "json",
        }
        body.update(params or {})
        encoded = urllib.parse.urlencode(body).encode("utf-8")
        request = urllib.request.Request(
            f"{BASE_URL}/{endpoint.lstrip('/')}",
            data=encoded,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "PerekupDrive/1.0 Rest-App client",
            },
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout
            ) as response:
                http = int(response.status)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            http = int(exc.code)
            raw = exc.read()
        except (OSError, TimeoutError) as exc:
            raise RestAppResponseError(
                f"Rest-App network error: {type(exc).__name__}"
            ) from exc
        self.last_diagnostics.update({
            "endpoint": endpoint,
            "http": http,
            "seconds": time.monotonic() - started,
        })
        if http in (401, 403):
            raise RestAppAuthenticationError(
                f"Rest-App authorization failed: HTTP {http}", http
            )
        if http == 429:
            type(self)._cooldown_until = time.time() + 3600
            raise RestAppRateLimitedError(
                "Rest-App rate limit: HTTP 429", 429
            )
        if http != 200:
            raise RestAppResponseError(f"Rest-App HTTP {http}", http)
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RestAppResponseError("Rest-App returned invalid JSON", http) from exc
        if not isinstance(payload, dict):
            raise RestAppResponseError("Rest-App JSON root is not an object", http)
        if str(payload.get("status", "")).lower() != "ok":
            message = str(payload.get("message") or payload.get("error") or "API error")
            lowered = message.lower()
            if "limit" in lowered:
                type(self)._cooldown_until = time.time() + 3600
                raise RestAppRateLimitedError(message, http)
            if any(word in lowered for word in ("token", "login", "auth", "доступ")):
                raise RestAppAuthenticationError(message, http)
            raise RestAppResponseError(message, http)
        return payload

    def info(self) -> dict:
        return self._post("info")

    def categories(self) -> list[dict]:
        if type(self)._categories_cache is not None:
            return list(type(self)._categories_cache)
        payload = self._post("category")
        rows = payload.get("data") if isinstance(payload.get("data"), list) else []
        type(self)._categories_cache = list(rows)
        return rows

    def regions(self) -> list[dict]:
        if type(self)._regions_cache is not None:
            return list(type(self)._regions_cache)
        payload = self._post("region")
        rows = payload.get("data") if isinstance(payload.get("data"), list) else []
        type(self)._regions_cache = list(rows)
        return rows

    def cities(self, region_id: str | int) -> list[dict]:
        key = str(region_id)
        if key in type(self)._cities_cache:
            return list(type(self)._cities_cache[key])
        payload = self._post("city", {"id": key})
        rows = payload.get("data") if isinstance(payload.get("data"), list) else []
        type(self)._cities_cache[key] = list(rows)
        return rows

    @staticmethod
    def _find_id(rows: list[dict], name: str) -> str:
        wanted = re.sub(r"\s+", " ", name).strip().casefold()
        for row in rows:
            if str(row.get("name", "")).strip().casefold() == wanted:
                return str(row.get("id", ""))
        return ""

    @staticmethod
    def _normalize(ad: dict) -> dict:
        images = ad.get("images")
        if isinstance(images, str):
            image = next((part.strip() for part in images.split(",") if part.strip()), "")
        elif isinstance(images, list):
            image = str(images[0]) if images else ""
        else:
            image = ""
        raw_price = str(ad.get("price") or "")
        digits = re.sub(r"\D", "", raw_price)
        price = int(digits) if digits else None
        title = str(ad.get("title") or "").strip()
        year_match = re.search(r"\b(19[5-9]\d|20[0-3]\d)\b", title)
        ad_id = ad.get("avito_id") or ad.get("id") or ad.get("Id") or ""
        return {
            "id": str(ad_id),
            "title": title,
            "price": price,
            "url": str(ad.get("url") or ""),
            "image": image or None,
            "location": str(ad.get("city") or ad.get("region") or ""),
            "published_at": str(ad.get("time") or "") or None,
            "source": "avito",
            "seller": str(ad.get("name") or ""),
            "description": str(ad.get("description") or ""),
            "params": ad.get("params") if isinstance(ad.get("params"), list) else [],
            "year": int(year_match.group(1)) if year_match else 0,
        }

    @staticmethod
    def _is_private(item: dict) -> bool:
        values = " ".join(
            str(param.get("value") or "") for param in item.get("params", [])
            if isinstance(param, dict)
        ).casefold()
        return not any(mark in values for mark in ("компания", "дилер", "магазин"))

    def search(
        self,
        *,
        region_name: str,
        city_name: str,
        price_min: int = 0,
        price_max: int = 99_000_000,
        brand: str = "",
        model: str = "",
        year: int = 0,
        private_only: bool = True,
        limit: int = 1000,
    ) -> list[dict]:
        key = (
            region_name.casefold(), city_name.casefold(), int(price_min),
            int(price_max), brand.casefold(), model.casefold(), int(year or 0),
            bool(private_only), min(1000, int(limit)),
        )
        now = time.time()
        if now < type(self)._cooldown_until:
            raise RestAppRateLimitedError("Rest-App cooldown is active", 429)
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self.cache_ttl:
                self.last_diagnostics = {
                    "http": 200, "status": "success", "cache_hit": True,
                    "items": len(cached[1]),
                }
                return list(cached[1])
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:
            with self._cache_lock:
                cached = self._cache.get(key)
                if cached and time.time() - cached[0] < self.cache_ttl:
                    return list(cached[1])
            regions = self.regions()
            region_id = self._find_id(regions, region_name)
            if not region_id:
                raise RestAppResponseError(f"Rest-App region not found: {region_name}")
            cities = self.cities(region_id)
            city_id = self._find_id(cities, city_name)
            params: dict[str, Any] = {
                "category_id": CAR_CATEGORY_ID,
                "region_id": region_id,
                "price1": max(0, int(price_min)),
                "price2": max(0, int(price_max)),
                "sort": "desc",
                "limit": min(1000, max(1, int(limit))),
            }
            if city_id:
                params["city_id"] = city_id
            query = " ".join(part.strip() for part in (brand, model) if part.strip())
            if query:
                params["q"] = query
                params["in"] = "title"
            payload = self._post("ads", params)
            raw_items = payload.get("data")
            if not isinstance(raw_items, list):
                raise RestAppResponseError("Rest-App data is not a list")
            unique: dict[str, dict] = {}
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                item = self._normalize(raw)
                if not item["id"]:
                    continue
                if year and item["year"] != int(year):
                    continue
                if private_only and not self._is_private(item):
                    continue
                unique[item["id"]] = item
            items = sorted(
                unique.values(),
                key=lambda item: item.get("published_at") or "",
                reverse=True,
            )
            with self._cache_lock:
                self._cache[key] = (time.time(), items)
            self.last_diagnostics.update({
                "status": "success" if items else "empty",
                "api_status": payload.get("status"),
                "cache_hit": False,
                "raw_items": len(raw_items),
                "items": len(items),
                "region_id": region_id,
                "city_id": city_id,
            })
            return list(items)
