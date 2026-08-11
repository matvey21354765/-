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


# ── Авито: бюджет времени на обход страниц ───────────────────────────


def test_avito_budget_is_smaller_than_the_search_timeout():
    """Парсер обязан уложиться в ожидание поиска, иначе выдача обрывается
    и пользователь видит «Avito: 0» при разобранных объявлениях."""
    budget = cb._avito_budget_sec()
    assert budget >= 120
    assert max(90, budget + 40) > budget


def test_avito_budget_reads_env(monkeypatch):
    monkeypatch.setenv("AVITO_BUDGET_SEC", "90")
    assert cb._avito_budget_sec() == 90
    monkeypatch.setenv("AVITO_BUDGET_SEC", "мусор")
    assert cb._avito_budget_sec() == 120        # значение по умолчанию
    monkeypatch.setenv("AVITO_BUDGET_SEC", "1")
    assert cb._avito_budget_sec() == 10          # ниже минимума не опускаемся


def test_pagination_stops_on_budget():
    """Обход прерывается по бюджету и отдаёт уже собранное."""
    import inspect
    src = inspect.getsource(cb._avito_legacy_fetch)
    assert "_budget" in src and "break" in src


# ── Плановая рассылка ────────────────────────────────────────────────


def test_scheduled_broadcast_fires_once_at_its_time():
    now = 1_000_000.0
    rows = [{"id": "a", "send_at": now, "sent_at": None}]
    assert cb.due_broadcasts(rows, now - 1) == []          # рано
    assert len(cb.due_broadcasts(rows, now)) == 1          # пора
    assert len(cb.due_broadcasts(rows, now + 3600)) == 1   # бот лежал час
    rows[0]["sent_at"] = now
    assert cb.due_broadcasts(rows, now + 3600) == []       # уже отправлена


def test_missed_broadcast_is_not_sent_days_later():
    """Если бот лежал сутки, анонс не должен прийти среди ночи."""
    now = 1_000_000.0
    rows = [{"id": "a", "send_at": now, "sent_at": None}]
    assert cb.due_broadcasts(rows, now + 25 * 3600) == []


def test_no_broadcast_is_scheduled_by_default():
    """Плановых рассылок нет: анонс обновления снят с расписания."""
    import inspect
    src = inspect.getsource(cb._scheduled_broadcast_loop)
    assert "schedule_broadcast(" not in src
    assert "drop_cancelled_broadcasts" in src


def test_cancelled_broadcast_is_removed_from_the_schedule():
    """Даже если строка осталась в файле, отменённая рассылка не уйдёт."""
    rows = [{"id": "update-2026-08-06", "send_at": 1, "sent_at": None},
            {"id": "other", "send_at": 2, "sent_at": None}]
    left = cb.drop_cancelled_broadcasts(rows)
    assert [r["id"] for r in left] == ["other"]


# ── Дата публикации прямо из выдачи Auto.ru ──────────────────────────


def test_offer_dates_read_from_the_search_page():
    """Дата лежит в JSON каждого объявления на странице выдачи.

    Раньше карточки уходили без даты, и в шапке стояло «бот впервые увидел»
    вместо времени, когда машина реально появилась.
    """
    with open("autoru.html", encoding="utf-8", errors="replace") as fh:
        html = fh.read()
    dates = cb._autoru_offer_dates(html)
    assert len(dates) >= 30
    assert all(ts > 0 for ts in dates.values())


def test_dates_land_on_the_right_cards():
    import datetime
    with open("autoru.html", encoding="utf-8", errors="replace") as fh:
        html = fh.read()
    items = cb._autoru_parse_html(html, datetime.date.today())
    dated = [i for i in items if i.get("_published_ts")]
    assert len(dated) >= len(items) * 0.8
    for item in dated:
        # id из ссылки объявления должен совпасть с id, по которому взята дата.
        assert cb._autoru_url_id(item["url"])
        assert item["_days_on_site"] >= 0
        assert item.get("_date_known")


