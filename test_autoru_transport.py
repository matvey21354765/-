from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from autoru_transport import (
    autoru_captcha_detected,
    autoru_items_from_result,
    await_autoru_result,
    autoru_proxies,
    get_autoru_transport,
    normalize_autoru_result,
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


def test_result_contract_accepts_listing_list():
    items = [{"id": "1"}, {"id": "2"}]
    assert autoru_items_from_result(items) == items


def test_result_contract_accepts_items_mapping():
    items = [{"id": "1"}, {"id": "2"}]
    assert autoru_items_from_result({"items": items}) == items


def test_strict_contract_contains_items_error_and_meta():
    result = normalize_autoru_result([{"id": "1"}])
    assert result == {
        "items": [{"id": "1"}],
        "error": None,
        "meta": {},
    }


def test_result_contract_accepts_results_mapping():
    items = [{"id": "1"}]
    assert normalize_autoru_result({"results": items})["items"] == items


def test_result_contract_accepts_legacy_tuple():
    items = [{"id": "1"}]
    assert normalize_autoru_result((items, {"legacy": True}))["items"] == items


def test_none_is_a_classified_error():
    result = normalize_autoru_result(None)
    assert result["items"] == []
    assert result["error"] == "no_result"


def test_coroutine_is_awaited_before_normalization():
    async def legacy_search():
        return [{"id": "1"}]

    result = asyncio.run(await_autoru_result(legacy_search()))
    assert result["items"] == [{"id": "1"}]
    assert result["error"] is None


def test_result_contract_accepts_empty_list():
    assert autoru_items_from_result([]) == []


def test_result_contract_treats_none_as_empty():
    assert autoru_items_from_result(None) == []


def test_result_contract_rejects_unexpected_type():
    assert autoru_items_from_result("not-json") == []


def test_price_with_currency_is_parsed():
    assert cb.parse_price("100 000 ₽") == 100000


def test_price_above_max_is_filtered():
    item = {"price": "100 001 ₽"}
    assert not cb.in_price_range(item, 0, 100000)


def test_price_inside_budget_is_kept():
    item = {"price": "100 000 ₽"}
    assert cb.in_price_range(item, 0, 100000)


def test_price_99999_is_kept():
    item = {"price": "99 999 ₽"}
    assert cb.in_price_range(item, 0, 100000)


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.last_url = ""
        self.last_kwargs = {}
        self.cookies = SimpleNamespace(set=lambda *args, **kwargs: None)

    def get(self, url, **kwargs):
        self.calls += 1
        self.last_url = url
        self.last_kwargs = kwargs
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
    result = cb.scrape_autoru("krasnodar")
    assert result["items"] == []
    assert result["error"] == "restriction_captcha"
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
    result = cb.scrape_autoru("krasnodar", price_max=100000)
    assert result["items"] == [listing]
    assert result["error"] is None
    assert fake.calls == 1
    assert cb._AUTORU_LAST_DIAG["error_type"] == ""


def test_krasnodar_uses_region_slug_without_second_text_filter(monkeypatch):
    fake = _FakeSession(
        _response(
            url="https://auto.ru/krasnodarskiy_kray/cars/used/",
            html="<html>normal listing page</html>",
        )
    )
    listing = {
        "source": "autoru",
        "source_id": "43",
        "title": "Автомобиль из Краснодарского края",
        "url": "https://auto.ru/cars/used/sale/43/",
        "_price_int": 99999,
    }
    monkeypatch.setattr(
        "curl_cffi.requests.Session",
        lambda **kwargs: fake,
    )
    monkeypatch.setattr(cb, "_autoru_parse_html", lambda *args: [listing])

    result = cb.scrape_autoru("krasnodar", price_min=0, price_max=100000)

    assert "/krasnodarskiy_kray/" in fake.last_url
    assert result["items"] == [listing]


def test_http_shell_uses_one_ajax_fallback(monkeypatch):
    html_response = _response(
        url="https://auto.ru/omskaya_oblast/cars/used/",
        html="<html>client shell</html>",
    )
    ajax_response = SimpleNamespace(
        status_code=200,
        url="https://auto.ru/-/ajax/desktop/listing/",
        text='{"offers": []}',
        content=b'{"offers": []}',
        headers={"content-type": "application/json"},
        json=lambda: {"offers": [{"id": "55"}]},
    )

    class AjaxSession(_FakeSession):
        def __init__(self):
            super().__init__(html_response)
            self.post_calls = 0

        def post(self, url, **kwargs):
            self.post_calls += 1
            self.post_kwargs = kwargs
            return ajax_response

    fake = AjaxSession()
    listing = {
        "source": "autoru", "source_id": "55",
        "url": "https://auto.ru/cars/used/sale/55/", "_price_int": 90000,
    }
    monkeypatch.setattr("curl_cffi.requests.Session", lambda **kwargs: fake)
    monkeypatch.setattr(cb, "_autoru_parse_html", lambda *args: [])
    monkeypatch.setattr(cb, "_autoru_parse_offers", lambda *args: [listing])
    result = cb.scrape_autoru("omsk", price_max=100000)
    assert result["items"] == [listing]
    assert result["error"] is None
    assert fake.calls == 1
    assert fake.post_calls == 1
    assert fake.post_kwargs["json"]["geo_id"] == cb.AUTORU_GEO_IDS["omsk"]


def test_http_200_without_offers_is_parse_error(monkeypatch):
    class EmptySession(_FakeSession):
        def post(self, url, **kwargs):
            return SimpleNamespace(
                status_code=200, url=url, text="{}", content=b"{}",
                headers={"content-type": "application/json"}, json=lambda: {},
            )

    fake = EmptySession(_response(
        url="https://auto.ru/omskaya_oblast/cars/used/",
        html="<html>client shell</html>",
    ))
    monkeypatch.setattr("curl_cffi.requests.Session", lambda **kwargs: fake)
    monkeypatch.setattr(cb, "_autoru_parse_html", lambda *args: [])
    monkeypatch.setattr(cb, "_autoru_parse_offers", lambda *args: [])
    result = cb.scrape_autoru("omsk", price_max=100000)
    assert result["items"] == []
    assert result["error"] == "parse_error"


def test_current_ssr_listing_card_is_parsed_without_inline_json():
    import datetime
    import control_bot as cb

    html = '''
    <div data-seo="listing-item">
      <a href="https://auto.ru/cars/used/sale/lada/granta/1234567890-abcd/">
        <img src="//avatars.avto.ru/car.jpg" />
      </a>
      <div class="ListingItemTitle">
        <a class="ListingItemTitle__link" href="https://auto.ru/cars/used/sale/lada/granta/1234567890-abcd/">
          Lada Granta<div class="ListingItemTitle__clicker"></div>
        </a>
      </div>
      <div class="ListingItemUniversalPrice__highlighted-abc">95 000 ₽</div>
      <div>2012</div>
    </div>
    '''
    items = cb._autoru_parse_html(html, datetime.date(2026, 8, 3))
    assert len(items) == 1
    assert items[0]["_price_int"] == 95000
    assert items[0]["title"] == "Lada Granta"
    assert items[0]["_photo_url"] == "https://avatars.avto.ru/car.jpg"


# ── Фото и дата публикации Auto.ru ───────────────────────────────────


def test_yandex_share_stub_is_not_accepted_as_car_photo():
    """og:image страницы без фото — логотип «Я», а не машина."""
    assert not cb._autoru_photo_ok("https://yastatic.net/s3/home/logo.png")
    assert not cb._autoru_photo_ok(
        "https://avatars.mds.yandex.net/get-verba/share/og-image.png")
    assert not cb._autoru_photo_ok("https://auto.ru/apple-touch-icon.png")
    assert cb._autoru_photo_ok(
        "https://avatars.mds.yandex.net/get-autoru-vos/1/2/1200x900")


def test_photo_read_from_every_autoru_shape():
    from_sizes = cb._autoru_photo_from_list(
        [{"sizes": {"456x342": "//avatars.mds.yandex.net/get-autoru-vos/a/b/456x342"}}])
    assert from_sizes.startswith("https://avatars.mds.yandex.net/")
    assert cb._autoru_photo_from_list(
        ["//avatars.mds.yandex.net/get-autoru-all/x/y/1200x900"])
    assert cb._autoru_photo_from_list(
        [{"url": "https://avatars.mds.yandex.net/get-autoru-vos/x/y/full"}])
    # Заглушка вместо фото — пусто, чтобы карточка ушла без картинки.
    assert cb._autoru_photo_from_list([{"sizes": {"456x342": "//yastatic.net/logo.png"}}]) == ""
    assert cb._autoru_photo_from_list(None) == ""


def test_photo_recovered_from_page_json_when_og_image_is_a_stub():
    page = 'x window.__INITIAL_STATE__={"card":{"photos":[{"sizes":{"1200x900":' \
           '"//avatars.mds.yandex.net/get-autoru-vos/q/w/1200x900"}}]}}</script>'
    assert cb._autoru_photo_from_page(page) == (
        "https://avatars.mds.yandex.net/get-autoru-vos/q/w/1200x900")
    # Регексный запасной путь, если __INITIAL_STATE__ не разобрался.
    raw = 'noise "832x624":"//avatars.mds.yandex.net/get-autoru-all/e/r/832x624" noise'
    assert cb._autoru_photo_from_page(raw).endswith("832x624")
    assert cb._autoru_photo_from_page("<html>no photos here</html>") == ""


def test_offer_keeps_exact_publish_moment_and_real_photo():
    import datetime

    offers = {"offers": [{
        "vehicle_info": {"mark_info": {"name": "Opel"},
                         "model_info": {"name": "Omega"}},
        "documents": {"year": 1984},
        "price_info": {"price": 77300},
        "url": "https://auto.ru/cars/used/sale/1",
        "photos": [{"sizes": {
            "1200x900": "//avatars.mds.yandex.net/get-autoru-vos/a/b/1200x900"}}],
        "additional_info": {"creation_date": "2026-08-04T13:05:00Z"},
    }]}
    item = cb._autoru_parse_offers(offers, datetime.date(2026, 8, 5))[0]
    assert item["_photo_url"].startswith("https://avatars.mds.yandex.net/")
    assert item["_published_ts"]
    # Время публикации известно точно — карточка покажет дату, а не «N дн назад».
    assert cb._ps.published_at(item) == item["_published_ts"]


# ── Разбор карточек выдачи Auto.ru ───────────────────────────────────


def _sample_cards():
    import datetime
    with open("autoru.html", encoding="utf-8", errors="replace") as fh:
        return cb._autoru_parse_html(fh.read(), datetime.date.today())


def test_every_card_of_the_page_is_parsed():
    """Цена размечена по-разному, и по одному классу терялось 34 карточки из 37."""
    from bs4 import BeautifulSoup
    with open("autoru.html", encoding="utf-8", errors="replace") as fh:
        html = fh.read()
    on_page = len(BeautifulSoup(html, "html.parser").select('[data-seo="listing-item"]'))
    assert len(_sample_cards()) == on_page


def test_cards_carry_description_and_price():
    cards = _sample_cards()
    assert all(c["description"] for c in cards)
    assert all(c["_price_int"] >= 10_000 for c in cards)


def test_mileage_is_not_glued_to_the_year():
    """«2025 6 900 км» — жадный шаблон давал пробег 20 256 900."""
    assert cb._autoru_mileage_from_text("Автомат 2025 6 900 км") == 6900
    assert cb._autoru_mileage_from_text("Lada 2109 272 137 км") == 272137
    assert cb._autoru_mileage_from_text("Новый автомобиль") == 0


def test_current_photo_cdn_is_accepted():
    """Auto.ru отдаёт фото с avatars.avto.ru — не только с mds.yandex.net."""
    assert cb._autoru_photo_ok(
        "https://avatars.avto.ru/get-autoru-vos/18031053/abc/456x342")
    assert cb._autoru_photo_ok(
        "https://avatars.mds.yandex.net/get-autoru-vos/1/2/1200x900")
    assert not cb._autoru_photo_ok("https://yastatic.net/s3/logo.png")


# ── Дата публикации со страницы объявления ───────────────────────────


def _msk(ts):
    """Даты площадок — московские, сравнивать их надо в МСК."""
    import datetime
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone(datetime.timedelta(hours=3)))


