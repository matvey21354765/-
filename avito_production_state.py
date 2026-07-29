"""Атомарное состояние provider и последний успешный кэш Авито."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


DEFAULT_STATE = {
    "blocked_until": None,
    "consecutive_blocks": 0,
    "last_http": None,
    "last_success_at": None,
    "last_failure_at": None,
    "last_exit_ip": None,
}


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def safe_read_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except (OSError, UnicodeError, json.JSONDecodeError):
        return json.loads(json.dumps(default))


def normalize_search_key(parts: dict[str, Any]) -> str:
    """Стабильный сетевой ключ без user_id."""
    normalized = {
        "region": str(parts.get("region") or "").strip().lower(),
        "category": str(parts.get("category") or "cars").strip().lower(),
        "price_min": max(0, int(parts.get("price_min") or 0)),
        "price_max": max(0, int(parts.get("price_max") or 99_000_000)),
        "brand": str(parts.get("brand") or "").strip().lower(),
        "model": str(parts.get("model") or "").strip().lower(),
        "year": max(0, int(parts.get("year") or 0)),
        "radius": max(0, int(parts.get("radius") or 0)),
        "sort": str(parts.get("sort") or "default").strip().lower(),
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AvitoProductionState:
    def __init__(
        self,
        *,
        state_path: Path = Path("data/avito_provider_state.json"),
        cache_path: Path = Path("data/avito_last_success.json"),
        cache_ttl: int = 1800,
        stale_cache_ttl: int = 86400,
    ) -> None:
        self.state_path = Path(state_path)
        self.cache_path = Path(cache_path)
        self.cache_ttl = max(1, int(cache_ttl))
        self.stale_cache_ttl = max(self.cache_ttl, int(stale_cache_ttl))

    def load_state(self) -> dict[str, Any]:
        state = safe_read_json(self.state_path, DEFAULT_STATE)
        if not isinstance(state, dict):
            state = dict(DEFAULT_STATE)
        return {**DEFAULT_STATE, **state}

    def save_state(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.state_path, {**DEFAULT_STATE, **state})

    def is_blocked(self, now: float | None = None) -> bool:
        now = time.time() if now is None else float(now)
        blocked_until = self.load_state().get("blocked_until")
        return isinstance(blocked_until, (int, float)) and now < blocked_until

    def record_block(self, http: int, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        state = self.load_state()
        count = max(0, int(state.get("consecutive_blocks") or 0)) + 1
        cooldown = 2 * 3600 if count == 1 else 6 * 3600 if count == 2 else 24 * 3600
        state.update({
            "blocked_until": now + cooldown,
            "consecutive_blocks": count,
            "last_http": int(http),
            "last_failure_at": now,
        })
        self.save_state(state)
        return state

    def record_success(
        self,
        search_key: str,
        items: list[dict],
        *,
        provider: str,
        exit_ip: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        cache = safe_read_json(self.cache_path, {"version": 1, "entries": {}})
        if not isinstance(cache, dict) or not isinstance(cache.get("entries"), dict):
            cache = {"version": 1, "entries": {}}
        cache["entries"][search_key] = {
            "search_key": search_key,
            "provider": provider,
            "exit_ip": exit_ip,
            "count": len(items),
            "cached_at": now,
            "items": list(items),
        }
        atomic_write_json(self.cache_path, cache)
        state = self.load_state()
        state.update({
            "blocked_until": None,
            "consecutive_blocks": 0,
            "last_http": 200,
            "last_success_at": now,
            "last_exit_ip": exit_ip or state.get("last_exit_ip"),
        })
        self.save_state(state)
        return state

    def cached(
        self,
        search_key: str,
        *,
        allow_stale: bool = True,
        now: float | None = None,
    ) -> tuple[list[dict], dict[str, Any]]:
        now = time.time() if now is None else float(now)
        cache = safe_read_json(self.cache_path, {"version": 1, "entries": {}})
        entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
        entry = entries.get(search_key) if isinstance(entries, dict) else None
        if not isinstance(entry, dict) or not isinstance(entry.get("items"), list):
            return [], {}
        cached_at = float(entry.get("cached_at") or 0)
        age = max(0.0, now - cached_at)
        fresh = age <= self.cache_ttl
        stale = not fresh and age <= self.stale_cache_ttl
        if not fresh and not (allow_stale and stale):
            return [], {}
        state = self.load_state()
        metadata = {
            "cached_at": cached_at,
            "age_seconds": age,
            "stale": stale,
            "blocked": self.is_blocked(now),
            "last_error_http": (
                state.get("last_http") if state.get("last_http") in (403, 429) else None
            ),
            "provider": entry.get("provider"),
            "count": len(entry["items"]),
        }
        return list(entry["items"]), metadata

    def latest_cache_age(self, now: float | None = None) -> float | None:
        now = time.time() if now is None else float(now)
        cache = safe_read_json(self.cache_path, {"version": 1, "entries": {}})
        entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
        timestamps = [
            float(entry.get("cached_at") or 0)
            for entry in entries.values()
            if isinstance(entry, dict) and entry.get("cached_at")
        ] if isinstance(entries, dict) else []
        return max(0.0, now - max(timestamps)) if timestamps else None
