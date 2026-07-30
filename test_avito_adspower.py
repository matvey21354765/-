from __future__ import annotations

import asyncio
import inspect
import sys
from types import SimpleNamespace

import pytest
from aiohttp import web

from avito_adspower.adspower.client import ProfileConnection
from avito_adspower.adspower.profile_manager import ProfileManager
from avito_adspower.application.profile_lock import ProfileLock
from avito_adspower.application.retry import RetryPolicy
from avito_adspower.browser.connector import BrowserConnector
from avito_adspower.browser.session import captcha_detected
from avito_adspower.config import WorkerConfig
from avito_adspower.exceptions import (
    AvitoCaptchaError,
    ProfileBusyError,
)
from avito_adspower.listing.normalizer import (
    extract_source_id,
    normalize_photo_url,
    parse_int,
)
from avito_adspower.models import SearchRequest
from avito_adspower.redaction import redact
from avito_adspower.search.extractor import canonicalize_url
from avito_adspower.search.url_builder import SearchUrlBuilder
from avito_worker import auth_middleware
from avito_worker_provider import AvitoWorkerProvider, is_under_order
import control_bot as cb


def _config(**values):
    return WorkerConfig(
        adspower_profile_id="profile-1",
        worker_token="worker-secret",
        **values,
    )


def test_search_url_is_public_and_filterable():
    url = SearchUrlBuilder().build(
        SearchRequest(
            city="krasnodar",
            brand="lada",
            price_max=1_000_000,
            seller_type="private",
            sort="date",
        )
    )
    assert url.startswith("https://www.avito.ru/krasnodar/avtomobili/lada")
    assert "pmax=1000000" in url
    assert "user=1" in url
    assert "s=104" in url


def test_source_id_comes_from_real_url_and_fallback_is_stable():
    url = "https://www.avito.ru/krasnodar/avtomobili/lada_2114_123456789"
    assert extract_source_id(url) == "123456789"
    assert extract_source_id("https://www.avito.ru/x") == extract_source_id(
        "https://www.avito.ru/x"
    )


def test_url_and_value_normalization():
    assert canonicalize_url("/krasnodar/avtomobili/x_123456789?context=H4s") == (
        "https://www.avito.ru/krasnodar/avtomobili/x_123456789"
    )
    assert parse_int("1 250 000 ₽") == 1_250_000
    assert normalize_photo_url("//img.avito.st/image/1") == (
        "https://img.avito.st/image/1"
    )


@pytest.mark.parametrize(
    ("url", "text"),
    [
        ("https://www.avito.ru/showcaptcha", ""),
        ("https://www.avito.ru/", "Проверка, что вы не робот"),
        ("https://www.avito.ru/", "SmartCaptcha"),
        ("https://www.avito.ru/", "Доступ ограничен"),
    ],
)
def test_captcha_detection(url, text):
    assert captcha_detected(url, text)


def test_captcha_is_not_retried():
    calls = 0

    async def run():
        nonlocal calls

        async def operation():
            nonlocal calls
            calls += 1
            raise AvitoCaptchaError("captcha")

        with pytest.raises(AvitoCaptchaError):
            await RetryPolicy(3).run(operation)

    asyncio.run(run())
    assert calls == 1


def test_profile_lock_rejects_parallel_use():
    async def run():
        async with ProfileLock("same-profile"):
            with pytest.raises(ProfileBusyError):
                async with ProfileLock("same-profile"):
                    pass

    asyncio.run(run())


def test_profile_manager_does_not_stop_preexisting_profile():
    class Client:
        stopped = False

        async def status(self):
            return ProfileConnection("profile-1", True, "ws://safe")

        async def start(self):
            raise AssertionError("must not start")

        async def stop(self):
            self.stopped = True

    async def run():
        client = Client()
        manager = ProfileManager(client, _config(stop_profile_on_exit=True))
        managed = await manager.acquire()
        assert not managed.started_by_worker
        await manager.release()
        assert not client.stopped

    asyncio.run(run())


def test_profile_started_by_worker_stops_only_when_configured():
    class Client:
        stopped = False

        async def status(self):
            return ProfileConnection("profile-1", False, None)

        async def start(self):
            return ProfileConnection("profile-1", True, "ws://safe")

        async def stop(self):
            self.stopped = True

    async def run():
        client = Client()
        manager = ProfileManager(client, _config(stop_profile_on_exit=True))
        assert (await manager.acquire()).started_by_worker
        await manager.release()
        assert client.stopped

    asyncio.run(run())


def test_connector_uses_existing_context_and_does_not_create_one(monkeypatch):
    class Page:
        pass

    page = Page()
    context = SimpleNamespace(pages=[page])
    browser = SimpleNamespace(contexts=[context])

    class Chromium:
        async def connect_over_cdp(self, endpoint):
            assert endpoint == "ws://safe"
            return browser

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            pass

    class Starter:
        async def start(self):
            return Playwright()

    fake = SimpleNamespace(async_playwright=lambda: Starter())
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake)

    async def run():
        connected = await BrowserConnector(_config()).connect("ws://safe")
        assert connected.context is context
        assert connected.page is page
        assert not connected.page_created
        await connected.close()

    asyncio.run(run())


def test_redaction_hides_worker_token_and_cdp():
    value = redact({
        "authorization": "Bearer secret",
        "cdp_endpoint": "ws://127.0.0.1/devtools/browser/secret",
    })
    assert value == {"authorization": "***", "cdp_endpoint": "***"}


def test_worker_provider_maps_real_listing_and_order_classifier():
    item = {
        "source_id": "42",
        "url": "https://www.avito.ru/x_123456789",
        "title": "Авто из Японии под заказ",
        "price": 900000,
        "photo_urls": ["https://img.avito.st/1"],
        "attributes": {"Коробка": "AT"},
    }
    normalized = AvitoWorkerProvider._normalize(item)
    assert normalized["source_id"] == "42"
    assert normalized["_price_int"] == 900000
    assert normalized["_photo_url"].startswith("https://")
    assert normalized["_under_order"]
    assert is_under_order(item)


def test_worker_provider_cache_prevents_duplicate_requests(monkeypatch):
    provider = AvitoWorkerProvider(
        worker_url="https://worker.invalid",
        worker_token="token",
    )
    calls = 0

    def request(_payload):
        nonlocal calls
        calls += 1
        return ([{
            "source": "avito",
            "source_id": "1",
            "url": "https://www.avito.ru/x_123456789",
        }], 200)

    monkeypatch.setattr(provider, "_request", request)
    first = provider.search(city="krasnodar", price_max=1_000_000)
    second = provider.search(city="krasnodar", price_max=1_000_000)
    assert calls == 1
    assert first.items == second.items
    assert second.diagnostics["cache_hit"]


def test_worker_auth_requires_bearer_token():
    request = SimpleNamespace(
        path="/v1/avito/search",
        app={"config": _config(), "rate_state": {}},
        headers={},
        remote="127.0.0.1",
    )

    async def handler(_):
        return web.Response()

    with pytest.raises(web.HTTPUnauthorized):
        asyncio.run(auth_middleware(request, handler))


def test_worker_provider_branch_is_in_scheduler_not_instance_lock():
    scheduler_source = inspect.getsource(cb._avito_scheduled_fetch_unlocked)
    lock_source = inspect.getsource(cb._acquire_single_instance_lock)
    assert 'AVITO_PROVIDER == "adspower_worker"' in scheduler_source
    assert "AvitoWorkerProvider().search" in scheduler_source
    assert "AvitoWorkerProvider" not in lock_source
