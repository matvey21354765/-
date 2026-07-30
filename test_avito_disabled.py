from __future__ import annotations

import control_bot as cb


def test_avito_enabled_is_false_by_default():
    assert cb.AVITO_ENABLED is False


def test_avito_is_absent_from_user_source_keyboard():
    assert "avito" not in cb.ALL_SOURCES
    keyboard = cb.sources_keyboard(cb.ALL_SOURCES)
    callback_data = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    }
    assert "toggle_src|avito" not in callback_data


def test_legacy_user_sources_drop_avito():
    settings = {"sources": ["avito", "drom", "autoru"]}
    assert cb._get_enabled_sources(settings) == ["drom", "autoru"]


def test_legacy_monitor_sources_drop_avito():
    settings = {"monitor_sources": ["avito", "drom", "tg"]}
    assert cb._monitor_sources(settings) == ["drom", "tg"]


def test_disabled_avito_search_does_not_initialize_provider(monkeypatch):
    monkeypatch.setattr(cb, "RestAppAvitoProvider", None)
    assert cb.scrape_avito("krasnodar") == []
