"""Тесты обхода антибота Авито: темп запросов, заголовки, эндпоинты, разбор JSON.

Ни один тест НЕ ходит в сеть: используются синтетические JSON/HTML и подставные
зависимости. control_bot.py целиком импортировать нельзя (тянет aiogram и внешние
сервисы), поэтому нужные функции извлекаются из исходника через AST и исполняются
в изолированном пространстве имён — тестируется настоящий код, а не его копия.
"""

from __future__ import annotations

import ast
import datetime
import json
import os
import re
import time
import unittest
import uuid
from pathlib import Path

CONTROL_BOT = Path(__file__).resolve().parent / "control_bot.py"
SOURCE = CONTROL_BOT.read_text(encoding="utf-8")

# Функции, которые вытаскиваем из control_bot.py для проверки.
_WANTED_FUNCS = {
    "_avito_pace_delay",
    "_avito_ip_budget_spend",
    "_avito_ip_budget_reset",
    "_avito_mobile_api_endpoints",
    "_avito_webjson_endpoints",
    "_avito_mobile_headers",
    "_avito_web_xhr_headers",
    "_avito_items_from_any_json",
}
_WANTED_CONSTS = {
    "AVITO_MOBILE_API_KEY",
    "_AVITO_MOBILE_API_VERSIONS",
    "_AVITO_WEBJSON_VERSIONS",
}


