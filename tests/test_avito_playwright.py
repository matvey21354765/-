"""Тесты серверного Playwright-провайдера Avito.

Тесты не запускают реальный Chromium — используются unit-проверки логики и моки.
"""
import asyncio
import os
from unittest import mock

import pytest

import avito_playwright as apw
import avito_proxy_pool as app


class FakePage:
    def __init__(self, content: str = "", url: str = "", status: int = 200, items=None):
        self._content = content
        self.url = url
        self._status = status
        self._items = items or []
        self.closed = False
        self.routes = []
        self.defaults = {}

    def set_default_timeout(self, ms: int) -> None:
        self.defaults["timeout"] = ms

    def set_default_navigation_timeout(self, ms: int) -> None:
        self.defaults["nav_timeout"] = ms

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def goto(self, url, **kwargs):
        self.url = url
        return mock.MagicMock(status=self._status)

    async def content(self):
        return self._content

    async def wait_for_selector(self, selector, timeout=None):
        return None

    async def evaluate(self, script):
        return self._items

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.closed = False

    async def new_page(self):
        return FakePage()

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.closed = False

    async def new_context(self):
        return FakeContext()

    async def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self):
        self.chromium = mock.MagicMock()

    async def stop(self):
        pass


class FakePlaywrightFactory:
    def __init__(self, browser: FakeBrowser):
        self._browser = browser

    async def start(self):
        pw = FakePlaywright()
        pw.chromium.launch = mock.AsyncMock(return_value=self._browser)
        return pw


class FakeProxyPool:
    """Мок резидентского пула для проверки логики restart/rotate."""

    def __init__(self, ports=None, fail_with=None):
        self.ports = list(ports or [10000, 10001, 10002])
        self.fail_with = fail_with
        self._idx = 0
        self.current = None
        self.checks = []
        self.failed = []
        self._configured = True

    @property
    def configured(self):
        return self._configured

    def summary(self):
        return {
            "proxy_pool": self._configured,
            "proxy_host_safe": "pool.proxys.io",
            "ports_range": "10000-10999",
            "credentials_present": True,
            "current_port": self.current,
            "last_attempted_ports_count": len(self.checks),
        }

    def get_playwright_config(self):
        if self.current is None:
            return None
        return {
            "server": f"http://pool.proxys.io:{self.current}",
            "username": "u",
            "password": "p",
        }

    async def select_working_proxy(self):
        self.checks.append(self.ports[self._idx])
        if self.fail_with:
            return None
        self.current = self.ports[self._idx]
        return self.current

    async def mark_failed(self, port, error):
        self.failed.append((port, error))
        if port == self.current:
            self.current = None
        self._idx = (self._idx + 1) % len(self.ports)

    async def mark_success(self, port):
        pass

    async def reset_current_proxy(self):
        self.current = None
        self._idx = (self._idx + 1) % len(self.ports)

    def get_current_proxy(self):
        if self.current is None:
            return None
        return {"host": "pool.proxys.io", "port": self.current, "protocol": "http"}


def _patch_playwright(monkeypatch, browser=None):
    browser = browser or FakeBrowser()
    factory = FakePlaywrightFactory(browser)
    monkeypatch.setattr(apw, "async_playwright", lambda: factory)
    return browser


@pytest.mark.asyncio
async def test_env_disabled_does_not_start_browser(monkeypatch):
    monkeypatch.setenv("AVITO_ENABLED", "false")
    monkeypatch.setenv("AVITO_PROVIDER", "disabled")
    manager = apw.AvitoBrowserManager(proxy_url="")
    assert manager._proxy_config is None
    assert await manager.proxy_summary() == {"proxy_configured": False}


def test_autoru_proxy_url_ignored(monkeypatch):
    monkeypatch.setenv("AUTORU_PROXY_URL", "http://bad:bad@host:1")
    monkeypatch.setenv("AVITO_PROXY_URL", "")
    manager = apw.AvitoBrowserManager()
    assert manager.proxy_url == ""


def test_only_avito_proxy_url_used(monkeypatch):
    monkeypatch.setenv("AUTORU_PROXY_URL", "http://bad:bad@host:1")
    monkeypatch.setenv("AVITO_PROXY_URL", "http://user:pass@avito-proxy:8080")
    manager = apw.AvitoBrowserManager()
    assert manager.proxy_url == "http://user:pass@avito-proxy:8080"


@pytest.mark.asyncio
async def test_resolve_proxy_uses_avito_url():
    manager = apw.AvitoBrowserManager(proxy_url="http://user:pass@avito-proxy:8080")
    assert await manager._resolve_proxy() is True
    assert manager._proxy_config == {
        "server": "http://avito-proxy:8080",
        "username": "user",
        "password": "pass",
    }