def test_json_object_around_finds_the_whole_offer():
    text = 'x{"counters":{"a":1},"hash":"abc123","id":"1234567890","price":5}y'
    pos = text.index('"hash"')
    obj = cb._json_object_around(text, pos)
    assert obj and obj["id"] == "1234567890" and obj["price"] == 5


def test_url_id_extracted_from_listing_link():
    assert cb._autoru_url_id(
        "https://auto.ru/cars/used/sale/vaz/2110/1132753426-5871a155/") == "1132753426"
    assert cb._autoru_url_id("https://auto.ru/cars/used/") == ""


# ── Почему Авито молчит ──────────────────────────────────────────────


def test_unavailable_reason_names_the_actual_problem():
    """«Временно недоступен» одинаково выглядел и при пустом прокси, и при капче."""
    f = cb.avito_unavailable_reason
    assert "AVITO_PROVIDER" in f(provider="disabled")
    assert "PROXY_URL" in f(provider="webjson", proxies=None)
    assert "пауза" in f(provider="webjson", proxies={"http": "x"},
                        rate_limited_until=1_000_000 + 300, now=1_000_000)
    assert "капч" in f(provider="webjson", proxies={"http": "x"},
                       diag={"reason": "captcha detected"})
    assert "403" in f(provider="webjson", proxies={"http": "x"}, diag={"http": 403})
    assert f(provider="webjson", proxies={"http": "x"}, diag={}) == ""


def test_proxy_problem_wins_over_platform_errors():
    """Сначала называем то, что чинится настройкой."""
    reason = cb.avito_unavailable_reason(
        provider="webjson", proxies=None, diag={"reason": "captcha"})
    assert "PROXY_URL" in reason


# ── Авито: успешный результат нельзя терять ──────────────────────────


def test_successful_parse_is_not_overwritten_by_later_attempt():
    """В логах было: «июльский парсер: 49 ✅», следом «webJSON итого 0» — и
    пользователь видел «Avito: 0». Следующие пути идут только при пустом
    результате."""
    import inspect
    src = inspect.getsource(cb._avito_scheduled_fetch_unlocked)
    start = src.index("Июльский оригинал")
    tail = src[start:start + 2500]
    idx = tail.index("_avito_webjson_search")
    # Перед повторным вызовом webJSON обязана стоять проверка пустоты.
    assert "if not parsed:" in tail[:idx]


def test_blocked_page_stops_pagination():
    """Правило рабочей версии: страница заблокирована — остальные не берём."""
    import inspect
    src = inspect.getsource(cb._avito_legacy_fetch)
    assert "_blocked_status" in src
    assert "дальше не идём" in src
    # При блокировке не делаем второй запрос тем же адресом.
    assert "if not text and not _blocked_status[0]:" in src


def test_relative_listing_url_still_yields_a_region():
    """Часть парсеров отдаёт ссылки без домена — регион всё равно нужен."""
    import perekup_search as ps
    assert ps._region_of_item({"url": "/omsk/avtomobili/vaz_2110_123"}) == "omsk"
    assert ps._region_of_item(
        {"url": "https://www.avito.ru/omsk/avtomobili/vaz_2110_123"}) == "omsk"


# ── Уведомления ──────────────────────────────────────────────────────


def test_instant_notifications_do_not_require_the_monitor_toggle():
    """«⚡ Мониторинг» выключен по умолчанию, и «Кто быстрее» молчал у всех."""
    import inspect
    src = inspect.getsource(cb._ps_new_listing_loop)
    # Упоминание в комментарии допустимо, проверки быть не должно.
    assert 'load_settings(uid).get("monitor_enabled")' not in src
    assert 'instant_notify' in src
    assert '_subscription_info(uid)["ended"]' in src


def test_notifications_do_not_depend_on_the_platform_date():
    """Раздел «Кто быстрее» требует точной даты, а её отдают не все площадки.

    Пока цикл брал именно раздел, свежие находки Drom/Auto.ru/Юлы уходили в
    «Новые сегодня» и уведомление не приходило никогда.
    """
    import inspect
    src = inspect.getsource(cb._ps_new_listing_loop)
    assert "_ps.notify_candidates(" in src
    assert '_ps.search_listings(\n' not in src
    # Дата со страницы проверяется уже после захода на неё.
    assert src.index("_ps_enrich_from_pages") < src.index("should_notify_now")


