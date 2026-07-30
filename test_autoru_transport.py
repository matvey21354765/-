from __future__ import annotations

import logging
from types import SimpleNamespace

from autoru_transport import (
    autoru_captcha_detected,
    autoru_proxies,
    get_autoru_transport,
    proxy_host_safe,
)
import control_bot as cb


def test_without_autoru_proxy_uses_direct_and_ignores_proxy_url(monkeypatch):
    monkeypatch.delenv("AUTORU_PROXY_URL", raising=False)
    monkeypatch.setenv("PROXY_URL", "http://secret:secret@old.invalid:8080")
    assert get_autoru_transport() == {"mode": "direct", "proxy_url": None}
    assert autoru_proxies() is None


def test_standard_proxy_environment_is_ignored(monkeypatch):
    monkeypatch.delenv("AUTORU_PROXY_URL", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://old.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://old.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "socks5://old.invalid:1080")
    assert get_autoru_transport()["mode"] == "direct"
    assert autoru_proxies() is None


def test_explicit_autoru_proxy_is_the_only_proxy(monkeypatch):
    dedicated = "http://user:pass@autoru.invalid:8080"
    monkeypatch.setenv("AUTORU_PROXY_URL", dedicated)
    monkeypatch.setenv("PROXY_URL", "http://old.invalid:8080")
    assert get_autoru_transport() == {
        "mode": "proxy",
        "proxy_url": dedicated,
    }
    assert autoru_proxies() == {"http": dedicated, "https": dedicated}


def test_transport_helper_does_not_log_credentials(monkeypatch, caplog):
    monkeypatch.setenv(
        "AUTORU_PROXY_URL",
        "http://private-user:private-pass@autoru.invalid:8080",
    )
    with caplog.at_level(logging.DEBUG):
        get_autoru_transport()
        autoru_proxies()
    assert "private-user" not in caplog.text
    assert "private-pass" not in caplog.text


def test_proxy_safe_host_never_contains_credentials(monkeypatch):
    monkeypatch.setenv(
        "AUTORU_PROXY_URL",
        "http://private-user:private-pass@proxy.example:17412",
    )
    assert proxy_host_safe() == "proxy.example:17412"


def test_showcaptcha_redirect_is_restriction():
    assert autoru_captcha_detected(
        200,
        "https://auto.ru/showcaptcha?retpath=%2Fcars",
        "<html></html>",
    )


def test_captcha_markers_and_statuses_are_restrictions():
    assert autoru_captcha_detected(200, "https://auto.ru/cars", "SmartCaptcha")
    assert autoru_captcha_detected(403, "https://auto.ru/cars", "")
    assert autoru_captcha_detected(429, "https://auto.ru/cars", "")


def test_normal_listing_page_is_not_captcha():
    assert not autoru_captcha_detected(
        200,
        "https://auto.ru/krasnodar/cars/used/",
        "<html><title>Автомобили</title></html>",
    )


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self.response

    def close(self):
        return None


def _response(*, url, html, status=200):
    return SimpleNamespace(
        status_code=status,
        url=url,
        text=html,
        content=html.encode(),
        headers={"content-type": "text/html; charset=utf-8"},
    )


def test_http_200_showcaptcha_stops_before_parser(monkeypatch):
    fake = _FakeSession(
        _response(
            url="https://auto.ru/showcaptcha",
            html="<html>SmartCaptcha</html>",
        )
    )
    monkeypatch.setattr(
        "curl_cffi.requests.Session",
        lambda **kwargs: fake,
    )
    monkeypatch.setattr(
        cb,
        "_autoru_parse_html",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("parser must not run for CAPTCHA")
        ),
    )
    assert cb.scrape_autoru("krasnodar") == []
    assert fake.calls == 1
    assert cb._AUTORU_LAST_DIAG["error_type"] == "restriction_captcha"


def test_http_200_normal_page_is_parsed_once(monkeypatch):
    fake = _FakeSession(
        _response(
            url="https://auto.ru/krasnodar/cars/used/",
            html="<html>normal listing page</html>",
        )
    )
    listing = {
        "source": "autoru",
        "source_id": "42",
        "url": "https://auto.ru/cars/used/sale/42/",
        "_price_int": 90000,
    }
    monkeypatch.setattr(
        "curl_cffi.requests.Session",
        lambda **kwargs: fake,
    )
    monkeypatch.setattr(cb, "_autoru_parse_html", lambda *args: [listing])
    assert cb.scrape_autoru("krasnodar", price_max=100000) == [listing]
    assert fake.calls == 1
    assert cb._AUTORU_LAST_DIAG["error_type"] == ""
