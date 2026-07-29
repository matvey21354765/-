"""Изолированный провайдер выдачи Авито через обязательный mobile proxy.

Провайдер выполняет один исходный GET и допускает ровно один внутренний
канонический переход, описанный в loaderData.data. Retry и fallback отсутствуют.
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import json
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from curl_cffi import requests


class AvitoVpnUnavailable(RuntimeError):
    """Настроенный mobile proxy недоступен или запрос завершился timeout."""


class AvitoProxyConnectionError(AvitoVpnUnavailable):
    """Настроенный proxy недоступен; прямой fallback запрещён."""


class AvitoProxyAuthenticationError(RuntimeError):
    """Proxy отклонил credentials (HTTP 407)."""


class AvitoBlockedError(RuntimeError):
    """Авито ограничил текущий выходной IP."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AvitoRateLimitedError(AvitoBlockedError):
    """Авито вернул HTTP 429."""


class AvitoParseError(RuntimeError):
    """Ответ Авито не содержит ожидаемого структурированного состояния."""


class AvitoRedirectLoopError(RuntimeError):
    """Авито вернул второй внутренний редирект."""


class _StateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.title_parts: list[str] = []
        self.script_parts: list[str] = []
        self._in_title = False
        self._in_state = False
        self.state_found = False

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
            self._in_state = True
            self.state_found = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
        elif tag.lower() == "script" and self._in_state:
            self._in_state = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_state:
            self.script_parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")

    @property
    def title(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.title_parts)).strip()

    def get_state_payload(self) -> str | None:
        parts = getattr(self, "script_parts", None)
        if not getattr(self, "state_found", False) or not parts:
            return None
        payload = "".join(parts).strip()
        return payload or None