def test_publish_date_read_from_page_markup():
    page = ('<div class="CardHead__creationDate">27 июня 2025</div>'
            '<div>306 (3 сегодня)</div>')
    ts = cb._published_ts_from_page(page, "autoru")
    assert ts
    assert _msk(ts).strftime("%d.%m.%Y") == "27.06.2025"


def test_publish_date_prefers_machine_readable_markup():
    page = '<meta property="article:published_time" content="2025-07-02T18:30:00+03:00">'
    ts = cb._published_ts_from_page(page, "drom")
    assert _msk(ts).strftime("%d.%m.%Y") == "02.07.2025"


def test_publish_date_from_platform_json_keys():
    assert cb._published_ts_from_page('{"sortTimeStamp":1751000000000}', "avito")
    assert cb._published_ts_from_page('{"date_published":1751000000}', "youla")
    assert cb._published_ts_from_page("<html>ничего</html>", "autoru") is None


# ── Фото не должно быть превью или огрызком ссылки ───────────────────


def test_escaped_json_photo_url_is_not_truncated():
    """В JSON страницы слэши экранированы; обрыв ссылки давал битое фото."""
    page = (r'{"small":"\/\/avatars.avto.ru\/get-autoru-vos\/476\/abc\/small",'
            r'"1200x900":"\/\/avatars.avto.ru\/get-autoru-vos\/476\/abc\/1200x900"}')
    assert cb._autoru_photo_from_page(page) == (
        "https://avatars.avto.ru/get-autoru-vos/476/abc/1200x900")


