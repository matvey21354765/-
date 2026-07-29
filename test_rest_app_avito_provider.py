from __future__ import annotations

import time

import pytest

from rest_app_avito_provider import (
    RestAppAvitoProvider,
    RestAppConfig,
    RestAppRateLimitedError,
)


@pytest.fixture(autouse=True)
def reset_provider_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REST_APP_DB_PATH", str(tmp_path / "rest-app.db"))
    RestAppAvitoProvider._cache.clear()
    RestAppAvitoProvider._key_locks.clear()
    RestAppAvitoProvider._regions_cache = None
    RestAppAvitoProvider._cities_cache.clear()
    RestAppAvitoProvider._cooldown_until = 0


def provider():
    return RestAppAvitoProvider(RestAppConfig("login", "token"), cache_ttl=120)


def test_normalize_required_fields():
    item = provider()._normalize({
        "avito_id": "123",
        "title": "Lada Vesta, 2020",
        "price": "750 000",
        "url": "https://www.avito.ru/item/123",
        "images": "https://img/1.jpg,https://img/2.jpg",
        "city": "Новосибирск",
        "time": "2026-07-29 10:00:00",
        "params": [{"name": "Год выпуска", "value": "2020"}],
    })
    assert item["id"] == "123"
    assert item["price"] == 750000
    assert item["image"] == "https://img/1.jpg"
    assert item["source"] == "avito"
    assert item["year"] == 2020


def test_identical_search_uses_cache(monkeypatch):
    client = provider()
    calls = []

    monkeypatch.setattr(client, "regions", lambda: [{"id": "1", "name": "Область"}])
    monkeypatch.setattr(client, "cities", lambda _: [{"id": "2", "name": "Город"}])

    def fake_post(endpoint, params=None):
        calls.append((endpoint, params))
        client.last_diagnostics["http"] = 200
        return {"status": "ok", "data": [{
            "avito_id": "123", "title": "Lada 2020", "price": "100",
            "city": "Город", "url": "https://www.avito.ru/item/123",
        }]}

    monkeypatch.setattr(client, "_post", fake_post)
    kwargs = dict(
        region_name="Область", city_name="Город", price_min=0,
        price_max=1000, brand="Lada",
    )
    first = client.search(**kwargs)
    second = client.search(**kwargs)
    assert first == second
    assert len(calls) == 1
    assert client.last_diagnostics["cache_hit"] is True


def test_deduplicates_by_avito_id(monkeypatch):
    client = provider()
    monkeypatch.setattr(client, "regions", lambda: [{"id": "1", "name": "Область"}])
    monkeypatch.setattr(client, "cities", lambda _: [{"id": "2", "name": "Город"}])
    monkeypatch.setattr(client, "_post", lambda *_args, **_kwargs: {
        "status": "ok",
        "data": [
            {"avito_id": "1", "title": "A", "price": "100"},
            {"avito_id": "1", "title": "A new", "price": "100"},
        ],
    })
    items = client.search(region_name="Область", city_name="Город")
    assert len(items) == 1
    assert items[0]["title"] == "A new"


def test_active_cooldown_makes_no_request(monkeypatch):
    client = provider()
    RestAppAvitoProvider._cooldown_until = time.time() + 60
    monkeypatch.setattr(
        client, "_post", lambda *_args, **_kwargs: pytest.fail("request made")
    )
    with pytest.raises(RestAppRateLimitedError):
        client.search(region_name="Область", city_name="Город")


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        ({"data": [{"Id": "1"}]}, "list"),
        ({"data": {"items": [{"Id": "1"}]}}, "dict.items"),
        ({"items": [{"Id": "1"}]}, "items"),
        ({"results": [{"Id": "1"}]}, "results"),
    ],
)
def test_extracts_actual_response_variants(payload, kind):
    rows, raw_type = RestAppAvitoProvider._extract_raw_items(payload)
    assert rows[0]["Id"] == "1"
    assert raw_type == kind


def test_demo_record_keeps_source_id_and_hidden_url():
    item = provider()._normalize({
        "Id": "777",
        "avito_id": "hidden_in_demo",
        "url": "hidden_in_demo",
        "title": "Toyota Camry",
        "price": "900000",
        "time": "2026-07-29 12:00:00",
        "region": "Новосибирская область",
        "city": "Новосибирск",
        "district": "Центральный",
        "images": ["https://img/first.jpg", "https://img/second.jpg"],
        "params": [
            {"name": "Год выпуска", "value": "2014"},
            {"name": "Пробег, км", "value": "120000"},
            {"name": "Мощность двигателя, л.с.", "value": "181"},
        ],
    })
    assert item["id"] == "777"
    assert item["source_id"] == "777"
    assert item["url"] is None
    assert item["demo_url_hidden"] is True
    assert item["location"] == "Новосибирская область, Новосибирск, Центральный"
    assert item["image"] == "https://img/first.jpg"
    assert item["specs"]["mileage"] == "120000"
    assert item["specs"]["power"] == "181"
