"""Тесты резидентского пула прокси Avito."""
import os
from unittest import mock

import pytest

import avito_proxy_pool as app


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json
        return json.loads(self.text)


def _patch_requests_get(monkeypatch, responses):
    """responses: list[(status, text)] or single callable."""
    it = iter(responses)

    def fake_get(*args, **kwargs):
        try:
            status, text = next(it)
        except StopIteration:
            status, text = 503, "No exit node"
        return FakeResponse(status, text)

    monkeypatch.setattr(app._requests, "get", fake_get)


def test_env_builds_port_range(monkeypatch):
    monkeypatch.setenv("AVITO_PROXY_HOST", "pool.proxys.io")
    monkeypatch.setenv("AVITO_PROXY_PORT_START", "10000")
    monkeypatch.setenv("AVITO_PROXY_PORT_END", "10999")
    monkeypatch.setenv("AVITO_PROXY_USERNAME", "u")
    monkeypatch.setenv("AVITO_PROXY_PASSWORD", "p")
    pool = app.AvitoProxyPool()
    assert pool.configured is True
    assert pool.port_start == 10000
    assert pool.port_end == 10999
    assert pool.host == "pool.proxys.io"


def test_secrets_not_logged(monkeypatch):
    monkeypatch.setenv("AVITO_PROXY_HOST", "pool.proxys.io")
    monkeypatch.setenv("AVITO_PROXY_PORT_START", "10000")
    monkeypatch.setenv("AVITO_PROXY_PORT_END", "10005")
    monkeypatch.setenv("AVITO_PROXY_USERNAME", "secret_user")
    monkeypatch.setenv("AVITO_PROXY_PASSWORD", "secret_pass")
    pool = app.AvitoProxyPool()
    summary = pool.summary()
    dumped = str(summary)
    assert "secret_user" not in dumped
    assert "secret_pass" not in dumped

    error = pool._safe_error("Auth failed for secret_user with secret_pass")
    assert "secret_user" not in error
    assert "secret_pass" not in error


@pytest.mark.asyncio
async def test_503_no_exit_node_switches_port(monkeypatch):
    monkeypatch.setenv("AVITO_PROXY_HOST", "pool.proxys.io")
    monkeypatch.setenv("AVITO_PROXY_PORT_START", "10000")
    monkeypatch.setenv("AVITO_PROXY_PORT_END", "10002")
    monkeypatch.setenv("AVITO_PROXY_USERNAME", "u")
    monkeypatch.setenv("AVITO_PROXY_PASSWORD", "p")
    monkeypatch.setenv("AVITO_PROXY_CHECK_LIMIT", "3")

    _patch_requests_get(monkeypatch, [
        (503, "Err-Msg: Unable to assign node to port as not match"),
        (503, "No exit node"),
        (200, '{"ip": "1.2.3.4"}'),
    ])

    pool = app.AvitoProxyPool()
    endpoint = await pool.select_working_proxy()
    assert endpoint.port == 10002
    assert pool.summary()["last_attempted_ports_count"] == 3


@pytest.mark.asyncio
async def test_407_returns_proxy_auth(monkeypatch):
    monkeypatch.setenv("AVITO_PROXY_HOST", "pool.proxys.io")
    monkeypatch.setenv("AVITO_PROXY_PORT_START", "10000")
    monkeypatch.setenv("AVITO_PROXY_PORT_END", "10002")
    monkeypatch.setenv("AVITO_PROXY_USERNAME", "u")
    monkeypatch.setenv("AVITO_PROXY_PASSWORD", "p")

    _patch_requests_get(monkeypatch, [(407, "Proxy Authentication Required")])

    pool = app.AvitoProxyPool()
    res = await pool.check_proxy(10000)
    assert res["ok"] is False
    assert res["error"] == "proxy_auth"


@pytest.mark.asyncio
async def test_timeout_switches_port(monkeypatch):
    monkeypatch.setenv("AVITO_PROXY_HOST", "pool.proxys.io")
    monkeypatch.setenv("AVITO_PROXY_PORT_START", "10000")
    monkeypatch.setenv("AVITO_PROXY_PORT_END", "10001")
    monkeypatch.setenv("AVITO_PROXY_USERNAME", "u")
    monkeypatch.setenv("AVITO_PROXY_PASSWORD", "p")

    from requests.exceptions import ConnectTimeout
    monkeypatch.setattr(
        app._requests, "get", mock.MagicMock(side_effect=ConnectTimeout("timeout"))
    )

    pool = app.AvitoProxyPool()
    endpoint = await pool.select_working_proxy()
    assert endpoint is None


