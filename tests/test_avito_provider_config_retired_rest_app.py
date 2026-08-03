from __future__ import annotations

import os

from avito_provider_config import get_avito_provider


def _clear_provider_env(monkeypatch) -> None:
    for name in (
        "AVITO_PROVIDER",
        "AVITO_SOURCE",
        "AVITO_ENABLED",
        "REST_APP_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


def test_rest_app_provider_is_migrated_to_playwright(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("AVITO_PROVIDER", "rest_app")
    monkeypatch.setenv("REST_APP_ENABLED", "true")

    assert get_avito_provider() == "playwright"
    assert os.environ["REST_APP_ENABLED"] == "false"


def test_legacy_rest_app_source_is_migrated_to_playwright(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("AVITO_SOURCE", "rest_app")

    assert get_avito_provider() == "playwright"
    assert os.environ["REST_APP_ENABLED"] == "false"


def test_enabled_avito_defaults_to_playwright(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("AVITO_ENABLED", "true")

    assert get_avito_provider() == "playwright"


def test_disabled_remains_disabled(monkeypatch) -> None:
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("AVITO_PROVIDER", "disabled")

    assert get_avito_provider() == "disabled"
