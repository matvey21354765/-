from __future__ import annotations

import logging
import threading
import time

import avito_rest_collector as arc


class FakeProvider:
    calls = 0
    payloads = []
    delay = 0.0

    def __init__(self):
        self.last_diagnostics = {"http": 200}

    def _post(self, endpoint, params):
        type(self).calls += 1
        type(self).payloads.append(dict(params))
        if self.delay:
            time.sleep(self.delay)
        return {"status": "ok", "data": [{
            "Id": "ad-1", "title": "Lada Granta 2015",
            "price": "99999", "region": "Краснодарский край",
            "city": "Краснодар", "marka": "Lada", "model": "Granta",
            "time": "2026-08-02 10:00:00", "url": "https://example/ad-1",
            "params": [{"name": "Год выпуска", "value": "2015"}],
        }]}

    @staticmethod
    def _extract_raw_items(payload):
        return payload["data"], "list"

    @staticmethod
    def _normalize(row):
        return {
            "id": row["Id"], "source_id": row["Id"], "title": row["title"],
            "price": int(row["price"]), "location": f"{row['region']}, {row['city']}",
            "marka": row["marka"], "model": row["model"], "year": 2015,
            "description": "", "published_at": row["time"], "url": row["url"],
            "source": "avito",
        }


def search(uid=1, brand="", region_id="23"):
    return {
        "user_id": uid, "search_id": str(uid), "region_id": region_id,
        "category_id": "9", "last_m": 3, "page": 1,
        "region": "Краснодарский край", "city": "Краснодар",
        "price_min": 0, "price_max": 100000, "brand": brand,
    }


def collector(tmp_path, monkeypatch, analyzer=None):
    FakeProvider.calls = 0
    FakeProvider.payloads = []
    FakeProvider.delay = 0
    monkeypatch.setattr(arc, "save_avito_history", lambda item: {"status": "new"})
    monkeypatch.setattr(arc, "save_safe_rest_app_sample", lambda payload: None)
    return arc.RestAppCollector(
        provider_factory=FakeProvider,
        db_path=tmp_path / "collector.db",
        analyzer=analyzer,
    )


def test_twenty_users_one_region_make_one_request(tmp_path, monkeypatch):
    obj = collector(tmp_path, monkeypatch)
    result = obj.collect_active([search(uid=i) for i in range(1, 21)])
    assert FakeProvider.calls == 1
    assert len(result) == 1


def test_different_brands_same_region_do_not_create_requests(tmp_path, monkeypatch):
    obj = collector(tmp_path, monkeypatch)
    obj.collect_active([search(1, "Lada"), search(2, "Toyota")])
    assert FakeProvider.calls == 1


def test_single_flight_joins_concurrent_calls(tmp_path, monkeypatch):
    obj = collector(tmp_path, monkeypatch)
    FakeProvider.delay = 0.15
    outputs = []
    threads = [threading.Thread(target=lambda: outputs.append(obj.collect_group(search())))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert FakeProvider.calls == 1
    assert len(outputs) == 2
    assert any(row["single_flight_joined"] for row in outputs)


def test_listing_analyzed_only_once(tmp_path, monkeypatch):
    calls = []
    obj = collector(tmp_path, monkeypatch, analyzer=lambda item: calls.append(item["id"]) or {"_deal_score": 80})
    obj.collect_group(search())
    obj._cache.clear()
    obj.collect_group(search())
    assert calls == ["ad-1"]


def test_listing_matches_all_suitable_users(tmp_path, monkeypatch):
    obj = collector(tmp_path, monkeypatch)
    item = FakeProvider._normalize(FakeProvider()._post("ads", {})["data"][0])
    matched = obj.match_users([item], [search(i, "Lada") for i in range(1, 21)])
    assert set(matched) == set(range(1, 21))


def test_delivery_is_unique_per_user(tmp_path, monkeypatch):
    obj = collector(tmp_path, monkeypatch)
    item = {"id": "ad-1", "source_id": "ad-1"}
    assert obj.mark_delivered(10, item, "s1")
    assert not obj.mark_delivered(10, item, "s2")
    assert obj.mark_delivered(11, item, "s1")


def test_region_without_active_search_is_not_requested(tmp_path, monkeypatch):
    obj = collector(tmp_path, monkeypatch)
    obj.collect_active([search(region_id="")])
    assert FakeProvider.calls == 0


def test_daily_usage_changes_interval(monkeypatch):
    monkeypatch.setattr(arc, "REST_APP_DAILY_SOFT_LIMIT", 8000)
    monkeypatch.setattr(arc, "REST_APP_DAILY_HARD_LIMIT", 9500)
    assert arc.RestAppCollector.interval_for_usage(7999) == 60
    assert arc.RestAppCollector.interval_for_usage(8000) == 120
    assert arc.RestAppCollector.interval_for_usage(9000) == 300
    assert arc.RestAppCollector.interval_for_usage(9500) is None


def test_secrets_are_absent_from_logs(tmp_path, monkeypatch, caplog):
    obj = collector(tmp_path, monkeypatch)
    monkeypatch.setenv("REST_APP_TOKEN", "top-secret-token")
    monkeypatch.setenv("REST_APP_LOGIN", "private-login")
    with caplog.at_level(logging.INFO):
        obj.collect_group(search())
    assert "top-secret-token" not in caplog.text
    assert "private-login" not in caplog.text


def test_cache_prevents_second_request(tmp_path, monkeypatch):
    obj = collector(tmp_path, monkeypatch)
    obj.collect_group(search())
    second = obj.collect_group(search())
    assert FakeProvider.calls == 1
    assert second["cache_hit"] is True


def test_manual_search_collects_once_on_cache_miss(tmp_path, monkeypatch):
    obj = collector(tmp_path, monkeypatch)
    first = obj.search(search())
    second = obj.search(search())
    assert len(first) == 1
    assert second == first
    assert FakeProvider.calls == 1


def test_collector_uses_confirmed_rest_app_time_payload(tmp_path, monkeypatch):
    obj = collector(tmp_path, monkeypatch)
    obj.collect_group(search(region_id="653700"))
    payload = FakeProvider.payloads[0]
    assert set(payload) == {"category_id", "sort", "limit", "date1", "date2"}
    assert payload["category_id"] == "9"
    assert "last_m" not in payload
    assert "region_id" not in payload


def test_normalization_degraded_mode_prevents_duplicate_request(tmp_path, monkeypatch):
    class InvalidProvider(FakeProvider):
        calls = 0
        payloads = []

        def _post(self, endpoint, params):
            type(self).calls += 1
            return {"status": "ok", "data": [{"Id": "bad", "title": "No price"}]}

    monkeypatch.setattr(arc, "save_avito_history", lambda item: {"status": "new"})
    monkeypatch.setattr(arc, "save_safe_rest_app_sample", lambda payload: None)
    obj = arc.RestAppCollector(
        provider_factory=InvalidProvider, db_path=tmp_path / "degraded.db"
    )
    first = obj.collect_group(search())
    second = obj.collect_group(search())
    assert first["status"] == "normalization_failed"
    assert second["status"] == "normalization_failed"
    assert second["degraded"] is True
    assert InvalidProvider.calls == 1
