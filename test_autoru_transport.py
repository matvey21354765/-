from __future__ import annotations

import logging

from autoru_transport import autoru_proxies, get_autoru_transport


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