def test_every_notification_loop_is_started():
    """Цикл, который не запущен, не пришлёт ни одного уведомления."""
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(cb.__file__).with_suffix(".py").read_text(encoding="utf-8"))
    started = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_task" and node.args):
            arg = node.args[0]
            if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                started.add(arg.func.id)
    for name in ("_ps_new_listing_loop", "_ps_saved_events_loop", "_ps_reports_loop",
                 "_ps_comeback_loop", "_trial_notification_loop",
                 "_price_watch_loop", "_scheduled_broadcast_loop"):
        assert name in started, name


# ── Пауза после бана не должна означать пустую выдачу ────────────────


def test_rotated_ip_shortens_the_pause(monkeypatch):
    """Ограничение висит на прежнем адресе: со свежим ждать десять минут незачем."""
    monkeypatch.setattr(cb, "_AVITO_RATE_LIMIT_UNTIL", 0.0)
    monkeypatch.setattr(cb, "_rotate_proxy_ip", lambda **kw: True)
    cb._avito_note_rate_limit(600)
    left = cb._AVITO_RATE_LIMIT_UNTIL - cb.time.time()
    assert 0 < left <= 120, left


def test_without_rotation_the_pause_stays_long(monkeypatch):
    monkeypatch.setattr(cb, "_AVITO_RATE_LIMIT_UNTIL", 0.0)
    monkeypatch.setattr(cb, "_rotate_proxy_ip", lambda **kw: False)
    cb._avito_note_rate_limit(600)
    left = cb._AVITO_RATE_LIMIT_UNTIL - cb.time.time()
    assert left > 300, left


def test_pause_serves_the_last_successful_result():
    """В паузе отдаём последнюю успешную выдачу, а не пустоту."""
    import inspect
    src = inspect.getsource(cb._avito_cached_result)
    start = src.index("_paused = _avito_rate_limited()")
    window = src[start:start + 900]
    assert "allow_stale=True" in window
    assert "_AVITO_PRODUCTION_STATE.cached" in window


def test_broken_photo_is_replaced_in_sections():
    """Telegram на недоступной ссылке отвечает «failed to get HTTP URL».

    В разделах фото проверяется и при отказе подменяется другим со страницы
    объявления; в обычной выдаче запасного фото нет, поэтому там карточка
    просто уходит текстом — лишний запрос на каждую карточку не нужен.
    """
    import inspect
    src = inspect.getsource(cb._ps_usable_photo)
    assert "_photo_is_loadable" in src
    assert "_fetch_listing_details" in src


def test_start_screen_offers_recommendations():
    import inspect
    src = inspect.getsource(cb._ps_send_start_screen)
    assert "pd_recommend" in src


def test_avito_serves_disk_cache_after_restart():
    """После перезапуска память пуста, а на диске лежит последняя выдача.

    Раньше бот в этот момент шёл в сеть, и если контейнер успевали
    перезапустить снова, поиск так и показывал «Avito: 0».
    """
    import inspect
    src = inspect.getsource(cb._avito_cached_result)
    start = src.index('if AVITO_PROVIDER == "webjson":')
    head = src[start:]
    assert "_AVITO_PRODUCTION_STATE.cached" in head
    assert "выдача из сохранённой на диске" in head
    # Диск проверяется ДО обращения в сеть.
    assert head.index("выдача из сохранённой на диске") < head.index("_avito_july_scraper")


# ── Живой поиск идёт тем путём, который реально отдаёт объявления ────