def test_preview_sizes_are_rejected():
    """Рядом с фото Auto.ru лежит размытое превью — в карточку идёт снимок."""
    for thumb in ("small", "thumb_m", "preview", "92x69", "120x90"):
        assert not cb._autoru_photo_ok(
            f"https://avatars.avto.ru/get-autoru-vos/1/2/{thumb}"), thumb
    assert cb._autoru_photo_ok("https://avatars.avto.ru/get-autoru-vos/1/2/1200x900")


def test_publish_moment_comes_from_the_offer_json():
    """Дата берётся из объекта самого объявления, а не по позиции в тексте."""
    ts = cb._autoru_published_ts({"additional_info": {"creation_date": "1759264644446"}})
    assert ts and _msk(ts).strftime("%d.%m.%Y") == "30.09.2025"
    assert cb._autoru_published_ts({}) is None
    assert cb._autoru_published_ts({"created": "2026-07-02T10:00:00Z"})


# ── Авито: добор настоящим поиском, когда лента пуста по городу ──────


def test_region_hits_counted_by_city_name():
    items = [{"location": "Пермский край, Пермь", "city": "Пермь"},
             {"location": "Москва", "city": "Москва"}]
    assert cb._avito_region_hits(items, "perm") == 1
    assert cb._avito_region_hits(items, "moscow") == 1