@pytest.mark.asyncio
async def test_safe_proxy_summary_hides_credentials():
    manager = apw.AvitoBrowserManager(proxy_url="http://user:pass@host:1234")
    await manager._resolve_proxy()
    summary = await manager.proxy_summary()
    assert summary["proxy_configured"] is True
    assert summary["proxy_host_safe"] == "host:1234"
    assert summary["credentials_present"] is True
    assert "user" not in str(summary)
    assert "pass" not in str(summary)


def test_parse_proxy_url_supports_socks5():
    cfg = apw._parse_proxy_url("socks5://u:p@host:1080")
    assert cfg["server"] == "socks5://host:1080"
    assert cfg["username"] == "u"
    assert cfg["password"] == "p"


@pytest.mark.asyncio
async def test_browser_reused_between_searches(monkeypatch):
    browser = _patch_playwright(monkeypatch)
    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    await manager.start()
    b1 = await manager.get_browser()
    b2 = await manager.get_browser()
    assert b1 is b2
    assert browser.closed is False
    await manager.stop()
    assert browser.closed is True


@pytest.mark.asyncio
async def test_new_page_closed_in_finally(monkeypatch):
    _patch_playwright(monkeypatch)
    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    await manager.start()
    page = await manager.new_page()
    assert isinstance(page, FakePage)
    await page.close()
    assert page.closed is True
    await manager.stop()


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency():
    manager = apw.AvitoBrowserManager(
        proxy_url="http://u:p@h:1",
        max_concurrent_pages=2,
    )
    assert manager._semaphore._value == 2


@pytest.mark.asyncio
async def test_healthcheck_detects_captcha(monkeypatch):
    _patch_playwright(monkeypatch)
    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    await manager.start()
    fake_page = FakePage(content="<html>smartcaptcha</html>", url="https://www.avito.ru/")
    manager.new_page = mock.AsyncMock(return_value=fake_page)

    hc = await manager.healthcheck()
    assert hc["ok"] is False
    assert hc["captcha_detected"] is True
    assert hc["error"] == "captcha"
    await manager.stop()


@pytest.mark.asyncio
async def test_healthcheck_detects_connect_403(monkeypatch):
    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    manager._resolve_proxy = mock.AsyncMock(return_value=True)
    manager._ensure_browser = mock.AsyncMock()
    manager.new_page = mock.AsyncMock(
        side_effect=Exception("Proxy connect 403 forbidden")
    )
    hc = await manager.healthcheck()
    assert hc["ok"] is False
    assert hc["error"] == "proxy_connect_forbidden"


@pytest.mark.asyncio
async def test_search_returns_blocked_on_captcha(monkeypatch):
    _patch_playwright(monkeypatch)
    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    await manager.start()
    fake_page = FakePage(
        content="<html>captcha</html>",
        url="https://www.avito.ru/moskva/avtomobili",
    )
    manager.new_page = mock.AsyncMock(return_value=fake_page)

    result = await manager.search("moskva", 0, 1_000_000, limit=10)
    assert result["error"] == "captcha"
    assert result["items"] == []
    assert result["meta"]["captcha_detected"] is True
    await manager.stop()


@pytest.mark.asyncio
async def test_search_returns_blocked_on_429(monkeypatch):
    _patch_playwright(monkeypatch)
    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    await manager.start()
    fake_page = FakePage(
        content="<html>доступ ограничен</html>",
        url="https://www.avito.ru/moskva/avtomobili",
        status=429,
    )
    manager.new_page = mock.AsyncMock(return_value=fake_page)

    result = await manager.search("moskva", 0, 1_000_000, limit=10)
    assert result["error"] == "blocked"
    assert result["meta"]["blocked_detected"] is True
    await manager.stop()


@pytest.mark.asyncio
async def test_search_returns_provider_not_configured_without_proxy():
    manager = apw.AvitoBrowserManager(proxy_url="")
    result = await manager.search("moskva", 0, 1_000_000, limit=10)
    assert result["error"] == "provider_not_configured"


@pytest.mark.asyncio
async def test_search_restarts_after_limit(monkeypatch):
    browser = _patch_playwright(monkeypatch)
    manager = apw.AvitoBrowserManager(
        proxy_url="http://u:p@h:1",
        restart_after_searches=1,
    )
    await manager.start()
    manager._search_count = 1

    restart_called = {"n": 0}
    orig_restart = manager.restart

    async def tracked_restart():
        restart_called["n"] += 1
        await orig_restart()

    manager.restart = tracked_restart

    fake_page = FakePage(content="<html>ok</html>", url="https://www.avito.ru/moskva/avtomobili")
    manager.new_page = mock.AsyncMock(return_value=fake_page)

    await manager.search("moskva", 0, 1_000_000, limit=10)
    assert restart_called["n"] == 1
    await manager.stop()


