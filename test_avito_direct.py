"""Одиночная диагностика прямого доступа VPS к странице поиска Авито."""

from __future__ import annotations

import html as html_lib
import json
import re
import time

from curl_cffi import requests as cffi_requests

from test_avito_duff_method import (
    BLOCK_MARKERS,
    HEADERS,
    SEARCH_URL,
    _AvitoStateHTMLParser,
    _nested,
)


def main() -> int:
    # Новая сессия не читает proxy-переменные окружения и не имеет старых cookies.
    session = cffi_requests.Session(impersonate="chrome120", trust_env=False)
    started = time.monotonic()
    try:
        response = session.get(
            SEARCH_URL,
            headers=HEADERS,
            proxies={},
            timeout=20,
        )
        elapsed = time.monotonic() - started
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"Время запроса: {elapsed:.2f} с")
        print(f"Ошибка: {type(exc).__name__}: {exc}")
        if "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower():
            print("VPS не может установить соединение с Авито напрямую.")
        return 1
    finally:
        try:
            session.cookies.clear()
        except Exception:
            pass
        session.close()

    document = response.text or ""
    page_parser = _AvitoStateHTMLParser()
    page_parser.feed(document)
    title = re.sub(r"\s+", " ", "".join(page_parser.title_parts)).strip()
    blocked = response.status_code in (403, 429) or any(
        marker in document.lower() for marker in BLOCK_MARKERS
    )
    state_script_found = page_parser.target_script_found

    print(f"HTTP: {response.status_code}")
    print(f"Итоговый URL: {response.url}")
    print(f"Title: {title}")
    print(f"Размер HTML: {len(response.content or b'')} байт")
    print(f"Текст блокировки: {'да' if blocked else 'нет'}")
    print(
        "Тег mime/invalid data-mfe-state: "
        f"{'да' if state_script_found else 'нет'}"
    )
    print(f"Время запроса: {elapsed:.2f} с")

    if response.status_code in (403, 429):
        print("Прямой IP VPS заблокирован Авито.")
        return 3
    if response.status_code != 200 or not state_script_found:
        return 4

    try:
        raw_state = "".join(page_parser.script_parts)
        state = json.loads(html_lib.unescape(raw_state))
        loader_data = _nested(state, "loaderData", "data", default={})
        if not isinstance(loader_data, dict):
            loader_data = {}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Ошибка JSON: {type(exc).__name__}: {exc}")
        return 5

    catalog = loader_data.get("catalog")
    search_core = loader_data.get("searchCore")
    context = loader_data.get("context")
    items = catalog.get("items", []) if isinstance(catalog, dict) else []
    if not isinstance(items, list):
        items = []

    print(f"Catalog найден: {'да' if isinstance(catalog, dict) else 'нет'}")
    print(f"SearchCore найден: {'да' if search_core is not None else 'нет'}")
    print(f"Context найден: {'да' if context is not None else 'нет'}")
    print(f"Items в catalog: {len(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
