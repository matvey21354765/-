from __future__ import annotations

import pytest

from avito_proxy_config import (
    AvitoProxyConfigError,
    build_mobile_proxy_config,
)
from marketplace_result import classify_network_error


VARS = (
    "AVITO_PROXY_HOST",
    "AVITO_PROXY_PORT",
    "AVITO_PROXY_PROTOCOL",
    "AVITO_PROXY_USER",
    "AVITO_PROXY_PASS",
    "PROXY_URL",
)


@pytest.fixture(autouse=True)
def clean_proxy_env(monkeypatch):
    for name in VARS:
        monkeypatch.delenv(name, raising=False)


def test_avito_variables_have_priority(monkeypatch):
    monkeypatch.setenv("AVITO_PROXY_HOST", "mproxy.site")
    monkeypatch.setenv("AVITO_PROXY_PORT", "17412")
    monkeypatch.setenv("AVITO_PROXY_PROTOCOL", "http")
    monkeypatch.setenv("AVITO_PROXY_USER", "mobile")
    monkeypatch.setenv("AVITO_PROXY_PASS", "secret")
    monkeypatch.setenv("PROXY_URL", "http://old:old@old.example:16358")
    config = build_mobile_proxy_config()
    assert (config.host, config.port) == ("mproxy.site", 17412)


def test_compatibility_fallback_is_supported(monkeypatch):
    monkeypatch.setenv("PROXY_URL", "http://u:p@fallback.example:8080")
    config = build_mobile_proxy_config()
    assert config.host == "fallback.example"
    assert config.proxies["http"] == config.proxies["https"]


def test_missing_proxy_is_controlled_error():
    with pytest.raises(AvitoProxyConfigError):
        build_mobile_proxy_config()


def test_fallback_credentials_are_decoded_then_encoded(monkeypatch):
    monkeypatch.setenv(
        "PROXY_URL", "http://u%40name:p%2Fword@fallback.example:8080"
    )
    config = build_mobile_proxy_config()
    assert "u%40name:p%2Fword@" in config.proxy_url


@pytest.mark.parametrize(
    ("message", "status", "error_class"),
    [
        ("CONNECT tunnel failed, response 407", "proxy_auth", "ProxyAuthenticationError"),
        ("CONNECT tunnel failed, response 403", "proxy_denied", "ProxyAccessDeniedError"),
        ("Connection timed out", "network_error", "ProxyConnectionError"),
    ],
)
def test_proxy_error_classification(message, status, error_class):
    assert classify_network_error(RuntimeError(message)) == (status, error_class)
