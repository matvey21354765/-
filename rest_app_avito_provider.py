"""Avito listings via the documented Rest-App API only."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from datetime import datetime, timedelta, timezone


BASE_URL = "https://rest-app.net/api"
CAR_CATEGORY_ID = "9"


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


REST_APP_MAX_TIME_WINDOWS = _env_int("REST_APP_MAX_TIME_WINDOWS", 6, 1, 6)
REST_APP_CACHE_TTL = _env_int("REST_APP_CACHE_TTL", 300, 30, 3600)
REST_APP_RESULT_LIMIT = _env_int("REST_APP_RESULT_LIMIT", 50, 1, 50)
REST_APP_DB_MAX_AGE_HOURS = _env_int(
    "REST_APP_DB_MAX_AGE_HOURS", 24, 1, 168
)


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
    _raw_cache: tuple[float, list[dict]] | None = None
    _cache_lock = threading.Lock()
    _key_locks: dict[tuple, threading.Lock] = {}
    _cooldown_until = 0.0
    _categories_cache: list[dict] | None = None
    _regions_cache: list[dict] | None = None
    _cities_cache: dict[str, list[dict]] = {}

    def __init__(
        self, config: RestAppConfig | None = None,
        cache_ttl: int = REST_APP_CACHE_TTL,
    ):
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
        self.last_diagnostics.update({
            "api_status": payload.get("status"),
            "top_level_keys": sorted(str(key) for key in payload),
            "raw_payload": payload,
        })
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

    @staticmethod
    def _date_range(days: int = 1) -> dict[str, str]:
        # Europe/Moscow has used fixed UTC+03:00 since 2014.
        now = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
        return {
            "date1": (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
            "date2": now.strftime("%Y-%m-%d %H:%M:%S"),
        }

    @staticmethod
    def _time_windows(max_windows: int = REST_APP_MAX_TIME_WINDOWS) -> list[dict[str, str]]:
        now = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
        intervals = ((0, 30), (30, 60), (60, 180), (180, 360), (360, 720), (720, 1440))
        return [
            {
                "date1": (now - timedelta(minutes=older)).strftime("%Y-%m-%d %H:%M:%S"),
                "date2": (now - timedelta(minutes=newer)).strftime("%Y-%m-%d %H:%M:%S"),
            }
            for newer, older in intervals[:max_windows]
        ]

    @staticmethod
    def _db_path() -> str:
        return os.getenv("REST_APP_DB_PATH", "data/rest_app_avito.db")

    @classmethod
    def _db(cls):
        path = cls._db_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS rest_app_listings ("
            "source TEXT NOT NULL, source_id TEXT NOT NULL, item_json TEXT NOT NULL, "
            "fetched_at REAL NOT NULL, UNIQUE(source, source_id))"
        )
        return connection

    @classmethod
    def _save_db(cls, items: list[dict]) -> None:
        if not items:
            return
        with cls._db() as connection:
            connection.executemany(
                "INSERT INTO rest_app_listings(source, source_id, item_json, fetched_at) "
                "VALUES('avito', ?, ?, ?) ON CONFLICT(source, source_id) DO UPDATE SET "
                "item_json=excluded.item_json, fetched_at=excluded.fetched_at",
                [
                    (
                        item["source_id"],
                        json.dumps(item, ensure_ascii=False),
                        time.time(),
                    )
                    for item in items
                ],
            )

    @classmethod
    def _load_db(
        cls, *, region_name: str, city_name: str, price_min: int,
        price_max: int, query: str, year: int, max_age: float | None = None,
    ) -> list[dict]:
        try:
            with cls._db() as connection:
                sql = (
                    "SELECT item_json FROM rest_app_listings WHERE source='avito'"
                )
                args: list[Any] = []
                if max_age is not None:
                    sql += " AND fetched_at>=?"
                    args.append(time.time() - max_age)
                sql += " ORDER BY fetched_at DESC LIMIT 1000"
                rows = connection.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        unique: dict[str, dict] = {}
        wanted_location = (city_name or region_name).casefold()
        query_folded = query.casefold()
        for (raw,) in rows:
            try:
                item = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            price = int(item.get("price") or 0)
            if price and not price_min <= price <= price_max:
                continue
            if wanted_location and wanted_location not in str(
                item.get("location") or ""
            ).casefold():
                continue
            if query_folded and query_folded not in str(
                item.get("title") or ""
            ).casefold():
                continue
            if year and int(item.get("year") or 0) != year:
                continue
            unique[item["source_id"]] = item
        return list(unique.values())

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
    def _extract_raw_items(payload: dict) -> tuple[list, str]:
        if isinstance(payload, list):
            return payload, "$"
        data = payload.get("data")
        if isinstance(data, list):
            return data, "list"
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"], "dict.items"
        for key in ("ads", "items", "result", "results"):
            if isinstance(payload.get(key), list):
                return payload[key], key
        return [], type(data).__name__

    @staticmethod
    def _param_map(ad: dict) -> dict[str, str]:
        result: dict[str, str] = {}
        for param in ad.get("params") or []:
            if not isinstance(param, dict):
                continue
            name = str(param.get("name") or "").strip()
            value = str(param.get("value") or "").strip()
            if name:
                result[name.casefold()] = value
        return result

    @classmethod
    def _normalize(cls, ad: dict) -> dict:
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
        params = cls._param_map(ad)
        def param_value(*needles: str) -> str:
            for key, value in params.items():
                if any(needle in key for needle in needles):
                    return value
            return ""

        year_text = param_value("год выпуска")
        year_match = re.search(r"\b(19[5-9]\d|20[0-3]\d)\b", year_text)
        source_id = str(ad.get("Id") or "").strip()
        avito_id = str(ad.get("avito_id") or "").strip()
        if not source_id and avito_id and avito_id != "hidden_in_demo":
            source_id = avito_id
        if not source_id:
            identity = "|".join(str(ad.get(key) or "") for key in (
                "title", "time", "city", "price"
            ))
            source_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        raw_url = str(ad.get("url") or "").strip()
        url = None if raw_url == "hidden_in_demo" else (raw_url or None)
        location = ", ".join(
            value for value in (
                str(ad.get("region") or "").strip(),
                str(ad.get("city") or "").strip(),
                str(ad.get("district") or "").strip(),
                str(ad.get("address") or "").strip(),
            )
            if value
        )
        specs = {
            "year": param_value("год выпуска"),
            "mileage": param_value("пробег"),
            "transmission": param_value("коробка передач"),
            "engine": param_value("тип двигателя"),
            "drive": param_value("привод"),
            "power": param_value("мощность"),
            "body": param_value("тип кузова"),
            "steering": param_value("руль"),
        }
        seller_type = (
            "dealer"
            if str(ad.get("postfix") or "").strip().casefold() == "компания"
            else "private"
        )
        return {
            "id": source_id,
            "source_id": source_id,
            "title": title,
            "price": price,
            "url": url,
            "image": image or None,
            "location": location,
            "published_at": str(ad.get("time") or "") or None,
            "source": "avito",
            "seller": str(ad.get("name") or ""),
            "seller_type": seller_type,
            "description": str(ad.get("description") or ""),
            "marka": str(ad.get("marka") or ""),
            "model": str(ad.get("model") or ""),
            "params": ad.get("params") if isinstance(ad.get("params"), list) else [],
            "specs": specs,
            "year": int(year_match.group(1)) if year_match else 0,
            "demo_url_hidden": raw_url == "hidden_in_demo",
            "demo_mode": raw_url == "hidden_in_demo" or avito_id == "hidden_in_demo",
            "demo_price_unreliable": raw_url == "hidden_in_demo" or avito_id == "hidden_in_demo",
        }

    @staticmethod
    def _is_private(item: dict) -> bool:
        if item.get("seller_type") == "dealer":
            return False
        values = " ".join(
            str(param.get("value") or "") for param in item.get("params", [])
            if isinstance(param, dict)
        ).casefold()
        return not any(mark in values for mark in ("компания", "дилер", "магазин"))

    @classmethod
    def _filter_items(
        cls, items: list[dict], *, region_name: str, city_name: str,
        price_min: int, price_max: int, brand: str, model: str, year: int,
        private_only: bool,
    ) -> tuple[list[dict], dict[str, Any]]:
        diagnostics: dict[str, Any] = {}
        location_terms = [
            value.strip().casefold() for value in (region_name, city_name)
            if value and value.strip()
        ]
        location_filtered = [
            item for item in items
            if not location_terms or any(
                term in str(item.get("location") or "").casefold()
                for term in location_terms
            )
        ]
        location_relaxed = bool(location_terms and not location_filtered and items)
        filtered = list(items) if location_relaxed else location_filtered
        diagnostics["location_filter_relaxed"] = location_relaxed
        diagnostics["after_location"] = len(filtered)
        demo_relax_price = os.getenv(
            "REST_APP_DEMO_RELAX_PRICE", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        filtered = [
            item for item in filtered
            if (demo_relax_price and item.get("demo_price_unreliable"))
            or (
                item.get("price") is not None
                and int(price_min) <= int(item["price"]) <= int(price_max)
            )
        ]
        diagnostics["after_price"] = len(filtered)
        terms = [value.strip().casefold() for value in (brand, model) if value.strip()]
        filtered = [
            item for item in filtered
            if not terms or all(
                term in " ".join(str(item.get(field) or "") for field in (
                    "title", "marka", "model", "description"
                )).casefold()
                for term in terms
            )
        ]
        diagnostics["after_brand"] = len(filtered)
        filtered = [
            item for item in filtered
            if not year or int(item.get("year") or 0) == int(year)
        ]
        diagnostics["after_year"] = len(filtered)
        without_private = list(filtered)
        if private_only:
            filtered = [item for item in filtered if cls._is_private(item)]
        relaxed = bool(private_only and not filtered and without_private)
        if relaxed:
            filtered = without_private
            for item in filtered:
                item["private_filter_relaxed"] = True
        diagnostics["private_filter_relaxed"] = relaxed
        diagnostics["after_private"] = len(filtered)
        return filtered, diagnostics

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
        lookback_minutes: int = 1440,
    ) -> list[dict]:
        key = (
            region_name.casefold(), city_name.casefold(), int(price_min),
            int(price_max), brand.casefold(), model.casefold(), int(year or 0),
            bool(private_only), min(1000, int(limit)), int(lookback_minutes),
        )
        now = time.time()
        if now < type(self)._cooldown_until:
            raise RestAppRateLimitedError("Rest-App cooldown is active", 429)

        def finish(source_items: list[dict], *, cache_hit: bool, db_hit: bool = False,
                   extra: dict[str, Any] | None = None) -> list[dict]:
            filtered, stages = self._filter_items(
                source_items, region_name=region_name, city_name=city_name,
                price_min=price_min, price_max=price_max, brand=brand, model=model,
                year=int(year or 0), private_only=private_only,
            )
            filtered = filtered[:max(1, int(limit))]
            diagnostics = {
                "http": 200, "status": "success" if filtered else "empty",
                "cache_hit": cache_hit, "db_hit": db_hit,
                "raw_items": len(source_items), "normalized_items": len(source_items),
                "items": len(filtered), **stages,
                "demo_mode": any(item.get("demo_mode") for item in source_items),
                "demo_price_unreliable": any(
                    item.get("demo_price_unreliable") for item in source_items
                ),
                "demo_url_hidden": any(
                    item.get("demo_url_hidden") for item in source_items
                ),
                "requests": 0,
            }
            if extra:
                diagnostics.update(extra)
            self.last_diagnostics = diagnostics
            logging.getLogger(__name__).info(
                "[Rest-App Avito] raw=%d normalized=%d after_location=%d "
                "after_price=%d after_brand=%d after_year=%d after_private=%d "
                "cache_hit=%s db_hit=%s",
                len(source_items), len(source_items), stages["after_location"],
                stages["after_price"], stages["after_brand"], stages["after_year"],
                stages["after_private"], str(cache_hit).lower(), str(db_hit).lower(),
            )
            logging.getLogger(__name__).info(
                "[Avito RestApp] requests=%d raw_total=%d unique_total=%d "
                "after_location=%d after_price=%d returned=%d",
                int(diagnostics.get("requests") or 0),
                int(diagnostics.get("raw_items") or len(source_items)),
                len(source_items), stages["after_location"],
                stages["after_price"], len(filtered),
            )
            with self._cache_lock:
                self._cache[key] = (time.time(), list(filtered))
            return list(filtered)

        with self._cache_lock:
            raw_cached = type(self)._raw_cache
        if raw_cached and now - raw_cached[0] < self.cache_ttl:
            return finish(list(raw_cached[1]), cache_hit=True)

        db_cached = self._load_db(
            region_name="", city_name="", price_min=0, price_max=99_000_000,
            query="", year=0, max_age=self.cache_ttl,
        )
        if db_cached:
            with self._cache_lock:
                type(self)._raw_cache = (now, list(db_cached))
            return finish(db_cached, cache_hit=True, db_hit=True)

        with self._cache_lock:
            key_lock = self._key_locks.setdefault(("rest_app_ads",), threading.Lock())
        with key_lock:
            with self._cache_lock:
                raw_cached = type(self)._raw_cache
            if raw_cached and time.time() - raw_cached[0] < self.cache_ttl:
                return finish(list(raw_cached[1]), cache_hit=True)
            params: dict[str, Any] = {
                "category_id": CAR_CATEGORY_ID,
                "sort": "desc",
                "limit": REST_APP_RESULT_LIMIT,
            }
            raw_items: list = []
            raw_data_type = "list"
            payload: dict = {"status": "ok"}
            request_count = 0
            for window in self._time_windows():
                window_params = {**params, **window}
                payload = self._post("ads", window_params)
                request_count += 1
                window_items, raw_data_type = self._extract_raw_items(payload)
                raw_items.extend(window_items)
            unique: dict[str, dict] = {}
            rejection_reasons: list[str] = []
            for raw in raw_items:
                if not isinstance(raw, dict):
                    if len(rejection_reasons) < 5:
                        rejection_reasons.append("element is not an object")
                    continue
                item = self._normalize(raw)
                if not item["id"]:
                    if len(rejection_reasons) < 5:
                        rejection_reasons.append("missing source identity")
                    continue
                unique[item["id"]] = item
            normalized = sorted(
                unique.values(),
                key=lambda item: item.get("published_at") or "",
                reverse=True,
            )
            self._save_db(normalized)
            db_hit = False
            if not normalized:
                normalized = self._load_db(
                    region_name="", city_name="", price_min=0,
                    price_max=99_000_000, query="", year=0,
                    max_age=REST_APP_DB_MAX_AGE_HOURS * 3600,
                )
                db_hit = bool(normalized)
            with self._cache_lock:
                type(self)._raw_cache = (time.time(), list(normalized))
            return finish(normalized, cache_hit=False, db_hit=db_hit, extra={
                "api_status": payload.get("status"),
                "raw_items": len(raw_items),
                "normalized_items": len(normalized),
                "raw_data_type": raw_data_type,
                "rejection_reasons": rejection_reasons,
                "requests": request_count,
            })
