"""Одиночная диагностика HTML-метода Duff89/parser_avito.

Не импортирует control_bot, не пишет в БД и выполняет максимум один HTTP GET.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv


# Готовая ссылка совпадает с URL поиска Челябинска из текущего проекта.
SEARCH_URL = (
    "https://www.avito.ru/chelyabinsk/avtomobili"
    "?seller_type=1&pmax=100000&s=104"
)
BLOCK_MARKERS = (
    "доступ ограничен",
    "проблема с ip",
    "слишком много запросов",
    "access denied",
    "proxy access denied",
    "подтвердите, что вы не робот",
    "captcha",
)
HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    "Referer": "https://www.avito.ru/chelyabinsk/avtomobili",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class _AvitoStateHTMLParser(HTMLParser):
    """Находит title и целевой script без внешних HTML-зависимостей."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.title_parts: list[str] = []
        self.script_parts: list[str] = []
        self._in_title = False
        self._in_target_script = False
        self.target_script_found = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attrs_map = dict(attrs)
        if tag.lower() == "title":
            self._in_title = True
        elif (
            tag.lower() == "script"
            and attrs_map.get("type") == "mime/invalid"
            and attrs_map.get("data-mfe-state") == "true"
        ):
            self._in_target_script = True
            self.target_script_found = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
        elif tag.lower() == "script" and self._in_target_script:
            self._in_target_script = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_target_script:
            self.script_parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")

    def get_state_payload(self) -> str | None:
        """Возвращает содержимое MFE state script, если оно найдено."""
        parts = getattr(self, "script_parts", None)
        if not getattr(self, "target_script_found", False) or not parts:
            return None
        payload = "".join(parts).strip()
        return payload or None


def _nested(data: Any, *path: str, default: Any = None) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def _first_text(data: dict, paths: tuple[tuple[str, ...], ...]) -> str:
    for path in paths:
        value = _nested(data, *path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _price(item: dict) -> int | float | None:
    for path in (
        ("priceDetailed", "value"),
        ("price", "value"),
        ("price",),
    ):
        value = _nested(item, *path)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            digits = re.sub(r"[^\d]", "", value)
            if digits:
                return int(digits)
    return None


def _image(item: dict) -> str:
    images = item.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return _first_text(
                first,
                (
                    ("url",),
                    ("imageUrl",),
                    ("1280x960",),
                    ("640x480",),
                    ("640x480", "url"),
                ),
            )
    return _first_text(
        item,
        (
            ("imageUrl",),
            ("image", "url"),
            ("photo", "url"),
        ),
    )


def _normalize(item: dict) -> dict:
    item_id = item.get("id") or item.get("itemId") or ""
    path = _first_text(
        item,
        (
            ("urlPath",),
            ("url",),
            ("uri",),
        ),
    )
    if path.startswith("/"):
        path = "https://www.avito.ru" + path
    location = _first_text(
        item,
        (
            ("location", "name"),
            ("location", "display"),
            ("location", "city"),
            ("address",),
        ),
    )
    return {
        "id": str(item_id),
        "title": _first_text(item, (("title",), ("name",))),
        "price": _price(item),
        "url": path,
        "location": location,
        "image": _image(item) or None,
        "published_at": _first_text(
            item,
            (
                ("publishedAt",),
                ("published_at",),
                ("time",),
                ("date",),
            ),
        )
        or None,
        "seller": _first_text(
            item,
            (
                ("seller", "name"),
                ("sellerName",),
                ("shop", "name"),
            ),
        )
        or None,
    }


def main() -> int:
    load_dotenv(dotenv_path=Path(".env"), override=False)
    proxy_url = os.getenv("PROXY_URL", "").strip()
    if not proxy_url:
        print("Ошибка: PROXY_URL не задан; прямой запрос запрещён.")
        return 2

    session = cffi_requests.Session(impersonate="chrome120", trust_env=False)
    try:
        response = session.get(
            SEARCH_URL,
            headers=HEADERS,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=20,
        )
    except Exception as exc:
        if "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower():
            print(f"Ошибка прокси: {type(exc).__name__}: {exc}")
            print("Сначала нужен рабочий прокси; повторный запрос не выполнялся.")
        else:
            print(f"Ошибка запроса: {type(exc).__name__}: {exc}")
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

    if blocked:
        print(
            "Метод парсинга проверить нельзя из-за сетевой блокировки; "
            "дополнительных запросов не было."
        )
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
    if items:
        normalized = [
            _normalize(item) for item in items[:3] if isinstance(item, dict)
        ]
        print(
            "Первые 3 объявления:\n"
            + json.dumps(normalized, ensure_ascii=False, indent=2)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