def _nested(value: Any, *path: str) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_text(item: dict, *paths: tuple[str, ...]) -> str:
    for path in paths:
        value = _nested(item, *path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _price(item: dict) -> int | None:
    for path in (("priceDetailed", "value"), ("price", "value"), ("price",)):
        value = _nested(item, *path)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            digits = re.sub(r"\D", "", value)
            if digits:
                return int(digits)
    return None


def _image(item: dict) -> str | None:
    images = item.get("images")
    if isinstance(images, list):
        for image in images:
            if isinstance(image, str) and image:
                return image
            if isinstance(image, dict):
                for key in ("1280x960", "640x480", "472x472", "416x416", "url"):
                    value = image.get(key)
                    if isinstance(value, str) and value:
                        return value
    value = _first_text(
        item,
        ("gallery", "imageLargeUrl"),
        ("gallery", "imageUrl"),
        ("imageUrl",),
    )
    return value or None


def _published_at(item: dict) -> str | None:
    value = (
        item.get("sortTimeStamp")
        or item.get("publishedAt")
        or item.get("published_at")
        or item.get("time")
    )
    if isinstance(value, (int, float)):
        timestamp = float(value) / 1000 if value > 10_000_000_000 else float(value)
        try:
            return _dt.datetime.fromtimestamp(
                timestamp, tz=_dt.timezone.utc
            ).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _seller(item: dict) -> str | None:
    direct = _first_text(
        item,
        ("seller", "name"),
        ("sellerName",),
        ("shop", "name"),
        ("user", "name"),
    )
    if direct:
        return direct
    steps = _nested(item, "iva", "UserInfoStep")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            payload = step.get("payload")
            if isinstance(payload, dict):
                value = (
                    payload.get("name")
                    or payload.get("title")
                    or payload.get("userName")
                )
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def normalize_item(item: dict) -> dict:
    item_id = item.get("id") or item.get("itemId") or ""
    url = _first_text(item, ("urlPath",), ("url",), ("uri",))
    if url.startswith("/"):
        url = urljoin("https://www.avito.ru", url)
    return {
        "id": str(item_id),
        "title": _first_text(item, ("title",), ("name",)),
        "price": _price(item),
        "url": url,
        "location": _first_text(
            item,
            ("location", "name"),
            ("location", "display"),
            ("location", "city"),
            ("address",),
        ),
        "image": _image(item),
        "published_at": _published_at(item),
        "seller": _seller(item),
        "source": "avito",
    }


class AvitoDuffProvider:
    def __init__(
        self,
        socks_proxy: str = "socks5h://127.0.0.1:10808",
        timeout: float = 20,
        max_internal_redirects: int = 1,
    ) -> None:
        self.socks_proxy = socks_proxy
        self.timeout = float(timeout)
        self.max_internal_redirects = max(0, min(1, int(max_internal_redirects)))
        self.last_diagnostics: dict[str, Any] = {}

    @property
    def proxies(self) -> dict[str, str]:
        parsed = urlparse(self.socks_proxy)
        if (
            parsed.scheme not in {"http", "socks5h"}
            or not parsed.hostname
            or parsed.port is None
        ):
            raise AvitoProxyConnectionError(
                "Корректный обязательный HTTP/SOCKS5H proxy не настроен"
            )
        return {"http": self.socks_proxy, "https": self.socks_proxy}

    @staticmethod
    def _headers(url: str) -> dict[str, str]:
        parsed = urlparse(url)
        referer_path = parsed.path.rsplit("/", 1)[0] or "/"
        return {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
            "Referer": f"https://www.avito.ru{referer_path}",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    @staticmethod
    def _safe_internal_url(base_url: str, target: str) -> str:
        if not isinstance(target, str) or not target.strip():
            raise AvitoParseError("Внутренний redirect не содержит data.url")
        lowered = target.strip().lower()
        if lowered.startswith(("javascript:", "data:")):
            raise AvitoParseError("Небезопасная схема внутреннего redirect")
        absolute = urljoin(base_url, target.strip())
        parsed = urlparse(absolute)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() not in {"avito.ru", "www.avito.ru"}
            or not parsed.path.startswith("/")
        ):
            raise AvitoParseError("Внутренний redirect ведёт вне avito.ru")
        return absolute

    def _request(self, session, url: str):
        started = time.monotonic()
        try:
            response = session.get(
                url,
                headers=self._headers(url),
                proxies=self.proxies,
                timeout=self.timeout,
                allow_redirects=True,
            )
        except Exception as exc:
            elapsed = time.monotonic() - started
            self.last_diagnostics.update({
                "http": None,
                "seconds": elapsed,
                "error": type(exc).__name__,
            })
            raise AvitoProxyConnectionError(
                f"Proxy недоступен: {type(exc).__name__}"
            ) from exc
        self.last_diagnostics.update({
            "http": int(response.status_code),
            "seconds": time.monotonic() - started,
            "final_url": str(response.url),
            "html_bytes": len(response.content or b""),
        })
        return response

    @staticmethod
    def _parse_response(response) -> tuple[dict, str]:
        document = response.text or ""
        parser = _StateParser()
        parser.feed(document)
        title = parser.title
        if response.status_code == 407:
            raise AvitoProxyAuthenticationError(
                "Proxy authentication failed: HTTP 407"
            )
        if response.status_code == 429:
            raise AvitoRateLimitedError(
                "Авито временно ограничил текущий IP: HTTP 429",
                429,
            )
        if response.status_code == 403 or "доступ ограничен" in title.lower():
            raise AvitoBlockedError(
                f"Авито ограничил доступ: HTTP {response.status_code}",
                int(response.status_code),
            )
        payload = parser.get_state_payload()
        if not payload:
            raise AvitoParseError("mime/invalid data-mfe-state не найден")
        try:
            state = json.loads(_html.unescape(payload))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AvitoParseError(f"Некорректный JSON состояния: {exc}") from exc
        loader_data = _nested(state, "loaderData", "data")
        if not isinstance(loader_data, dict):
            raise AvitoParseError("loaderData.data отсутствует")
        return loader_data, title

    @staticmethod
    def _is_internal_redirect(loader_data: dict) -> bool:
        return bool(
            loader_data.get("redirected") is True
            or _nested(loader_data, "status", "code") == 301
        )

    def search(self, search_url: str) -> list[dict]:
        self.last_diagnostics = {
            "requests": 0,
            "internal_redirect": False,
            "catalog_items": 0,
            "normalized": 0,
        }
        session = requests.Session(impersonate="chrome120", trust_env=False)
        try:
            response = self._request(session, search_url)
            self.last_diagnostics["requests"] += 1
            loader_data, title = self._parse_response(response)
            self.last_diagnostics["title"] = title

            if self._is_internal_redirect(loader_data):
                if self.max_internal_redirects < 1:
                    raise AvitoRedirectLoopError(
                        "Внутренние redirect отключены настройкой"
                    )
                canonical_url = self._safe_internal_url(
                    str(response.url), loader_data.get("url", "")
                )
                self.last_diagnostics.update({
                    "internal_redirect": True,
                    "canonical_url": canonical_url,
                })
                response = self._request(session, canonical_url)
                self.last_diagnostics["requests"] += 1
                loader_data, title = self._parse_response(response)
                self.last_diagnostics["title"] = title
                if self._is_internal_redirect(loader_data):
                    raise AvitoRedirectLoopError(
                        "Авито вернул второй внутренний redirect"
                    )

            catalog = loader_data.get("catalog")
            if not isinstance(catalog, dict):
                raise AvitoParseError("catalog отсутствует")
            items = catalog.get("items")
            if not isinstance(items, list):
                raise AvitoParseError("catalog.items отсутствует")
            normalized = [
                normalize_item(item) for item in items if isinstance(item, dict)
            ]
            self.last_diagnostics.update({
                "catalog_items": len(items),
                "normalized": len(normalized),
                "search_core_found": loader_data.get("searchCore") is not None,
                "context_found": loader_data.get("context") is not None,
            })
            return normalized
        finally:
            try:
                session.cookies.clear()
            except Exception:
                pass
            session.close()
