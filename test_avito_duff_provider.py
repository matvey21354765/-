from __future__ import annotations

import html
import json
import threading
import time
from pathlib import Path

import pytest

import avito_duff_provider as provider_module
from avito_duff_provider import (
    AvitoBlockedError,
    AvitoDuffProvider,
    AvitoParseError,
    AvitoRedirectLoopError,
    AvitoVpnUnavailable,
    normalize_item,
)


ROOT = Path(__file__).resolve().parent
INITIAL_HTML = (ROOT / "data" / "vless_avito_response.html").read_text(
    encoding="utf-8", errors="replace"
)
CANONICAL_HTML = (
    ROOT / "data" / "vless_avito_canonical_response.html"
).read_text(encoding="utf-8", errors="replace")


class FakeResponse:
    def __init__(self, status: int, text: str, url: str = "https://www.avito.ru/x"):
        self.status_code = status
        self.text = text
        self.url = url
        self.content = text.encode("utf-8")


class FakeSession:
    responses: list[FakeResponse] = []
    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        self.cookies = self

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.responses.pop(0)

    def clear(self):
        return None

    def close(self):
        return None


def state_html(data: object) -> str:
    payload = html.escape(json.dumps({"loaderData": {"data": data}}))
    return (
        "<html><head><title>Авито</title></head><body>"
        '<script type="mime/invalid" data-mfe-state="true">'
        f"{payload}</script></body></html>"
    )


@pytest.fixture(autouse=True)
def fake_session(monkeypatch):
    FakeSession.responses = []
    FakeSession.calls = []
    monkeypatch.setattr(provider_module.requests, "Session", FakeSession)


def test_saved_html_contains_catalog_items():
    FakeSession.responses = [FakeResponse(200, CANONICAL_HTML)]
    result = AvitoDuffProvider().search("https://www.avito.ru/x")
    assert result
    assert all(item["source"] == "avito" for item in result)


def test_saved_html_internal_redirect_then_catalog():
    FakeSession.responses = [
        FakeResponse(200, INITIAL_HTML),
        FakeResponse(200, CANONICAL_HTML),
    ]
    provider = AvitoDuffProvider()
    result = provider.search("https://www.avito.ru/chelyabinsk/avtomobili")
    assert result
    assert provider.last_diagnostics["requests"] == 2
    assert provider.last_diagnostics["internal_redirect"] is True


def test_external_redirect_is_rejected():
    FakeSession.responses = [
        FakeResponse(
            200,
            state_html({
                "status": {"code": 301},
                "redirected": True,
                "url": "https://example.com/search",
            }),
        )
    ]
    with pytest.raises(AvitoParseError, match="вне avito"):
        AvitoDuffProvider().search("https://www.avito.ru/x")


def test_second_internal_redirect_is_loop():
    redirect = state_html({
        "status": {"code": 301},
        "redirected": True,
        "url": "/chelyabinsk/avtomobili/canonical",
    })
    FakeSession.responses = [
        FakeResponse(200, redirect),
        FakeResponse(200, redirect),
    ]
    with pytest.raises(AvitoRedirectLoopError):
        AvitoDuffProvider().search("https://www.avito.ru/x")
    assert len(FakeSession.calls) == 2


def test_missing_mime_invalid():
    FakeSession.responses = [FakeResponse(200, "<html><title>Авито</title></html>")]
    with pytest.raises(AvitoParseError, match="mime/invalid"):
        AvitoDuffProvider().search("https://www.avito.ru/x")


def test_missing_loader_data():
    body = (
        '<script type="mime/invalid" data-mfe-state="true">'
        '{"loaderData":{}}</script>'
    )
    FakeSession.responses = [FakeResponse(200, body)]
    with pytest.raises(AvitoParseError, match="loaderData.data"):
        AvitoDuffProvider().search("https://www.avito.ru/x")


def test_missing_socks_raises_vpn_unavailable(monkeypatch):
    def fail_get(self, url, **kwargs):
        raise TimeoutError("SOCKS connect timeout")

    monkeypatch.setattr(FakeSession, "get", fail_get)
    with pytest.raises(AvitoVpnUnavailable):
        AvitoDuffProvider().search("https://www.avito.ru/x")