def test_direct_search_skipped_while_rate_limited(monkeypatch):
    """Правило WORKING_CONFIG: под ограничением IP не жжём запросами."""
    monkeypatch.setattr(cb, "_avito_rate_limited", lambda: True)
    called = []
    monkeypatch.setattr(cb, "_avito_webjson_search",
                        lambda *a, **k: called.append(1) or [{"url": "x"}])
    assert cb._avito_direct_region_search(
        "perm", price_min=0, price_max=100, sort_by_date=True, brand="") == []
    assert not called


def test_direct_search_runs_when_feed_is_thin(monkeypatch):
    monkeypatch.setattr(cb, "_avito_rate_limited", lambda: False)
    monkeypatch.setattr(cb, "_spfa_user_search_active", lambda: False)
    monkeypatch.setattr(cb, "_avito_webjson_search",
                        lambda *a, **k: [{"url": "https://avito.ru/1"}])
    out = cb._avito_direct_region_search(
        "perm", price_min=0, price_max=100, sort_by_date=True, brand="any")
    assert out and out[0]["url"] == "https://avito.ru/1"


# ── Фото: проверка ссылки перед отправкой ────────────────────────────


def test_photo_url_checked_before_sending(monkeypatch):
    """Telegram не считает ошибкой недоступное фото — показывает крестик."""
    cb._PHOTO_CHECK_CACHE.clear()

    class _Resp:
        def __init__(self, status, ctype, length):
            self.status_code, self.headers = status, {
                "Content-Type": ctype, "Content-Length": str(length)}

        def close(self):
            pass

    import requests
    cases = {
        "https://ok/photo.jpg": _Resp(200, "image/jpeg", 40000),
        "https://gone/photo.jpg": _Resp(404, "text/html", 500),
        "https://stub/photo.jpg": _Resp(200, "image/gif", 120),
        "https://page/photo.jpg": _Resp(200, "text/html", 40000),
    }
    monkeypatch.setattr(requests, "get", lambda url, **kw: cases[url])
    assert cb._photo_is_loadable("https://ok/photo.jpg")
    assert not cb._photo_is_loadable("https://gone/photo.jpg")
    assert not cb._photo_is_loadable("https://stub/photo.jpg")
    assert not cb._photo_is_loadable("https://page/photo.jpg")
    assert not cb._photo_is_loadable("")


def test_photo_check_is_cached(monkeypatch):
    cb._PHOTO_CHECK_CACHE.clear()
    calls = []

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "image/jpeg", "Content-Length": "40000"}

        def close(self):
            pass

    import requests
    monkeypatch.setattr(requests, "get",
                        lambda url, **kw: calls.append(url) or _Resp())
    for _ in range(3):
        cb._photo_is_loadable("https://ok/photo.jpg")
    assert len(calls) == 1


def test_youla_picks_the_largest_photo():
    """Первый кадр в images часто превью на 90 пикселей."""
    import inspect
    src = inspect.getsource(cb.scrape_youla)
    assert "_youla_best_photo(imgs)" in src


# ── Авито: точный момент публикации ──────────────────────────────────


def test_avito_listing_keeps_the_exact_publish_moment():
    """sortTimeStamp разбирался только в сутки: у сегодняшних объявлений
    выходило «площадка не указала дату»."""
    import perekup_search as ps
    now = __import__("time").time()
    item = {"_published_ts": ps.parse_published_ts(now - 900)}
    assert ps.published_at(item)
    assert abs(ps.published_at(item) - (now - 900)) < 5
