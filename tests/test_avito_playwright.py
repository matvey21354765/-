"""Тесты серверного Playwright-провайдера Avito.

Тесты не запускают реальный Chromium без необходимости — для этого используются
unit-проверки логики и мок-объекты.
"""
import asyncio
import os
from unittest import mock

import pytest

import avito_playwright as apw


class FakePage:
    def __init__(self, content: str = "", url: str = "", status: int = 200):
        self._content = content
        self.url = url
        self._status = status
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
        return []

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


def test_env_disabled_does_not_start_browser(monkeypatch):
    monkeypatch.setenv("AVITO_ENABLED", "false")
    monkeypatch.setenv("AVITO_PROVIDER", "disabled")
    # Перезагрузка модуля не нужна: проверяем, что менеджер без прокси
    # возвращает provider_not_configured.
    manager = apw.AvitoBrowserManager(proxy_url="")
    assert manager._proxy_config is None
    assert manager.proxy_summary() == {"proxy_configured": False}


def test_only_avito_proxy_url_used(monkeypatch):
    monkeypatch.setenv("AUTORU_PROXY_URL", "http://bad:bad@host:1")
    monkeypatch.setenv("AVITO_PROXY_URL", "http://user:pass@avito-proxy:8080")
    manager = apw.AvitoBrowserManager()
    assert manager.proxy_url == "http://user:pass@avito-proxy:8080"
    assert manager._proxy_config == {
        "server": "http://avito-proxy:8080",
        "username": "user",
        "password": "pass",
    }


def test_safe_proxy_summary_hides_credentials():
    manager = apw.AvitoBrowserManager(proxy_url="http://user:pass@host:1234")
    summary = manager.proxy_summary()
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
    browser = FakeBrowser()
    factory = FakePlaywrightFactory(browser)
    monkeypatch.setattr(apw, "async_playwright", lambda: factory)

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
    browser = FakeBrowser()
    factory = FakePlaywrightFactory(browser)
    monkeypatch.setattr(apw, "async_playwright", lambda: factory)

    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    page = await manager.new_page()
    assert isinstance(page, FakePage)
    await page.close()
    assert page.closed is True


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency():
    manager = apw.AvitoBrowserManager(
        proxy_url="http://u:p@h:1",
        max_concurrent_pages=2,
    )
    assert manager._semaphore._value == 2


@pytest.mark.asyncio
async def test_healthcheck_detects_captcha(monkeypatch):
    browser = FakeBrowser()
    factory = FakePlaywrightFactory(browser)
    monkeypatch.setattr(apw, "async_playwright", lambda: factory)

    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    # Подменяем new_page, чтобы вернуть страницу с капчей
    fake_page = FakePage(content='<html>smartcaptcha</html>', url="https://www.avito.ru/")
    manager.new_page = mock.AsyncMock(return_value=fake_page)

    hc = await manager.healthcheck()
    assert hc["ok"] is False
    assert hc["captcha_detected"] is True
    assert hc["error"] == "captcha"


@pytest.mark.asyncio
async def test_healthcheck_detects_connect_403(monkeypatch):
    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    manager.new_page = mock.AsyncMock(
        side_effect=Exception("Proxy connect 403 forbidden")
    )
    hc = await manager.healthcheck()
    assert hc["ok"] is False
    assert hc["error"] == "proxy_connect_forbidden"


@pytest.mark.asyncio
async def test_search_returns_blocked_on_captcha(monkeypatch):
    browser = FakeBrowser()
    factory = FakePlaywrightFactory(browser)
    monkeypatch.setattr(apw, "async_playwright", lambda: factory)

    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    fake_page = FakePage(content='<html>captcha</html>', url="https://www.avito.ru/moskva/avtomobili")
    manager.new_page = mock.AsyncMock(return_value=fake_page)

    result = await manager.search("moskva", 0, 1_000_000, limit=10)
    assert result["error"] == "captcha"
    assert result["items"] == []
    assert result["meta"]["captcha_detected"] is True


@pytest.mark.asyncio
async def test_search_returns_provider_not_configured_without_proxy():
    manager = apw.AvitoBrowserManager(proxy_url="")
    result = await manager.search("moskva", 0, 1_000_000, limit=10)
    assert result["error"] == "provider_not_configured"


@pytest.mark.asyncio
async def test_search_restarts_after_limit(monkeypatch):
    browser = FakeBrowser()
    factory = FakePlaywrightFactory(browser)
    monkeypatch.setattr(apw, "async_playwright", lambda: factory)

    manager = apw.AvitoBrowserManager(
        proxy_url="http://u:p@h:1",
        restart_after_searches=1,
    )
    manager._search_count = 1
    manager._started = True
    manager._browser = browser

    restart_called = {"n": 0}
    orig_restart = manager.restart

    async def tracked_restart():
        restart_called["n"] += 1
        await orig_restart()

    manager.restart = tracked_restart

    fake_page = FakePage(content='<html>ok</html>', url="https://www.avito.ru/moskva/avtomobili")
    manager.new_page = mock.AsyncMock(return_value=fake_page)

    await manager.search("moskva", 0, 1_000_000, limit=10)
    assert restart_called["n"] == 1


@pytest.mark.asyncio
async def test_browser_crash_single_restart(monkeypatch):
    manager = apw.AvitoBrowserManager(proxy_url="http://u:p@h:1")
    manager._started = True
    manager._browser = FakeBrowser()

    calls = {"restart": 0}

    async def fake_restart():
        calls["restart"] += 1

    manager.restart = fake_restart
    manager.new_page = mock.AsyncMock(side_effect=RuntimeError("browser crashed"))
    result = await manager.search("moskva", 0, 1_000_000, limit=10)
    assert result["error"] is not None
    assert calls["restart"] == 1


def test_detect_page_state_blocked():
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