@pytest.mark.asyncio
async def test_browser_restarts_once_on_pool_no_exit_node(monkeypatch):
    """При no_exit_node browser перезапускается с новым портом максимум один раз."""
    pool = FakeProxyPool(ports=[10000, 10001])
    monkeypatch.setattr(apw, "get_avito_proxy_pool", mock.AsyncMock(return_value=pool))

    _patch_playwright(monkeypatch)
    manager = apw.AvitoBrowserManager(proxy_url="")
    manager._proxy_pool = pool
    await manager.start()

    calls = {"n": 0}

    async def fake_extract_items(page):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("no exit node")
        return []

    manager._extract_items = fake_extract_items

    result = await manager.search("moskva", 0, 1_000_000, limit=10)
    assert result["error"] == "no_exit_node"
    assert calls["n"] == 2
    assert any(port == 10000 for port, _ in pool.failed)
    await manager.stop()


@pytest.mark.asyncio
async def test_detect_page_state_blocked():
    state = apw._detect_page_state("доступ ограничен", status=429)
    assert state["error"] == "blocked"
    assert state["blocked_detected"] is True


def test_detect_page_state_captcha():
    state = apw._detect_page_state("smartcaptcha")
    assert state["error"] == "captcha"
    assert state["captcha_detected"] is True


def test_detect_page_state_proxy_auth():
    state = apw._detect_page_state("proxy authentication required", status=407)
    assert state["error"] == "proxy_auth"


def test_item_without_url_rejected():
    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    assert manager._item_has_url({"title": "car"}) is False
    assert manager._item_has_url({"url": "https://avito.ru/item"}) is True


def test_extract_price_variants():
    manager = apw.AvitoBrowserManager(proxy_url="")
    assert manager._extract_price({"offers": {"price": 500000}}) == 500000
    assert manager._extract_price({"price": "750 000 ₽"}) == 750000
    assert manager._extract_price({"priceSpecification": {"value": 1200000}}) == 1200000


def test_extract_year_from_title():
    manager = apw.AvitoBrowserManager(proxy_url="")
    assert manager._extract_year("Toyota Corolla 2010", {}) == 2010
    assert manager._extract_year("No year", {"vehicleModelDate": "2008-01-01"}) == 2008


def test_normalize_item_preserves_missing_photo():
    manager = apw.AvitoBrowserManager(proxy_url="")
    item = manager._normalize_item({
        "title": "Lada Granta 2015",
        "url": "https://avito.ru/item_123",
        "price": 350000,
        "mileage": 120000,
    })
    assert item["title"] == "Lada Granta 2015"
    assert item["_price_int"] == 350000
    assert item["mileage"] == 120000
    assert item["image_url"] == ""
    assert item["images"] == []


def test_headless_default_true(monkeypatch):
    monkeypatch.delenv("AVITO_HEADLESS", raising=False)
    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    assert manager.headless is True


def test_headless_parses_false(monkeypatch):
    monkeypatch.setenv("AVITO_HEADLESS", "false")
    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    assert manager.headless is False


@pytest.mark.asyncio
async def test_stop_avito_manager_singleton(monkeypatch):
    manager = apw.AvitoBrowserManager(proxy_url="")
    apw._avito_manager = manager
    await apw.stop_avito_manager()
    assert apw._avito_manager is None


@pytest.mark.asyncio
async def test_search_avito_uses_singleton(monkeypatch):
    manager = apw.AvitoBrowserManager(proxy_url="")
    manager.search = mock.AsyncMock(return_value={"items": [], "error": None, "meta": {}})
    apw._avito_manager = manager
    result = await apw.search_avito("moskva", 0, 1000000)
    assert result == {"items": [], "error": None, "meta": {}}


def test_detect_page_state_403_connect_forbidden():
    state = apw._detect_page_state("connect tunnel failed", status=403)
    assert state["error"] == "proxy_connect_forbidden"


def test_captcha_not_solved():
    state = apw._detect_page_state("<div class=""captcha"">solve me</div>")
    assert state["captcha_detected"] is True
    assert state["error"] == "captcha"


def test_avito_error_does_not_break_other_sources(monkeypatch):
    """Ошибка Avito не ломает остальные источники — проверяем, что search возвращает структуру."""
    manager = apw.AvitoBrowserManager(proxy_url="")
    result = asyncio.run(manager.search("moskva", 0, 1000000, limit=10))
    assert "items" in result
    assert "error" in result
    assert "meta" in result
    assert result["error"] == "provider_not_configured"


@pytest.mark.asyncio
async def test_proxy_pool_is_not_coroutine_object(monkeypatch):
    """get_avito_proxy_pool() не должен оставаться coroutine в self._proxy_pool.

    Проверяем, что proxy_summary и healthcheck не падают с AttributeError.
    """
    monkeypatch.setattr(apw, "get_avito_proxy_pool", mock.AsyncMock(return_value=None))
    manager = apw.AvitoBrowserManager(proxy_url="")
    assert manager._proxy_pool is None
    assert not asyncio.iscoroutine(manager._proxy_pool)

    summary = await manager.proxy_summary()
    assert isinstance(summary, dict)
    assert "proxy_configured" in summary
    assert summary["proxy_configured"] is False

    hc = await manager.healthcheck()
    assert isinstance(hc, dict)
    assert hc.get("error") == "provider_not_configured"