@pytest.mark.asyncio
async def test_working_port_reused(monkeypatch):
    monkeypatch.setenv("AVITO_PROXY_HOST", "pool.proxys.io")
    monkeypatch.setenv("AVITO_PROXY_PORT_START", "10000")
    monkeypatch.setenv("AVITO_PROXY_PORT_END", "10005")
    monkeypatch.setenv("AVITO_PROXY_USERNAME", "u")
    monkeypatch.setenv("AVITO_PROXY_PASSWORD", "p")
    monkeypatch.setenv("AVITO_PROXY_STICKY_MINUTES", "0")

    _patch_requests_get(monkeypatch, [
        (200, '{"ip": "1.2.3.4"}'),
        (200, '{"ip": "1.2.3.4"}'),
    ])

    pool = app.AvitoProxyPool()
    p1 = await pool.select_working_proxy()
    p2 = await pool.select_working_proxy()
    assert p1.port == p2.port == 10000
    assert pool.summary()["last_attempted_ports_count"] == 1


@pytest.mark.asyncio
async def test_check_limit_respected(monkeypatch):
    monkeypatch.setenv("AVITO_PROXY_HOST", "pool.proxys.io")
    monkeypatch.setenv("AVITO_PROXY_PORT_START", "10000")
    monkeypatch.setenv("AVITO_PROXY_PORT_END", "10999")
    monkeypatch.setenv("AVITO_PROXY_USERNAME", "u")
    monkeypatch.setenv("AVITO_PROXY_PASSWORD", "p")
    monkeypatch.setenv("AVITO_PROXY_CHECK_LIMIT", "5")

    _patch_requests_get(monkeypatch, [(503, "No exit node")] * 5)

    pool = app.AvitoProxyPool()
    endpoint = await pool.select_working_proxy()
    assert endpoint is None
    assert pool.summary()["last_attempted_ports_count"] == 5


@pytest.mark.asyncio
async def test_port_stable_during_search(monkeypatch):
    """Порт не меняется во время одного поиска — current_port фиксируется."""
    monkeypatch.setenv("AVITO_PROXY_HOST", "pool.proxys.io")
    monkeypatch.setenv("AVITO_PROXY_PORT_START", "10000")
    monkeypatch.setenv("AVITO_PROXY_PORT_END", "10005")
    monkeypatch.setenv("AVITO_PROXY_USERNAME", "u")
    monkeypatch.setenv("AVITO_PROXY_PASSWORD", "p")

    _patch_requests_get(monkeypatch, [(200, '{"ip": "1.2.3.4"}')])

    pool = app.AvitoProxyPool()
    endpoint = await pool.select_working_proxy()
    assert endpoint.port == 10000
    current = pool.get_current_proxy()
    assert current.port == 10000
    assert pool.get_playwright_config()["server"] == "http://pool.proxys.io:10000"


@pytest.mark.asyncio
async def test_mark_failed_rotates_to_next_port(monkeypatch):
    monkeypatch.setenv("AVITO_PROXY_HOST", "pool.proxys.io")
    monkeypatch.setenv("AVITO_PROXY_PORT_START", "10000")
    monkeypatch.setenv("AVITO_PROXY_PORT_END", "10002")
    monkeypatch.setenv("AVITO_PROXY_USERNAME", "u")
    monkeypatch.setenv("AVITO_PROXY_PASSWORD", "p")

    _patch_requests_get(monkeypatch, [
        (200, '{"ip": "1.2.3.4"}'),
        (200, '{"ip": "5.6.7.8"}'),
    ])

    pool = app.AvitoProxyPool()
    p1 = await pool.select_working_proxy()
    await pool.mark_failed(p1.port, "no_exit_node")
    p2 = await pool.select_working_proxy()
    assert p1.port == 10000
    assert p2.port == 10001


def test_pool_not_configured_without_credentials(monkeypatch):
    monkeypatch.setenv("AVITO_PROXY_HOST", "pool.proxys.io")
    monkeypatch.setenv("AVITO_PROXY_PORT_START", "10000")
    monkeypatch.setenv("AVITO_PROXY_PORT_END", "10005")
    monkeypatch.delenv("AVITO_PROXY_USERNAME", raising=False)
    monkeypatch.delenv("AVITO_PROXY_PASSWORD", raising=False)
    pool = app.AvitoProxyPool()
    assert pool.configured is False
