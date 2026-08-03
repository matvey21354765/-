from __future__ import annotations

import datetime
import logging

import control_bot


def _normalized(index: int) -> dict:
    return {
        "id": str(index), "source_id": str(index), "source": "avito",
        "title": f"Lada {index}", "price": 100_000 + index,
        "url": f"https://www.avito.ru/item/{index}", "location": "Москва",
        "images": [], "published_at": "2026-08-03T10:00:00+00:00",
    }


def test_fifty_collector_items_reach_telegram_counter(monkeypatch):
    normalized = [_normalized(index) for index in range(1, 51)]

    class Collector:
        last_diagnostics = {"provider_returned": 50, "cache_hit": False}
        _latest_items = None
        def search(self, search):
            return normalized
        def now(self):
            return 0

    monkeypatch.setattr(control_bot, "AVITO_ENABLED", True)
    monkeypatch.setattr(control_bot, "_REST_APP_COLLECTOR", Collector())
    result = control_bot._avito_cached_result(
        "moscow", 0, 1_000_000, True, ""
    )
    assert len(result) == 50
    final = control_bot._avito_common_pipeline(
        result, 0, 1_000_000, "all", ""
    )
    assert len(final) == 50
    assert sum(item["source"] == "avito" for item in final) == 50


def test_common_filter_logs_drop_and_counter_matches_final(caplog):
    accepted = _normalized(1)
    rejected = {**_normalized(2), "price": 2_000_000, "_price_int": 2_000_000}
    with caplog.at_level(logging.INFO):
        final = control_bot._avito_common_pipeline(
            [accepted, rejected], 0, 1_000_000, "all", ""
        )
    telegram_counter = sum(item.get("source") == "avito" for item in final)
    assert telegram_counter == len(final) == 1
    assert "[AVITO COMMON FILTER DROP]" in caplog.text
    assert "source_id=2" in caplog.text
    assert "reason=price" in caplog.text
