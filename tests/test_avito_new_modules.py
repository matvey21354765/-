"""Тесты новых модулей Avito: REST, normalizer, history, deal_score, seller, AI."""
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

import avito_history as avh
from avito_normalizer import normalize_avito_item, normalize_rest_app_items
from avito_history import save_avito_history, get_avito_history
from deal_score import calculate_deal_score
from seller_analyzer import analyze_seller, _seller_key
from ai_car_analyzer import analyze_car_text, extract_brand_model
from avito_rest_provider import AvitoRestProvider


class FakeRestProvider:
    def __init__(self, items):
        self.items = items
        self.last_diagnostics = {"http": 200, "after_private": len(items)}

    def search(self, **kwargs):
        return list(self.items)


@pytest.fixture(autouse=True)
def isolate_history_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setattr(avh, "_DB_PATH", Path(tmp.name))
    monkeypatch.setattr(avh, "_INIT_DONE", False)
    yield
    try:
        os.unlink(tmp.name)
    except Exception:
        pass


def test_normalize_rest_app_item():
    raw = {
        "source_id": "abc123",
        "title": "BMW X5 2016",
        "price": 900000,
        "marka": "BMW",
        "model": "X5",
        "region": "Москва",
        "city": "Москва",
        "description": "Хорошее состояние",
        "phone": "+79990000000",
        "seller": "Иван",
        "seller_type": "private",
        "images": ["https://img1.jpg", "https://img2.jpg"],
        "url": "https://avito.ru/item/abc123",
        "published_at": "2026-07-31 12:00:00",
        "specs": {"mileage": "120 000 км"},
    }
    item = normalize_avito_item(raw, source="rest_app")
    assert item["source"] == "rest_app"
    assert item["avito_id"] == "abc123"
    assert item["price"] == 900000
    assert item["brand"] == "BMW"
    assert item["model"] == "X5"
    assert item["city"] == "Москва"
    assert item["seller"] == "Иван"
    assert len(item["images"]) == 2
    assert item["url"].startswith("http")


def test_normalize_rest_app_items_list():
    items = [
        {"title": "Lada Granta 2018", "price": 400000, "source_id": "x1"},
        {"title": "Toyota Camry 2020", "price": 1500000, "source_id": "x2"},
    ]
    normalized = normalize_rest_app_items(items)
    assert len(normalized) == 2
    assert normalized[0]["price"] == 400000


def test_deal_score_below_market():
    item = {
        "title": "BMW X5 2016",
        "price": 900000,
        "images": ["https://img.jpg"],
        "seller_type": "private",
        "description": "Срочно продам, торг уместен",
        "mileage": 50000,
        "year": 2016,
    }
    result = calculate_deal_score(item, market_price=1_100_000)
    assert result["score"] >= 70
    assert result["potential_profit"] == 200_000
    assert "below_market" in result["flags"]


def test_deal_score_junk_phrase():
    item = {
        "title": "Двигатель на BMW X5",
        "price": 50000,
        "description": "разбор, запчасти",
        "images": [],
        "seller_type": "private",
    }
    result = calculate_deal_score(item)
    assert result["score"] < 50


def test_seller_analyzer_dealer_by_volume():
    item = {
        "title": "BMW X5 2016",
        "seller": "АвтоСалон Моторс",
        "seller_type": "dealer",
        "phone": "+79990000001",
        "city": "Москва",
    }
    history = [{"city": "СПб"}, {"city": "Казань"}, {"city": "Москва"}]
    result = analyze_seller(item, history=history)
    assert result["type"] == "dealer"
    assert result["type_label"] == "ПЕРЕКУП/ДИЛЕР"
    assert result["listing_count"] == 3


def test_seller_analyzer_private():
    item = {
        "title": "Lada Granta 2015",
        "seller": "Иван",
        "seller_type": "private",
        "phone": "+79990000002",
        "city": "Екатеринбург",
        "description": "Собственник, один хозяин",
    }
    result = analyze_seller(item, history=[])
    assert result["type"] == "private"
    assert result["type_label"] == "ЧАСТНИК"


def test_seller_key_phone():
    item = {"phone": "+7 (999) 000-00-00", "seller": "Иван"}
    assert _seller_key(item) == "phone:9990000000"


def test_ai_car_analyzer_positives_and_risks():
    item = {
        "title": "BMW X5 2016",
        "description": "Срочно продам. После ДТП. Требует ремонта. Только звонки.",
        "price": 800000,
        "images": [],
    }
    result = analyze_car_text(item)
    assert "срочно" in [p.lower() for p in result["positives"]]
    assert "после дтп" in [n.lower() for n in result["negatives"]]
    assert any("развод" in r.lower() or "площадки" in r.lower() for r in result["risks"])
    assert "проверьте" in result["recommendation"].lower()


def test_extract_brand_model():
    assert extract_brand_model("BMW X5 2016") == {"brand": "BMW", "model": "X5"}
    assert extract_brand_model("Toyota Camry") == {"brand": "TOYOTA", "model": "Camry"}


def test_history_save_and_update():
    item = {
        "avito_id": "id-001",
        "title": "BMW X5",
        "price": 900000,
        "brand": "BMW",
        "model": "X5",
        "year": 2016,
        "city": "Москва",
        "seller": "Иван",
        "phone": "+79990000000",
        "url": "https://avito.ru/item/id-001",
        "images": ["https://img.jpg"],
        "description": "Хорошее авто",
    }
    r1 = save_avito_history(item)
    assert r1["status"] == "new"
    r2 = save_avito_history({**item, "price": 850000})
    assert r2["status"] == "price_changed"
    assert r2["old_price"] == 900000
    assert r2["new_price"] == 850000

    hist = get_avito_history("id-001")
    assert hist is not None
    assert hist["price"] == 850000
    assert len(hist["price_history"]) >= 2


@pytest.mark.asyncio
async def test_avito_rest_provider_search(monkeypatch):
    raw_items = [
        {
            "source_id": "r1",
            "title": "BMW X5 2016",
            "price": 900000,
            "marka": "BMW",
            "model": "X5",
            "region": "Москва",
            "city": "Москва",
            "description": "Срочно",
            "images": "https://img.jpg",
            "url": "https://avito.ru/item/r1",
            "time": "2026-07-31 12:00:00",
        }
    ]
    monkeypatch.setenv("REST_APP_LOGIN", "test")
    monkeypatch.setenv("REST_APP_TOKEN", "test")

    provider = AvitoRestProvider()
    provider._provider = FakeRestProvider(raw_items)
    items = await provider.search_ads(city="Москва", price1=500000, price2=1_000_000)
    assert len(items) == 1
    assert items[0]["title"] == "BMW X5 2016"
    assert items[0]["price"] == 900000
    assert items[0]["source"] == "rest_app"


def test_history_enabled_env():
    # Просто проверяем, что импорт работает при переменной
    os.environ["AVITO_HISTORY_ENABLED"] = "true"
    from avito_history import save_avito_history
    assert save_avito_history is not None
