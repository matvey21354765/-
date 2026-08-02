"""Single REST-App collector shared by every Avito consumer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from avito_history import save_avito_history
from rest_app_avito_provider import CAR_CATEGORY_ID, RestAppAvitoProvider


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


REST_REQUEST_INTERVAL = _env_int("REST_REQUEST_INTERVAL", 60)
REST_APP_LAST_MINUTES = _env_int("REST_APP_LAST_MINUTES", 3)
REST_APP_CACHE_TTL_SECONDS = _env_int("REST_APP_CACHE_TTL_SECONDS", 55)
REST_APP_MAX_PAGES = _env_int("REST_APP_MAX_PAGES", 1)
REST_APP_DAILY_SOFT_LIMIT = _env_int("REST_APP_DAILY_SOFT_LIMIT", 8000)
REST_APP_DAILY_HARD_LIMIT = _env_int("REST_APP_DAILY_HARD_LIMIT", 9500)


def canonical_request_key(search: dict[str, Any]) -> str:
    return ":".join((
        str(search.get("region_id") or ""),
        str(search.get("category_id") or CAR_CATEGORY_ID),
        str(search.get("last_m") or REST_APP_LAST_MINUTES),
        str(search.get("page") or 1),
    ))


def _identity(item: dict[str, Any]) -> str:
    avito_id = str(item.get("source_id") or item.get("id") or "").strip()
    if avito_id and avito_id != "hidden_in_demo":
        return avito_id
    url = str(item.get("url") or "").split("?", 1)[0].rstrip("/")
    return "url:" + hashlib.sha256(url.encode()).hexdigest() if url else ""


class RestAppCollector:
    """Owns all REST-App HTTP calls, raw cache, history and delivery dedup."""

    _instances = 0
    _instances_lock = threading.Lock()

    def __init__(
        self,
        *,
        provider_factory: Callable[[], RestAppAvitoProvider] = RestAppAvitoProvider,
        db_path: str | Path | None = None,
        analyzer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.provider_factory = provider_factory
        self.db_path = Path(db_path or os.getenv(
            "AVITO_COLLECTOR_DB_PATH", "data/avito_collector.db"
        ))
        self.analyzer = analyzer or (lambda item: {})
        self.now = now
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._flights: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._registered: dict[str, dict[str, Any]] = {}
        self._stats = defaultdict(int)
        self._last_request_at = 0.0
        self._init_db()
        with self._instances_lock:
            type(self)._instances += 1

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS collector_items (
                    identity TEXT PRIMARY KEY,
                    item_json TEXT NOT NULL,
                    analysis_json TEXT,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_avito_deliveries (
                    user_id INTEGER NOT NULL,
                    avito_id TEXT NOT NULL,
                    search_id TEXT,
                    sent_at REAL NOT NULL,
                    UNIQUE(user_id, avito_id)
                );
                CREATE TABLE IF NOT EXISTS rest_app_requests (
                    requested_at REAL NOT NULL,
                    request_key TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                """
            )

    @classmethod
    def collector_instances(cls) -> int:
        with cls._instances_lock:
            return cls._instances

    def register_search(self, search: dict[str, Any]) -> str:
        normalized = dict(search)
        normalized.setdefault("category_id", CAR_CATEGORY_ID)
        normalized.setdefault("last_m", REST_APP_LAST_MINUTES)
        normalized.setdefault("page", 1)
        search_id = str(normalized.get("search_id") or (
            f"{normalized.get('user_id', '')}:{canonical_request_key(normalized)}"
        ))
        normalized["search_id"] = search_id
        with self._lock:
            self._registered[search_id] = normalized
        return search_id

    def active_searches(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(value) for value in self._registered.values()]

    def requests_used_today(self) -> int:
        day_start = self.now() - (self.now() % 86400)
        with self._connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM rest_app_requests WHERE requested_at>=?",
                (day_start,),
            ).fetchone()[0])

    @staticmethod
    def interval_for_usage(usage: int) -> int | None:
        if usage >= REST_APP_DAILY_HARD_LIMIT:
            return None
        if usage >= 9000:
            return 300
        if usage >= REST_APP_DAILY_SOFT_LIMIT:
            return 120
        return REST_REQUEST_INTERVAL

    def _record_request(self, key: str, status: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO rest_app_requests VALUES(?, ?, ?)",
                (self.now(), key, status),
            )

    def _fetch(self, search: dict[str, Any]) -> tuple[list[dict], dict[str, Any]]:
        provider = self.provider_factory()
        params: dict[str, Any] = {
            "category_id": str(search.get("category_id") or CAR_CATEGORY_ID),
            "last_m": int(search.get("last_m") or REST_APP_LAST_MINUTES),
            "page": 1,
            "sort": "desc",
            "limit": 50,
        }
        region_id = str(search.get("region_id") or "").strip()
        if region_id:
            params["region_id"] = region_id
        payload = provider._post("ads", params)
        raw, raw_type = provider._extract_raw_items(payload)
        unique: dict[str, dict] = {}
        for row in raw:
            if not isinstance(row, dict):
                continue
            item = provider._normalize(row)
            identity = _identity(item)
            if identity:
                unique[identity] = item
        items = sorted(
            unique.values(), key=lambda x: x.get("published_at") or "", reverse=True
        )
        return items, {
            "status": payload.get("status"),
            "http": int(provider.last_diagnostics.get("http") or 200),
            "raw_count": len(raw),
            "normalized_count": len(items),
            "raw_type": raw_type,
        }

    def _store_and_analyze(self, items: list[dict]) -> tuple[list[dict], int]:
        new_items: list[dict] = []
        analyzed = 0
        with self._connect() as db:
            for item in items:
                identity = _identity(item)
                if not identity:
                    continue
                row = db.execute(
                    "SELECT analysis_json FROM collector_items WHERE identity=?",
                    (identity,),
                ).fetchone()
                if row is None:
                    analysis = self.analyzer(item) or {}
                    analyzed += 1
                    db.execute(
                        "INSERT INTO collector_items VALUES(?, ?, ?, ?, ?)",
                        (identity, json.dumps(item, ensure_ascii=False),
                         json.dumps(analysis, ensure_ascii=False), self.now(), self.now()),
                    )
                    item.update(analysis)
                    new_items.append(item)
                else:
                    analysis = json.loads(row["analysis_json"] or "{}")
                    item.update(analysis)
                    db.execute(
                        "UPDATE collector_items SET item_json=?, last_seen=? WHERE identity=?",
                        (json.dumps(item, ensure_ascii=False), self.now(), identity),
                    )
                save_avito_history({**item, "avito_id": identity})
        return new_items, analyzed

    def collect_group(
        self, search: dict[str, Any], *, active_searches: int = 1,
        users_covered: int = 1,
        searches: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        key = canonical_request_key(search)
        request_id = uuid.uuid4().hex[:12]
        now = self.now()
        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < REST_APP_CACHE_TTL_SECONDS:
                self._stats["cache_hits"] += 1
                self._stats["duplicate_requests_prevented"] += 1
                return {"items": list(cached[1]), "new_items": [], "cache_hit": True,
                        "single_flight_joined": False, "request_id": request_id}
            flight = self._flights.get(key)
            if flight is None:
                flight = threading.Event()
                self._flights[key] = flight
                owner = True
            else:
                owner = False
                self._stats["duplicate_requests_prevented"] += 1
        if not owner:
            flight.wait(timeout=35)
            with self._lock:
                cached = self._cache.get(key)
            return {"items": list(cached[1]) if cached else [], "new_items": [],
                    "cache_hit": bool(cached), "single_flight_joined": True,
                    "request_id": request_id}

        status = "error"
        meta: dict[str, Any] = {}
        new_items: list[dict] = []
        matched_users = 0
        try:
            usage = self.requests_used_today()
            if self.interval_for_usage(usage) is None:
                status = "hard_limit"
                return {"items": [], "new_items": [], "cache_hit": False,
                        "single_flight_joined": False, "request_id": request_id,
                        "status": status}
            items, meta = self._fetch(search)
            status = str(meta.get("status") or "ok")
            self._record_request(key, status)
            self._last_request_at = self.now()
            new_items, _ = self._store_and_analyze(items)
            if searches:
                matched_users = len(self.match_users(new_items, searches))
            with self._lock:
                self._cache[key] = (self.now(), list(items))
            return {"items": items, "new_items": new_items, "cache_hit": False,
                    "single_flight_joined": False, "request_id": request_id,
                    "status": status, **meta}
        finally:
            with self._lock:
                event = self._flights.pop(key, None)
                if event:
                    event.set()
            logging.getLogger(__name__).info(
                "[REST COLLECTOR] request_id=%s region_id=%s active_searches=%d "
                "users_covered=%d cache_hit=%s single_flight_joined=%s status=%s "
                "raw_count=%d normalized_count=%d new_count=%d matched_users=%d "
                "requests_used_today=%d",
                request_id, search.get("region_id") or "", active_searches,
                users_covered, "false", "false", status,
                int(meta.get("raw_count") or 0), int(meta.get("normalized_count") or 0),
                len(new_items), matched_users, self.requests_used_today(),
            )

    @staticmethod
    def matches(item: dict[str, Any], search: dict[str, Any]) -> bool:
        location = str(item.get("location") or "").casefold()
        wanted = [str(search.get(k) or "").strip().casefold()
                  for k in ("region", "city") if search.get(k)]
        if wanted and not any(term in location for term in wanted):
            return False
        price = int(item.get("price") or 0)
        if price and not int(search.get("price_min") or 0) <= price <= int(
            search.get("price_max") or 99_000_000
        ):
            return False
        haystack = " ".join(str(item.get(k) or "") for k in (
            "title", "marka", "model", "description"
        )).casefold()
        for field in ("brand", "model"):
            term = str(search.get(field) or "").strip().casefold()
            if term and term not in haystack:
                return False
        year = int(search.get("year") or 0)
        if year and int(item.get("year") or 0) != year:
            return False
        if float(item.get("_deal_score") or 0) < float(search.get("min_deal_score") or 0):
            return False
        if float(item.get("_potential_profit") or 0) < float(search.get("min_profit") or 0):
            return False
        return True

    def match_users(self, items: list[dict], searches: list[dict]) -> dict[int, list[dict]]:
        matched: dict[int, list[dict]] = defaultdict(list)
        for item in items:
            for search in searches:
                uid = int(search.get("user_id") or 0)
                if uid and self.matches(item, search):
                    matched[uid].append(item)
        return dict(matched)

    def mark_delivered(self, user_id: int, item: dict, search_id: str = "") -> bool:
        identity = _identity(item)
        if not identity:
            return False
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO user_avito_deliveries VALUES(?, ?, ?, ?)",
                (int(user_id), identity, search_id, self.now()),
            )
            return cursor.rowcount == 1

    def cached_for_search(self, search: dict[str, Any]) -> list[dict]:
        key = canonical_request_key(search)
        with self._lock:
            cached = self._cache.get(key)
        if not cached:
            return []
        return [item for item in cached[1] if self.matches(item, search)]

    def collect_active(self, searches: list[dict[str, Any]]) -> dict[str, Any]:
        groups: dict[str, list[dict]] = defaultdict(list)
        for search in searches:
            if search.get("region_id"):
                self.register_search(search)
                groups[canonical_request_key(search)].append(search)
        output: dict[str, Any] = {}
        for key, group in groups.items():
            users = {int(row.get("user_id") or 0) for row in group}
            result = self.collect_group(
                group[0], active_searches=len(group),
                users_covered=len(users - {0}),
                searches=group,
            )
            output[key] = result
        return output

    async def run(self, search_loader: Callable[[], list[dict]]) -> None:
        while True:
            usage = self.requests_used_today()
            interval = self.interval_for_usage(usage)
            if interval is None:
                await asyncio.sleep(60)
                continue
            searches = await asyncio.to_thread(search_loader)
            known_ids = {str(row.get("search_id") or "") for row in searches}
            searches.extend(
                row for row in self.active_searches()
                if str(row.get("search_id") or "") not in known_ids
            )
            if searches:
                await asyncio.to_thread(self.collect_active, searches)
            await asyncio.sleep(interval)

    def diagnostics(self) -> dict[str, Any]:
        now = self.now()
        with self._connect() as db:
            last_hour = int(db.execute(
                "SELECT COUNT(*) FROM rest_app_requests WHERE requested_at>=?",
                (now - 3600,),
            ).fetchone()[0])
        searches = self.active_searches()
        regions = {str(row.get("region_id")) for row in searches if row.get("region_id")}
        users = {int(row.get("user_id") or 0) for row in searches if row.get("user_id")}
        used = self.requests_used_today()
        return {
            "active_users": len(users), "active_searches": len(searches),
            "active_regions": len(regions), "requests_last_hour": last_hour,
            "estimated_requests_per_day": last_hour * 24,
            "duplicate_requests_prevented": self._stats["duplicate_requests_prevented"],
            "cache_hits": self._stats["cache_hits"], "rest_app_used_today": used,
            "rest_app_remaining": max(0, REST_APP_DAILY_HARD_LIMIT - used),
            "collector_instances": self.collector_instances(),
        }


_COLLECTOR: RestAppCollector | None = None
_COLLECTOR_LOCK = threading.Lock()


def get_rest_app_collector() -> RestAppCollector:
    global _COLLECTOR
    with _COLLECTOR_LOCK:
        if _COLLECTOR is None:
            _COLLECTOR = RestAppCollector()
        return _COLLECTOR
