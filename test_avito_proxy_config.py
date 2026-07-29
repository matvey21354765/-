from __future__ import annotations

import pytest

from avito_duff_provider import (
    AvitoBlockedError,
    AvitoProxyConnectionError,
    AvitoRateLimitedError,
)
from avito_proxy_config import AvitoProxyConfig
from scripts import check_avito_proxy


class Response:
    def __init__(self, status=200, ip="203.0.113.10"):
        self.status_code = status
        self._ip = ip

    def json(self):
        return {"ip": self._ip}


class Session:
    response = Response()
    calls = []

    def __init__(self, **kwargs):
        self.cookies = self

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def clear(self):
        pass

    def close(self):
        pass


class GoodProvider:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.last_diagnostics = {
            "http": 200,
            "title": "Авито",
            "catalog_items": 1,
        }

    def search(self, url):
        return [{
            "title": "Автомобиль",
            "price": 100000,
            "url": "https://www.avito.ru/item/1",
        }]


@pytest.fixture(autouse=True)
def reset_session():
    Session.response = Response()
    Session.calls = []


def test_existing_environment_names(monkeypatch):
    monkeypatch.setenv("AVITO_PROXY_HOST", "mproxy.site")
    monkeypatch.setenv("AVITO_PROXY_PORT", "17412")
    monkeypatch.setenv("AVITO_PROXY_PROTOCOL", "http")
    monkeypatch.setenv("AVITO_PROXY_USER", "user")
    monkeypatch.setenv("AVITO_PROXY_PASS", "pass")
    config = AvitoProxyConfig.from_env()
    assert config.host == "mproxy.site"
    assert config.credentials_present is True


def test_credentials_are_url_encoded():
    config = AvitoProxyConfig(
        host="mproxy.site",
        port=17412,
        protocol="http",
        user="u@s:er",
        password="p/a?ss#",
    )
    assert "u%40s%3Aer" in config.proxy_url
    assert "p%2Fa%3Fss%23" in config.proxy_url
    assert "p/a?ss#" not in config.proxy_url
    assert "p/a?ss#" not in repr(config)


def test_http_proxy_applies_to_http_and_https():
    config = AvitoProxyConfig(
        host="mproxy.site", port=17412, protocol="http"
    )
    assert config.proxies["http"] == config.proxy_url
    assert config.proxies["https"] == config.proxy_url


def test_socks5_is_normalized_to_remote_dns(monkeypatch):
    monkeypatch.setenv("AVITO_PROXY_HOST", "bproxy.site")
    monkeypatch.setenv("AVITO_PROXY_PORT", "17412")
    monkeypatch.setenv("AVITO_PROXY_PROTOCOL", "socks5")
    config = AvitoProxyConfig.from_env()
    assert config.proxy_url.startswith("socks5h://")


def test_timeout_is_limited_to_30_seconds(monkeypatch):
    monkeypatch.setenv("AVITO_PROXY_HOST", "mproxy.site")
    monkeypatch.setenv("AVITO_PROXY_PORT", "17412")
    monkeypatch.setenv("AVITO_PROXY_TIMEOUT", "999")
    assert AvitoProxyConfig.from_env().timeout == 30


def test_ipify_timeout_stops_before_avito():
    Session.response = TimeoutError("timeout")

    class ForbiddenProvider:
        def __init__(self, **kwargs):
            raise AssertionError("direct/proxy Avito fallback is forbidden")

    report = check_avito_proxy.run_diagnostic(
        AvitoProxyConfig("mproxy.site", 17412, "http"),
        session_factory=Session,
        provider_factory=ForbiddenProvider,
        canonical_url="https://www.avito.ru/search",
    )
    assert report["error_type"] == "proxy_connection"
    assert len(Session.calls) == 1


def test_proxy_407_stops_before_avito():
    Session.response = Response(status=407)
    report = check_avito_proxy.run_diagnostic(
        AvitoProxyConfig("mproxy.site", 17412, "http"),
        session_factory=Session,
        provider_factory=GoodProvider,
        canonical_url="https://www.avito.ru/search",
    )
    assert report["error_type"] == "proxy_authentication"
    assert report["avito_http"] is None


@pytest.mark.parametrize(
    ("exception", "http", "error_type"),
    [
        (AvitoBlockedError("blocked", 403), 403, "blocked"),
        (AvitoRateLimitedError("limited", 429), 429, "rate_limited"),
    ],
)
def test_avito_block_status_has_no_retry(
    monkeypatch, exception, http, error_type
):
    monkeypatch.setattr(check_avito_proxy, "_set_fixed_cooldown", lambda code: None)
    calls = []

    class Provider:
        def __init__(self, **kwargs):
            self.last_diagnostics = {"http": http}

        def search(self, url):
            calls.append(url)
            raise exception

    report = check_avito_proxy.run_diagnostic(
        AvitoProxyConfig("mproxy.site", 17412, "http"),
        session_factory=Session,
        provider_factory=Provider,
        canonical_url="https://www.avito.ru/search",
    )
    assert report["avito_http"] == http
    assert report["error_type"] == error_type
    assert len(calls) == 1


def test_successful_catalog_response():
    report = check_avito_proxy.run_diagnostic(
        AvitoProxyConfig("mproxy.site", 17412, "http"),
        session_factory=Session,
        provider_factory=GoodProvider,
        canonical_url="https://www.avito.ru/search",
    )
    assert report["external_ip"] == "203.0.113.10"
    assert report["avito_http"] == 200
    assert report["catalog_items_found"] is True
    assert report["items_count"] == 1


def test_provider_connection_error_does_not_fallback():
    calls = []

    class Provider:
        def __init__(self, **kwargs):
            self.last_diagnostics = {}

        def search(self, url):
            calls.append(url)
            raise AvitoProxyConnectionError("proxy unavailable")

    report = check_avito_proxy.run_diagnostic(
        AvitoProxyConfig("mproxy.site", 17412, "http"),
        session_factory=Session,
        provider_factory=Provider,
        canonical_url="https://www.avito.ru/search",
    )
    assert report["error_type"] == "proxy_connection"
    assert len(calls) == 1
