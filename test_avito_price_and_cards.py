"""Цена объявления Авито и постраничная навигация по разделам.

Скриншоты показали две поломки: с Авито в выдачу попадало 1-2 объявления
(карточки без цены в JSON выбрасывались целиком) и под страницей раздела не
было кнопки «показать ещё».
"""
from __future__ import annotations

import control_bot


def test_price_from_price_detailed():
    text, value = control_bot._avito_price_from_item(
        {"priceDetailed": {"string": "191 000", "fullString": "191 000 ₽"}})
    assert value == 191_000
    assert "191" in text


def test_price_found_in_params_list():
    """Новый формат каталога держит цену в списке — раньше списки не смотрели
    вообще, объявление уходило в отброс."""
    _, value = control_bot._avito_price_from_item({
        "params": [
            {"title": "Пробег", "value": 247_652},
            {"title": "Цена", "valueText": "74 000 ₽"},
        ],
    })
    assert value == 74_000


def test_mileage_is_never_taken_for_price():
    _, value = control_bot._avito_price_from_item({
        "params": [{"title": "Пробег", "valueText": "247 652 км"}],
    })
    assert value == 0


def test_item_without_price_uses_rouble_amount_from_blob():
    item = {
        "title": "ВАЗ (LADA) 2115 Samara 1.6 MT, 2010, 247 652 км",
        "urlPath": "/perm/avtomobili/vaz_2115_1234567",
        "badges": [{"title": "Цена 74 000 ₽"}],
    }
    parsed = control_bot._avito_item_from_json(item, control_bot.datetime.date.today())
    assert parsed is not None
    assert parsed["_price_int"] == 74_000


def test_item_with_only_mileage_in_title_is_dropped():
    item = {
        "title": "ВАЗ (LADA) 2106, 2001, 95 000 км",
        "urlPath": "/perm/avtomobili/vaz_2106_7654321",
    }
    assert control_bot._avito_item_from_json(
        item, control_bot.datetime.date.today()) is None


def _texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def test_nav_keyboard_offers_more_when_listings_left():
    kb = control_bot._ps_nav_keyboard("fresh", 0, 10, 42)
    texts = _texts(kb)
    assert any("Показать ещё (32)" in t for t in texts)
    assert not any("Назад" in t for t in texts)
    assert any("Все разделы" in t for t in texts)


def test_nav_keyboard_on_last_page_has_no_more_button():
    kb = control_bot._ps_nav_keyboard("fresh", 4, 2, 42)
    texts = _texts(kb)
    assert not any("Показать ещё" in t for t in texts)
    assert any("Назад" in t for t in texts)


def test_card_keyboard_has_no_pagination_button():
    kb = control_bot._ps_card_keyboard(1, "avito:1", category="fresh", page=0)
    assert not any("Следующая" in t for t in _texts(kb))
    assert any("Открыть" in t for t in _texts(kb))