def test_live_search_uses_the_july_scraper_first():
    """В боевых логах объявления отдаёт именно июльский парсер
    («brace-JSON (urlPath) извлёк 49», «июльский парсер: 49 ✅»), а webJSON
    на том же адресе получает 403/429. Живой поиск обязан идти первым путём."""
    import inspect
    src = inspect.getsource(cb._avito_cached_result)
    start = src.index('if AVITO_PROVIDER == "webjson":')
    body = src[start:]
    july = body.index("_avito_july_scraper")
    webjson = body.index("_avito_webjson_search")
    assert july < webjson, "июльский парсер должен вызываться раньше webJSON"
    # webJSON — только когда июльский путь пуст (и мы не в паузе).
    assert "if not _direct and not _paused:" in body[july:webjson]


def test_july_scraper_respects_the_time_budget():
    """Обход не должен выходить за время, которое ждёт живой поиск."""
    import inspect
    src = inspect.getsource(cb._avito_july_scraper)
    assert "_avito_budget_sec()" in src
    assert "бюджет" in src


def test_july_scraper_stops_when_first_page_is_blocked():
    """Правило рабочей версии: первая страница заблокирована — дальше не идём."""
    import inspect
    src = inspect.getsource(cb._avito_july_scraper)
    assert "_AVITO_PAGE1_BLOCKED" in src
    assert "остальные страницы пропускаем" in src


# ── Больше объявлений с Авито, не сжигая адрес ───────────────────────


def test_fallback_runs_when_results_are_few_not_only_zero():
    """На узком бюджете ценовой URL отдаёт единицы объявлений.

    Запасной путь (общий список без ценового фильтра) включался только при
    полном нуле, поэтому в выдаче стояло «Avito: 6».
    """
    import inspect
    src = inspect.getsource(cb._avito_july_scraper)
    assert "AVITO_MIN_RESULTS" in src
    assert "len(results) < _min_results" in src


def test_fallback_adds_to_results_instead_of_replacing():
    import inspect
    src = inspect.getsource(cb._avito_july_scraper)
    assert "results = results + [it for it in fb_results" in src


def test_fallback_pages_are_sequential():
    """Пять одновременных запросов с одного адреса — прямой путь к бану."""
    import inspect
    src = inspect.getsource(cb._avito_july_scraper)
    tail = src[src.index("дочитываем без фильтра"):]
    assert "ThreadPoolExecutor" not in tail
    assert "_avito_pace()" in tail
    assert "AVITO_FALLBACK_PAGES" in tail


def test_fallback_respects_the_time_budget():
    import inspect
    src = inspect.getsource(cb._avito_july_scraper)
    tail = src[src.index("дочитываем без фильтра"):]
    assert "_budget" in tail


# ── Пауза не должна глушить рабочий путь ─────────────────────────────


def test_pause_does_not_silence_the_july_path():
    """Паузу включают 403 от webJSON, а объявления отдаёт июльский парсер.

    Если в паузу не пробовать вообще ничего, пользователь гарантированно
    получает ноль — при том что рабочий путь мог бы ответить.
    """
    import inspect
    body = inspect.getsource(cb._avito_cached_result)
    body = body[body.index('if AVITO_PROVIDER == "webjson":'):]
    # В паузу сначала отдаём кэш…
    assert body.index("пауза после ограничения — отдаём") < body.index("_avito_july_scraper(")
    # …а если его нет — короткий заход июльским парсером.
    assert "коротким заходом" in body
    assert "_pages = 2 if _paused" in body


def test_webjson_is_skipped_while_paused():
    """webJSON в паузу не трогаем: его же отказы паузу и включают."""
    import inspect
    body = inspect.getsource(cb._avito_cached_result)
    assert "if not _direct and not _paused:" in body


def test_blocked_first_page_skips_the_fallback_too():
    """Запасной путь бьёт по тому же адресу — при бане это продлевает бан."""
    import inspect
    src = inspect.getsource(cb._avito_july_scraper)
    assert "_AVITO_PAGE1_BLOCKED and not results" in src
    assert "запасной путь не пробуем" in src


def test_cache_is_preferred_over_any_network_call():
    """Свежий кэш и диск идут раньше любых запросов — так поиск переживает
    и перезапуск контейнера, и паузу."""
    import inspect
    body = inspect.getsource(cb._avito_cached_result)
    body = body[body.index('if AVITO_PROVIDER == "webjson":'):]
    net = min(body.index("_avito_july_scraper("), body.index("_avito_webjson_search("))
    assert body.index("if _cached_now and _fresh_enough") < net
    assert body.index("выдача из сохранённой на диске") < net


