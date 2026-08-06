"""Поведение при выключенном Авито.

Провайдер по умолчанию — рабочий webjson (см. WORKING_CONFIG.md), поэтому
«выключено» проверяется явной настройкой AVITO_PROVIDER=disabled, а не
отсутствием переменной.
"""
from __future__ import annotations

import control_bot as cb
from avito_provider_config import get_avito_provider


def test_default_provider_is_the_working_one(monkeypatch):
    monkeypatch.delenv("AVITO_PROVIDER", raising=False)
    monkeypatch.delenv("AVITO_SOURCE", raising=False)
    assert get_avito_provider() == "webjson"


def test_avito_can_be_switched_off_explicitly(monkeypatch):
    monkeypatch.setenv("AVITO_PROVIDER", "disabled")
    monkeypatch.delenv("AVITO_SOURCE", raising=False)
    assert get_avito_provider() == "disabled"


def test_source_keyboard_follows_the_enabled_list(monkeypatch):
    """Выключенный Авито не должен появляться в выборе площадок."""
    monkeypatch.setattr(cb, "ALL_SOURCES", ["drom", "autoru", "youla", "vk", "tg"])
    keyboard = cb.sources_keyboard(cb.ALL_SOURCES)
    callback_data = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    }
    assert "toggle_src|avito" not in callback_data


def test_legacy_user_sources_drop_avito(monkeypatch):
    monkeypatch.setattr(cb, "ALL_SOURCES", ["drom", "autoru", "youla", "vk", "tg"])
    settings = {"sources": ["avito", "drom", "autoru"]}
    assert cb._get_enabled_sources(settings) == ["drom", "autoru"]


def test_legacy_monitor_sources_drop_avito(monkeypatch):
    monkeypatch.setattr(cb, "AVITO_ENABLED", False)
    settings = {"monitor_sources": ["avito", "drom", "tg"]}
    assert cb._monitor_sources(settings) == ["drom", "tg"]


def test_disabled_avito_search_returns_nothing(monkeypatch):
    monkeypatch.setattr(cb, "AVITO_ENABLED", False)
    monkeypatch.setattr(cb, "RestAppAvitoProvider", None)
    assert cb.scrape_avito("krasnodar") == []