@pytest.mark.parametrize("status", [403, 429])
def test_blocked_http(status):
    FakeSession.responses = [FakeResponse(status, "<title>Доступ ограничен</title>")]
    with pytest.raises(AvitoBlockedError) as caught:
        AvitoDuffProvider().search("https://www.avito.ru/x")
    assert caught.value.status_code == status


def test_normalize_item():
    result = normalize_item({
        "id": 123,
        "title": "Тестовый автомобиль",
        "priceDetailed": {"value": 95000},
        "urlPath": "/city/avtomobili/test_123",
        "location": {"name": "Челябинск"},
        "images": [{"472x472": "https://img.avito.st/test.jpg"}],
        "sortTimeStamp": 1_785_313_191_000,
        "seller": {"name": "Иван"},
    })
    assert result == {
        "id": "123",
        "title": "Тестовый автомобиль",
        "price": 95000,
        "url": "https://www.avito.ru/city/avtomobili/test_123",
        "location": "Челябинск",
        "image": "https://img.avito.st/test.jpg",
        "published_at": "2026-07-29T08:19:51+00:00",
        "seller": "Иван",
        "source": "avito",
    }


def test_scheduler_does_not_run_provider_in_parallel(monkeypatch):
    import control_bot

    started = threading.Event()
    release = threading.Event()
    calls = []

    class SlowProvider:
        def __init__(self, **kwargs):
            self.last_diagnostics = {
                "http": 200,
                "requests": 1,
                "catalog_items": 0,
            }

        def search(self, **kwargs):
            calls.append(kwargs)
            started.set()
            release.wait(timeout=2)
            return []

    monkeypatch.setattr(control_bot, "RestAppAvitoProvider", SlowProvider)
    monkeypatch.setattr(control_bot, "AVITO_PROVIDER", "rest_app")
    monkeypatch.setattr(control_bot, "AVITO_ENABLED", True)
    control_bot._AVITO_SCHEDULE.clear()
    control_bot._AVITO_GLOBAL_NEXT_ATTEMPT_AT = 0
    key1 = control_bot._avito_schedule_key("chelyabinsk", 0, 100000, True, "")
    key2 = control_bot._avito_schedule_key("moskva", 0, 100000, True, "")
    worker = threading.Thread(
        target=control_bot._avito_scheduled_fetch, args=(key1, 1000)
    )
    worker.start()
    assert started.wait(timeout=1)
    control_bot._avito_scheduled_fetch(key2, 1000)
    release.set()
    worker.join(timeout=2)
    assert len(calls) == 1


def test_manual_search_registers_priority_and_reuses_cache(monkeypatch):
    import control_bot

    monkeypatch.setattr(control_bot, "AVITO_ENABLED", True)
    monkeypatch.setattr(control_bot, "_AVITO_SCHEDULER_RUNNING", False)
    control_bot._AVITO_SCHEDULE.clear()
    assert control_bot.scrape_avito("chelyabinsk", price_max=100000) == []
    key = control_bot._avito_schedule_key(
        "chelyabinsk", 0, 100000, False, ""
    )
    assert control_bot._AVITO_SCHEDULE[key]["priority"] == 1
    expected = [{"url": "https://www.avito.ru/item/1"}]
    control_bot._AVITO_SCHEDULE[key]["items"] = expected
    assert control_bot.scrape_avito(
        "chelyabinsk", price_max=100000
    ) == expected


def test_manual_search_waits_for_inflight_scheduler(monkeypatch):
    import control_bot

    monkeypatch.setattr(control_bot, "AVITO_ENABLED", True)
    monkeypatch.setattr(control_bot, "_AVITO_SCHEDULER_RUNNING", True)
    control_bot._AVITO_SCHEDULE.clear()
    control_bot._AVITO_GLOBAL_NEXT_ATTEMPT_AT = 0
    key = control_bot._avito_schedule_key(
        "chelyabinsk", 0, 100000, False, ""
    )
    entry = control_bot._avito_schedule_entry(key)
    entry["in_flight"] = True
    result = []

    worker = threading.Thread(
        target=lambda: result.extend(
            control_bot.scrape_avito("chelyabinsk", price_max=100000)
        )
    )
    worker.start()
    time.sleep(0.05)
    with control_bot._AVITO_SCHEDULE_LOCK:
        entry["items"] = [{"url": "https://www.avito.ru/item/2"}]
        entry["in_flight"] = False
        entry["ready_event"].set()
    worker.join(timeout=1)
    assert result == [{"url": "https://www.avito.ru/item/2"}]
