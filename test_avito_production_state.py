from __future__ import annotations

import json
from pathlib import Path

import pytest

from avito_production_state import (
    AvitoProductionState,
    normalize_search_key,
)


def manager(tmp_path: Path) -> AvitoProductionState:
    return AvitoProductionState(
        state_path=tmp_path / "state.json",
        cache_path=tmp_path / "cache.json",
        cache_ttl=1800,
        stale_cache_ttl=86400,
    )


def test_http_200_saves_success_cache(tmp_path):
    store = manager(tmp_path)
    store.record_success("key", [{"id": "1"}], provider="duff_vless", now=1000)
    items, meta = store.cached("key", now=1001)
    assert items == [{"id": "1"}]
    assert meta["stale"] is False


@pytest.mark.parametrize("http", [403, 429])
def test_block_does_not_erase_success_cache(tmp_path, http):
    store = manager(tmp_path)
    store.record_success("key", [{"id": "1"}], provider="duff_vless", now=1000)
    store.record_block(http, now=2000)
    items, meta = store.cached("key", allow_stale=True, now=2001)
    assert items == [{"id": "1"}]
    assert meta["blocked"] is True
    assert meta["last_error_http"] == http


def test_429_returns_stale_cache(tmp_path):
    store = manager(tmp_path)
    store.record_success("key", [{"id": "1"}], provider="duff_vless", now=1000)
    store.record_block(429, now=4000)
    items, meta = store.cached("key", allow_stale=True, now=4001)
    assert items == [{"id": "1"}]
    assert meta["stale"] is True


def test_adaptive_cooldown_and_success_reset(tmp_path):
    store = manager(tmp_path)
    first = store.record_block(429, now=1000)
    assert first["blocked_until"] == 1000 + 2 * 3600
    second = store.record_block(429, now=first["blocked_until"] + 1)
    assert second["blocked_until"] == first["blocked_until"] + 1 + 6 * 3600
    third = store.record_block(403, now=second["blocked_until"] + 1)
    assert third["blocked_until"] == second["blocked_until"] + 1 + 24 * 3600
    store.record_success("key", [{"id": "1"}], provider="duff_vless", now=99999)
    state = store.load_state()
    assert state["consecutive_blocks"] == 0
    assert state["blocked_until"] is None


def test_search_key_merges_equivalent_users():
    base = {
        "region": " Chelyabinsk ",
        "category": "cars",
        "price_min": 0,
        "price_max": 100000,
        "brand": "LADA",
        "model": "",
        "year": 0,
        "radius": 200,
        "sort": "date",
    }
    first = normalize_search_key({**base, "user_id": 1})
    second = normalize_search_key({
        **base,
        "region": "chelyabinsk",
        "brand": "lada",
        "user_id": 999,
    })
    assert first == second
    assert "user_id" not in first


def test_atomic_state_and_cache_leave_no_tmp_file(tmp_path):
    store = manager(tmp_path)
    store.record_success("key", [{"id": "1"}], provider="duff_vless", now=1000)
    store.record_block(429, now=2000)
    assert store.state_path.exists()
    assert store.cache_path.exists()
    assert not store.state_path.with_name("state.json.tmp").exists()
    assert not store.cache_path.with_name("cache.json.tmp").exists()
    json.loads(store.state_path.read_text(encoding="utf-8"))
    json.loads(store.cache_path.read_text(encoding="utf-8"))


def test_corrupt_state_recovers_safely(tmp_path):
    store = manager(tmp_path)
    store.state_path.write_text("{broken", encoding="utf-8")
    state = store.load_state()
    assert state["consecutive_blocks"] == 0
    assert state["blocked_until"] is None
    assert store.state_path.read_text(encoding="utf-8") == "{broken"


def test_blocked_until_skips_provider_network(monkeypatch, tmp_path):
    import control_bot

    store = manager(tmp_path)
    key = control_bot._avito_schedule_key(
        "chelyabinsk", 0, 100000, True, ""
    )
    persistent_key = control_bot._avito_persistent_key(key)
    store.record_success(
        persistent_key,
        [{"url": "https://www.avito.ru/item/1"}],
        provider="duff_vless",
        now=1000,
    )
    store.record_block(429, now=1100)

    class ForbiddenProvider:
        def __init__(self, **kwargs):
            raise AssertionError("network provider must not be constructed")

    monkeypatch.setattr(control_bot, "_AVITO_PRODUCTION_STATE", store)
    monkeypatch.setattr(control_bot, "AvitoDuffProvider", ForbiddenProvider)
    monkeypatch.setattr(control_bot, "AVITO_ENABLED", True)
    control_bot._AVITO_SCHEDULE.clear()
    control_bot._AVITO_GLOBAL_NEXT_ATTEMPT_AT = 0
    result = control_bot._avito_scheduled_fetch(key, now=1200)
    assert result == [{"url": "https://www.avito.ru/item/1"}]


def test_healthcheck_never_calls_avito(monkeypatch, tmp_path):
    import control_bot
    from curl_cffi import requests as cffi_requests

    urls = []
    responses = iter(["10.0.0.1", "20.0.0.1"])

    class Response:
        status_code = 200

        def __init__(self, ip):
            self.ip = ip

        def json(self):
            return {"ip": self.ip}

    class Session:
        def __init__(self, **kwargs):
            pass

        def get(self, url, **kwargs):
            urls.append(url)
            return Response(next(responses))

        def close(self):
            pass

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(cffi_requests, "Session", Session)
    monkeypatch.setattr(
        control_bot.socket if hasattr(control_bot, "socket") else __import__("socket"),
        "create_connection",
        lambda *args, **kwargs: Connection(),
    )
    monkeypatch.setattr(control_bot, "_AVITO_PRODUCTION_STATE", manager(tmp_path))
    report = control_bot.check_avito_transport_health()
    assert report["ips_differ"] is True
    assert urls == [
        "https://api.ipify.org?format=json",
        "https://api.ipify.org?format=json",
    ]
    assert all("avito.ru" not in url for url in urls)