# ── Авито должен укладываться во время, которое его ждут ─────────────


def test_browser_fallback_is_off_by_default():
    """Замер: один вызов браузера не укладывается и в две минуты, игнорируя
    свой таймаут, и в одиночку срывает весь обход. В WORKING_CONFIG.md
    Playwright и так помечен как нерабочий."""
    assert cb._AVITO_BROWSER_ENABLED is False


def test_dead_api_chain_is_off_by_default():
    """Мобильный API и поисковики стоят ~33 секунды и отдают ноль."""
    assert cb._AVITO_API_CHAIN_ENABLED is False


def test_browser_timeout_fits_the_remaining_budget():
    """Обёртка ждёт (timeout*2 + wait)/1000 + 20 секунд — считаем обратно."""
    for left in (120, 60, 46):
        ms = cb._avito_browser_timeout_ms(left)
        wall = (ms * 2 + 4000) / 1000 + 20
        assert wall <= cb._AVITO_BROWSER_MAX_SEC + 1, (left, wall)
        assert wall <= left, (left, wall)


def test_browser_is_skipped_without_spare_time():
    import inspect
    src = inspect.getsource(cb._avito_july_scraper)
    assert "_AVITO_BROWSER_MIN_SEC" in src
    assert "на браузер не осталось времени" in src


def test_budget_is_checked_even_with_zero_results():
    """При нулевом улове обход шёл минутами и обрывался таймаутом поиска."""
    import inspect
    src = inspect.getsource(cb._avito_july_scraper)
    assert "if (time.time() - _started) >= _budget:" in src
    assert "if results and (time.time() - _started)" not in src


def test_deadline_is_known_before_pages_are_fetched():
    import inspect
    src = inspect.getsource(cb._avito_july_scraper)
    assert src.index("_deadline = _started + _budget") < src.index("def _fetch_page")


# ── Маршруты Авито: пул РФ SOCKS5 и память об отказах ────────────────


def test_july_scraper_tries_the_socks_pool():
    """WORKING_CONFIG.md обещает пул РФ SOCKS5 после основного прокси, но в
    рабочем пути его не было: без PROXY_URL запросы уходили с адреса
    дата-центра, который Авито банит сразу."""
    import inspect
    src = inspect.getsource(cb._avito_july_scraper)
    assert "_avito_socks_routes()" in src
    assert "_avito_remember_route" in src


def test_dead_route_is_not_retried():
    """Три встроенных SOCKS5 успели умереть, и перебор на каждой странице
    съедал больше половины бюджета обхода."""
    cb._AVITO_DEAD_ROUTES.clear()
    cb._avito_remember_route(None)
    total = len(cb._avito_socks_routes())
    assert total >= 1
    cb._avito_mark_route_dead("РФ-socks1")
    assert len(cb._avito_socks_routes()) == total - 1
    cb._AVITO_DEAD_ROUTES.clear()


def test_dead_mark_expires():
    cb._AVITO_DEAD_ROUTES.clear()
    now = 1_000_000.0
    cb._avito_mark_route_dead("РФ-socks1", now=now)
    assert len(cb._avito_socks_routes(now=now + 10)) == 2
    later = now + cb._AVITO_DEAD_ROUTE_COOLDOWN + 1
    assert len(cb._avito_socks_routes(now=later)) == 3
    cb._AVITO_DEAD_ROUTES.clear()


def test_successful_route_is_tried_first_and_revived():
    cb._AVITO_DEAD_ROUTES.clear()
    cb._avito_mark_route_dead("РФ-socks3")
    cb._avito_remember_route("РФ-socks3")          # ответил — снимаем пометку
    routes = cb._avito_socks_routes()
    assert routes[0][0] == "РФ-socks3"
    assert len(routes) == 3
    cb._AVITO_DEAD_ROUTES.clear()
    cb._avito_remember_route(None)