def _load_namespace() -> dict:
    """Пространство имён с настоящими функциями Авито-обхода из control_bot.py."""
    tree = ast.parse(SOURCE)
    picked: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANTED_FUNCS:
            picked.append(node)
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & _WANTED_CONSTS:
                picked.append(node)
    module = ast.Module(body=picked, type_ignores=[])
    ns: dict = {
        "os": os, "re": re, "time": time, "json": json,
        "datetime": datetime, "uuid": uuid,
        # заглушки внешних зависимостей — по умолчанию «ничего не нашли»
        "_avito_user_agent": lambda: (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "_deep_get": _fake_deep_get,
        "_avito_find_items_in_json": lambda obj, depth=0: [],
        "_avito_duck_items": lambda obj, region, acc, depth=0, seen_ids=None: acc,
        "_avito_item_from_json": lambda it, today: None,
        "_rotate_proxy_ip": lambda min_interval=50.0, force=False: True,
        # настройки темпа/бюджета — фиксируем, чтобы тесты не зависели от окружения
        "AVITO_PACE_MIN_MS": 700,
        "AVITO_PACE_MAX_MS": 2200,
        "AVITO_IP_BUDGET": 3,
        "AVITO_PROXY_ROTATE_URL": "https://rotate.example/ip",
        "_AVITO_IP_SPENT": 0,
        "_AVITO_LAST_REQUEST_AT": 0.0,
    }
    exec(compile(module, str(CONTROL_BOT), "exec"), ns)
    missing = _WANTED_FUNCS - set(ns)
    if missing:
        raise AssertionError(f"в control_bot.py нет функций: {sorted(missing)}")
    return ns


def _fake_deep_get(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


NS = _load_namespace()


class TestPacing(unittest.TestCase):
    """Джиттер между запросами: Авито ловит ботов по ритму, а не только по TLS."""

    def test_delay_is_inside_configured_window(self):
        for rnd in (0.0, 0.25, 0.5, 1.0):
            d = NS["_avito_pace_delay"](now=1000.0, last=1000.0, rnd=rnd)
            self.assertGreaterEqual(d, 0.7)
            self.assertLessEqual(d, 2.2)

    def test_delay_grows_with_random_value(self):
        low = NS["_avito_pace_delay"](now=1000.0, last=1000.0, rnd=0.0)
        high = NS["_avito_pace_delay"](now=1000.0, last=1000.0, rnd=1.0)
        self.assertLess(low, high)

    def test_first_request_is_not_delayed(self):
        self.assertEqual(NS["_avito_pace_delay"](now=1000.0, last=0.0, rnd=1.0), 0.0)

    def test_recent_request_shortens_wait(self):
        """Если пауза уже «набежала» сама — спать повторно не нужно."""
        d = NS["_avito_pace_delay"](now=1000.0, last=999.0, rnd=0.0)
        self.assertAlmostEqual(d, 0.0, places=3)

    def test_never_negative(self):
        d = NS["_avito_pace_delay"](now=1000.0, last=500.0, rnd=1.0)
        self.assertEqual(d, 0.0)

    def test_bad_random_value_does_not_crash(self):
        self.assertGreaterEqual(NS["_avito_pace_delay"](now=1.0, last=0.0, rnd="x"), 0.0)
        self.assertGreaterEqual(NS["_avito_pace_delay"](now=1.0, last=0.0, rnd=5.0), 0.0)


class TestIpBudget(unittest.TestCase):
    """Бюджет запросов на один IP: после N запросов берём свежий адрес."""

    def setUp(self):
        NS["_AVITO_IP_SPENT"] = 0
        self.rotations = []
        NS["_rotate_proxy_ip"] = lambda min_interval=50.0, force=False: (
            self.rotations.append(min_interval) or True)

    def test_rotates_only_when_budget_exhausted(self):
        self.assertFalse(NS["_avito_ip_budget_spend"]())
        self.assertFalse(NS["_avito_ip_budget_spend"]())
        self.assertTrue(NS["_avito_ip_budget_spend"]())   # бюджет = 3
        self.assertEqual(len(self.rotations), 1)

    def test_counter_resets_after_rotation(self):
        for _ in range(3):
            NS["_avito_ip_budget_spend"]()
        self.assertEqual(NS["_AVITO_IP_SPENT"], 0)
        self.assertFalse(NS["_avito_ip_budget_spend"]())

    def test_disabled_without_rotate_url(self):
        NS["AVITO_PROXY_ROTATE_URL"] = ""
        try:
            for _ in range(10):
                self.assertFalse(NS["_avito_ip_budget_spend"]())
            self.assertEqual(self.rotations, [])
        finally:
            NS["AVITO_PROXY_ROTATE_URL"] = "https://rotate.example/ip"

    def test_rotation_failure_is_swallowed(self):
        def boom(min_interval=50.0, force=False):
            raise RuntimeError("прокси недоступен")
        NS["_rotate_proxy_ip"] = boom
        for _ in range(3):
            self.assertFalse(NS["_avito_ip_budget_spend"]())

    def test_reset_clears_counter(self):
        NS["_avito_ip_budget_spend"]()
        NS["_avito_ip_budget_reset"]()
        self.assertEqual(NS["_AVITO_IP_SPENT"], 0)


class TestEndpoints(unittest.TestCase):
    """Порядок перебора точек входа."""

    def test_mobile_endpoints_newest_first(self):
        eps = NS["_avito_mobile_api_endpoints"]()
        self.assertTrue(all(e.startswith("https://m.avito.ru/api/") for e in eps))
        self.assertTrue(all(e.endswith("/items") for e in eps))
        vers = [int(e.rsplit("/", 2)[-2]) for e in eps]
        self.assertEqual(vers, sorted(vers, reverse=True))
        # старые поколения API обычно защищены слабее — они обязаны быть в списке
        self.assertIn(11, vers)
        self.assertIn(9, vers)

    def test_webjson_endpoints_cover_several_generations(self):
        eps = NS["_avito_webjson_endpoints"]()
        self.assertIn("https://www.avito.ru/web/1/js/items", eps)
        self.assertGreaterEqual(len(eps), 2)
        self.assertEqual(len(set(eps)), len(eps))

    def test_mobile_api_key_is_configurable(self):
        self.assertTrue(NS["AVITO_MOBILE_API_KEY"])
        self.assertIn("AVITO_MOBILE_API_KEY", SOURCE)
        self.assertIn('os.getenv("AVITO_MOBILE_API_KEY"', SOURCE)


class TestHeaders(unittest.TestCase):
    def test_mobile_headers_look_like_the_android_app(self):
        h = NS["_avito_mobile_headers"]()
        self.assertIn("ru.avito.avitomobile", h["User-Agent"])
        self.assertEqual(h["x-source"], "android")
        self.assertTrue(h["x-app-version"])
        self.assertEqual(len(h["X-Request-Id"]), 32)
        # у каждого запроса свой идентификатор — как у настоящего клиента
        self.assertNotEqual(h["X-Request-Id"],
                            NS["_avito_mobile_headers"]()["X-Request-Id"])

    def test_xhr_headers_declare_cors_not_navigation(self):
        h = NS["_avito_web_xhr_headers"]("https://www.avito.ru/moskva/avtomobili")
        self.assertEqual(h["Sec-Fetch-Dest"], "empty")
        self.assertEqual(h["Sec-Fetch-Mode"], "cors")
        self.assertEqual(h["Sec-Fetch-Site"], "same-origin")
        self.assertEqual(h["x-requested-with"], "XMLHttpRequest")
        self.assertEqual(h["Origin"], "https://www.avito.ru")
        self.assertEqual(h["Referer"], "https://www.avito.ru/moskva/avtomobili")
        # у XHR не бывает Upgrade-Insecure-Requests / Sec-Fetch-User
        self.assertNotIn("Upgrade-Insecure-Requests", h)
        self.assertNotIn("Sec-Fetch-User", h)

    def test_xhr_client_hints_match_user_agent_version(self):
        h = NS["_avito_web_xhr_headers"]("https://www.avito.ru/moskva/avtomobili")
        ver = re.search(r"Chrome/(\d+)", h["User-Agent"]).group(1)
        self.assertIn(f'"Google Chrome";v="{ver}"', h["sec-ch-ua"])

    def test_xhr_headers_use_cookie_user_agent(self):
        NS["_avito_user_agent"] = lambda: "Mozilla/5.0 ... Chrome/99.0.0.0 Safari/537.36"
        try:
            h = NS["_avito_web_xhr_headers"]("https://www.avito.ru/x")
            self.assertIn("Chrome/99", h["User-Agent"])
            self.assertIn('v="99"', h["sec-ch-ua"])
        finally:
            NS["_avito_user_agent"] = lambda: (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class TestItemsFromAnyJson(unittest.TestCase):
    """Разбор ответа любого эндпоинта Авито."""

    def setUp(self):
        self.today = datetime.date(2026, 8, 4)
        NS["_avito_find_items_in_json"] = lambda obj, depth=0: []
        NS["_avito_duck_items"] = lambda obj, region, acc, depth=0, seen_ids=None: acc
        NS["_avito_item_from_json"] = self._fake_item

    @staticmethod
    def _fake_item(it, today):
        title = it.get("title")
        _id = it.get("id")
        if not title or not _id:
            return None
        return {"source": "avito", "title": title,
                "url": f"https://www.avito.ru/a/{_id}", "_price_int": it.get("price", 0)}

    def test_mobile_api_result_items(self):
        data = {"result": {"items": [{"id": 1, "title": "Toyota Camry"},
                                     {"id": 2, "title": "Kia Rio"}]}}
        out = NS["_avito_items_from_any_json"](data, "moskva", self.today)
        self.assertEqual([i["title"] for i in out], ["Toyota Camry", "Kia Rio"])

    def test_mobile_api_value_wrapper_is_unwrapped(self):
        """Мобильный API кладёт карточку в it['value'] — её надо развернуть."""
        data = {"result": {"items": [
            {"type": "item", "value": {"id": 77, "title": "Lada Vesta", "price": 500000}},
        ]}}
        out = NS["_avito_items_from_any_json"](data, "moskva", self.today)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Lada Vesta")
        self.assertEqual(out[0]["_price_int"], 500000)

    def test_web_json_catalog_items(self):
        data = {"catalog": {"items": [{"id": 5, "title": "Ford Focus"}]}}
        out = NS["_avito_items_from_any_json"](data, "moskva", self.today)
        self.assertEqual(len(out), 1)

    def test_flat_items_key(self):
        data = {"items": [{"id": 9, "title": "Mazda 3"}]}
        self.assertEqual(len(NS["_avito_items_from_any_json"](data, "moskva", self.today)), 1)

    def test_duplicates_are_dropped(self):
        data = {"items": [{"id": 9, "title": "Mazda 3"}, {"id": 9, "title": "Mazda 3"}]}
        self.assertEqual(len(NS["_avito_items_from_any_json"](data, "moskva", self.today)), 1)

    def test_falls_back_to_duck_typing_on_unknown_shape(self):
        """Переименовали поля — работает поиск по признакам, а не по ключу."""
        NS["_avito_duck_items"] = lambda obj, region, acc, depth=0, seen_ids=None: (
            acc.extend([{"id": 42, "title": "Skoda Octavia"}]) or acc)
        data = {"payload": {"weird": {"blocks": []}}}
        out = NS["_avito_items_from_any_json"](data, "moskva", self.today)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "Skoda Octavia")

    def test_blocked_json_gives_empty_list_not_exception(self):
        blocked = {"too-many-requests": True,
                   "message": "Доступ с вашего IP-адреса временно ограничен"}
        self.assertEqual(NS["_avito_items_from_any_json"](blocked, "moskva", self.today), [])

    def test_garbage_input_is_safe(self):
        for bad in (None, [], "", 0, {"result": {"items": "нет"}}, {"items": [None, 1, "x"]}):
            self.assertEqual(NS["_avito_items_from_any_json"](bad, "moskva", self.today), [])

    def test_item_converter_exception_does_not_break_the_batch(self):
        def flaky(it, today):
            if it.get("id") == 2:
                raise ValueError("битая карточка")
            return TestItemsFromAnyJson._fake_item(it, today)
        NS["_avito_item_from_json"] = flaky
        data = {"items": [{"id": 1, "title": "A"}, {"id": 2, "title": "B"},
                          {"id": 3, "title": "C"}]}
        out = NS["_avito_items_from_any_json"](data, "moskva", self.today)
        self.assertEqual([i["title"] for i in out], ["A", "C"])

    def test_duck_typing_exception_is_swallowed(self):
        def boom(obj, region, acc, depth=0, seen_ids=None):
            raise RecursionError("слишком глубоко")
        NS["_avito_duck_items"] = boom
        self.assertEqual(NS["_avito_items_from_any_json"]({"x": 1}, "moskva", self.today), [])


class TestPipelineWiring(unittest.TestCase):
    """Новые шаги действительно встроены в цепочку поиска (проверка по исходнику)."""

    @staticmethod
    def _func_source(name: str) -> str:
        for node in ast.parse(SOURCE).body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(SOURCE, node) or ""
        raise AssertionError(f"функция {name} не найдена")

    def test_webjson_uses_xhr_headers_and_endpoint_list(self):
        src = self._func_source("_avito_webjson_search")
        self.assertIn("_avito_web_xhr_headers(", src)
        self.assertIn("_avito_webjson_endpoints()", src)

    def test_webjson_paces_requests_and_spends_ip_budget(self):
        src = self._func_source("_avito_webjson_search")
        self.assertIn("_avito_pace()", src)
        self.assertIn("_avito_ip_budget_spend()", src)

    def test_webjson_falls_back_to_mobile_api_after_html(self):
        src = self._func_source("_avito_webjson_search")
        self.assertIn("_avito_html_search(", src)
        self.assertIn("_avito_mobile_api_search(", src)
        self.assertLess(src.index("_avito_html_search("),
                        src.index("_avito_mobile_api_search("),
                        "мобильный API должен пробоваться после HTML, а не вместо него")

    def test_mobile_api_fallback_cannot_break_the_search(self):
        """Падение мобильного API не должно ронять весь поиск."""
        src = self._func_source("_avito_webjson_search")
        tail = src[src.index("_avito_mobile_api_search(") - 400:]
        self.assertIn("except Exception", tail)

    def test_html_search_paces_requests(self):
        src = self._func_source("_avito_html_search")
        self.assertIn("_avito_pace()", src)
        self.assertIn("_avito_ip_budget_spend()", src)

    def test_mobile_api_uses_android_tls_profile(self):
        src = self._func_source("_avito_mobile_api_search")
        self.assertIn("chrome131_android", src)
        self.assertIn("_avito_mobile_headers()", src)
        self.assertIn("_avito_items_from_any_json(", src)

    def test_mobile_api_logs_non_200_answers(self):
        """Без лога статуса чинить блокировку вслепую невозможно."""
        src = self._func_source("_avito_mobile_api_search")
        self.assertIn("mobileAPI", src)
        self.assertIn("r.status_code", src)

    def test_other_marketplaces_untouched(self):
        for marker in ("drom.ru", "youla", "vk.com"):
            self.assertIn(marker, SOURCE.lower(),
                          f"пропала поддержка {marker} — правки задели чужой маркетплейс")


if __name__ == "__main__":
    unittest.main()
