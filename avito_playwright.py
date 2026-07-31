"""Серверный headless Playwright-провайдер для Avito.

Telegram-бот → серверный Playwright worker → резидентский proxy pool / AVITO_PROXY_URL → Avito.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import re
import time
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

from avito_proxy_pool import AvitoProxyPool, get_avito_proxy_pool

try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext, Playwright
except ImportError as _imp_err:  # pragma: no cover - ci without playwright
    async_playwright = None
    Page = Browser = BrowserContext = Playwright = Any


class AvitoPlaywrightError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class AvitoPlaywrightConfigError(AvitoPlaywrightError):
    def __init__(self, message: str) -> None:
        super().__init__("configuration", message)


def _bool_env(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_proxy_url(raw: str) -> dict:
    """Безопасно разбирает AVITO_PROXY_URL в формат Playwright."""
    parsed = urlparse(raw.strip())
    scheme = parsed.scheme.lower() or "http"
    if scheme == "socks5":
        scheme = "socks5"
    if scheme not in {"http", "https", "socks5"}:
        raise AvitoPlaywrightConfigError(f"Неподдерживаемый протокол прокси: {scheme}")
    if not parsed.hostname or parsed.port is None:
        raise AvitoPlaywrightConfigError("AVITO_PROXY_URL должен содержать host:port")
    server = f"{scheme}://{parsed.hostname}:{parsed.port}"
    config: dict[str, Any] = {"server": server}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


def _safe_proxy_summary(config: dict) -> dict[str, Any]:
    server = config.get("server", "")
    host_port = server.split("://", 1)[-1] if "://" in server else server
    return {
        "proxy_configured": True,
        "proxy_protocol": config.get("server", "").split("://", 1)[0] or "http",
        "proxy_host_safe": host_port,
        "credentials_present": bool(config.get("username") and config.get("password")),
    }


def _detect_page_state(text: str, status: int | None = None) -> dict[str, Any]:
    """Определяет CAPTCHA, блокировку, proxy-ошибки по тексту страницы/статусу."""
    t = (text or "").lower()
    status = status or 0
    result = {"captcha_detected": False, "blocked_detected": False, "error": None}

    if status in (403, 407) or "proxy authentication" in t or "connect tunnel failed" in t:
        if status == 407 or "proxy authentication" in t:
            result["error"] = "proxy_auth"
        else:
            result["error"] = "proxy_connect_forbidden"
        return result

    captcha_markers = (
        "captcha", "smartcaptcha", "я не робот", "вы не робот",
        "проверка, что вы не робот", "подтвердите, что вы не робот",
        "antibot", "antispam",
    )
    if any(m in t for m in captcha_markers):
        result["captcha_detected"] = True
        result["error"] = "captcha"
        return result

    block_markers = (
        "доступ ограничен", "доступ временно ограничен", "превышен лимит",
        "too many requests", "too-many-requests", "вы заблокированы",
        "подозрительная активность", "с вашего ip поступает",
    )
    if status in (429, 503) or any(m in t for m in block_markers):
        result["blocked_detected"] = True
        result["error"] = "blocked"
        return result

    return result


def _proxy_error_restartable(error: str | None) -> bool:
    """Ошибки, при которых можно один раз переключить порт и повторить поиск."""
    if not error:
        return False
    return error in {
        "no_exit_node",
        "proxy_auth",
        "proxy_connect",
        "proxy_timeout",
        "proxy_dns",
        "proxy_error",
        "proxy_unavailable",
        "blocked",
        "captcha",
    }


class AvitoBrowserManager:
    def __init__(
        self,
        proxy_url: str | None = None,
        headless: bool | None = None,
        timeout_seconds: float | None = None,
        navigation_timeout_seconds: float | None = None,
        max_concurrent_pages: int | None = None,
        restart_after_searches: int | None = None,
    ) -> None:
        self.proxy_url = (proxy_url or os.getenv("AVITO_PROXY_URL", "")).strip()
        self.headless = headless if headless is not None else _bool_env(os.getenv("AVITO_HEADLESS"), True)
        self.timeout = float(timeout_seconds or os.getenv("AVITO_BROWSER_TIMEOUT_SECONDS", "30"))
        self.navigation_timeout = float(navigation_timeout_seconds or os.getenv("AVITO_NAVIGATION_TIMEOUT_SECONDS", "30"))
        self.max_concurrent = int(max_concurrent_pages or os.getenv("AVITO_MAX_CONCURRENT_PAGES", "1"))
        self.restart_after = int(restart_after_searches or os.getenv("AVITO_BROWSER_RESTART_AFTER_SEARCHES", "50"))

        # Резидентский пул имеет приоритет. AVITO_PROXY_URL — fallback.
        self._proxy_pool: AvitoProxyPool | None = None
        if self._pool_configured():
            self._proxy_pool = get_avito_proxy_pool()

        self._proxy_config: dict | None = None
        self._launch_proxy_config: dict | None = None

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, self.max_concurrent))
        self._search_count = 0
        self._started = False

    @staticmethod
    def _pool_configured() -> bool:
        host = os.getenv("AVITO_PROXY_HOST", "").strip()
        try:
            start = int(os.getenv("AVITO_PROXY_PORT_START", "0") or 0)
            end = int(os.getenv("AVITO_PROXY_PORT_END", "0") or 0)
        except (TypeError, ValueError):
            start = end = 0
        user = os.getenv("AVITO_PROXY_USERNAME", "").strip()
        pwd = os.getenv("AVITO_PROXY_PASSWORD", "").strip()
        return bool(host and start and end >= start and user and pwd)

    def proxy_summary(self) -> dict[str, Any]:
        if self._proxy_pool and self._proxy_pool.configured:
            return {
                **self._proxy_pool.summary(),
                "proxy_configured": self._proxy_config is not None,
            }
        if not self._proxy_config:
            return {"proxy_configured": False}
        return _safe_proxy_summary(self._proxy_config)

    async def _resolve_proxy(self) -> bool:
        """Выбирает/парсит прокси. Возвращает True если готов к запуску."""
        if self._proxy_pool and self._proxy_pool.configured:
            port = await self._proxy_pool.select_working_proxy()
            if port is None:
                self._proxy_config = None
                return False
            self._proxy_config = self._proxy_pool.get_playwright_config()
            return True

        if self.proxy_url:
            try:
                self._proxy_config = _parse_proxy_url(self.proxy_url)
                return True
            except Exception:
                self._proxy_config = None
                return False

        self._proxy_config = None
        return False

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            if async_playwright is None:
                raise AvitoPlaywrightConfigError("playwright не установлен")
            if not await self._resolve_proxy():
                raise AvitoPlaywrightConfigError("Avito proxy не настроен")
            await self._launch()

    async def _launch(self) -> None:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": self.headless,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        }
        if self._proxy_config:
            launch_kwargs["proxy"] = self._proxy_config
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context()
        self._launch_proxy_config = dict(self._proxy_config) if self._proxy_config else None
        self._started = True

    async def _ensure_browser(self) -> None:
        """Запускает или перезапускает браузер, если прокси изменился."""
        if not self._started:
            await self.start()
            return
        if self._proxy_config != self._launch_proxy_config:
            await self._restart_same_pool()

    async def _restart_same_pool(self) -> None:
        await self.stop()
        await self._launch()

    async def _restart_with_new_proxy(self) -> bool:
        """Переключает порт (если пул) и перезапускает browser. Максимум один раз."""
        if self._proxy_pool:
            port = self._proxy_pool.get_current_proxy()
            await self._proxy_pool.mark_failed(port.get("port") if port else None, "restart")
            await self._proxy_pool.reset_current_proxy()
        if not await self._resolve_proxy():
            return False
        await self.stop()
        await self._launch()
        return True

    async def stop(self) -> None:
        async with self._lock:
            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            self._started = False
            self._launch_proxy_config = None
            self._search_count = 0

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def get_browser(self) -> Browser:
        await self._ensure_browser()
        if self._browser is None:
            raise AvitoPlaywrightError("browser_not_started", "Браузер не запущен")
        return self._browser

    async def new_page(self) -> Page:
        browser = await self.get_browser()
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(self.timeout * 1000)
        page.set_default_navigation_timeout(self.navigation_timeout * 1000)

        async def _route_handler(route, request):
            resource_type = request.resource_type
            if resource_type in ("video", "media", "font", "websocket"):
                await route.abort()
            else:
                await route.continue_()

        try:
            await page.route("**/*", _route_handler)
        except Exception:
            pass
        return page

    async def healthcheck(self) -> dict[str, Any]:
        summary = self.proxy_summary()
        result: dict[str, Any] = {
            "ok": False,
            "headless": self.headless,
            "chromium_started": self._started,
            **summary,
            "error": None,
            "captcha_detected": False,
            "blocked_detected": False,
            "final_url": None,
        }
        if not await self._resolve_proxy():
            result["error"] = "provider_not_configured"
            return result
        page = None
        try:
            await self._ensure_browser()
            page = await self.new_page()
            resp = await page.goto(
                "https://www.avito.ru/",
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout * 1000,
            )
            result["final_url"] = page.url
            text = await page.content()
            state = _detect_page_state(text, status=resp.status if resp else None)
            result["captcha_detected"] = state["captcha_detected"]
            result["blocked_detected"] = state["blocked_detected"]
            result["error"] = state["error"]
            result["ok"] = state["error"] is None
        except Exception as exc:
            err_text = str(exc).lower()
            if "407" in err_text or "proxy authentication" in err_text:
                result["error"] = "proxy_auth"
            elif "no exit node" in err_text or "unable to assign node" in err_text:
                result["error"] = "no_exit_node"
            elif "403" in err_text and "connect" in err_text:
                result["error"] = "proxy_connect_forbidden"
            elif "timeout" in err_text:
                result["error"] = "timeout"
            else:
                result["error"] = "network_error"
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
        return result

    async def search(
        self,
        city: str,
        price_min: int = 0,
        price_max: int = 99_000_000,
        query: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        started_at = time.time()
        meta = {
            "provider": "playwright",
            "transport": "browser_proxy",
            **self.proxy_summary(),
            "final_url": None,
            "captcha_detected": False,
            "blocked_detected": False,
            "raw_items_count": 0,
            "normalized_items_count": 0,
            "elapsed_ms": 0,
        }
        result = {"items": [], "error": None, "meta": meta}

        if not await self._resolve_proxy():
            if self._proxy_pool and self._proxy_pool.configured:
                result["error"] = "proxy_unavailable"
            else:
                result["error"] = "provider_not_configured"
            meta["elapsed_ms"] = int((time.time() - started_at) * 1000)
            return result

        async with self._semaphore:
            retry_once = True
            while True:
                page = None
                try:
                    if self._search_count >= self.restart_after > 0:
                        await self.restart()
                    else:
                        await self._ensure_browser()

                    page = await self.new_page()
                    url = self._build_search_url(city, price_min, price_max, query)
                    resp = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self.navigation_timeout * 1000,
                    )
                    meta["final_url"] = page.url

                    try:
                        await page.wait_for_selector('[data-marker="item"]', timeout=5000)
                    except Exception:
                        pass

                    text = await page.content()
                    state = _detect_page_state(text, status=resp.status if resp else None)
                    meta["captcha_detected"] = state["captcha_detected"]
                    meta["blocked_detected"] = state["blocked_detected"]
                    if state["error"]:
                        result["error"] = state["error"]
                        if retry_once and self._proxy_pool and _proxy_error_restartable(state["error"]):
                            retry_once = False
                            await self._restart_with_new_proxy()
                            continue
                        meta["elapsed_ms"] = int((time.time() - started_at) * 1000)
                        return result

                    raw_items = await self._extract_items(page)
                    meta["raw_items_count"] = len(raw_items)
                    items = [self._normalize_item(it) for it in raw_items if self._item_has_url(it)]
                    meta["normalized_items_count"] = len(items)
                    result["items"] = items[:limit]
                    self._search_count += 1
                    if self._proxy_pool:
                        await self._proxy_pool.mark_success(self._proxy_pool.get_current_proxy().get("port") if self._proxy_pool.get_current_proxy() else None)
                    meta["elapsed_ms"] = int((time.time() - started_at) * 1000)
                    return result
                except Exception as exc:
                    err_text = str(exc).lower()
                    if "407" in err_text or "proxy authentication" in err_text:
                        result["error"] = "proxy_auth"
                    elif "no exit node" in err_text or "unable to assign node" in err_text:
                        result["error"] = "no_exit_node"
                    elif "403" in err_text and "connect" in err_text:
                        result["error"] = "proxy_connect_forbidden"
                    elif "timeout" in err_text:
                        result["error"] = "timeout"
                    else:
                        result["error"] = "network_error"
                    if retry_once and self._proxy_pool and _proxy_error_restartable(result["error"]):
                        retry_once = False
                        await self._restart_with_new_proxy()
                        continue
                    meta["elapsed_ms"] = int((time.time() - started_at) * 1000)
                    return result
                finally:
                    if page:
                        try:
                            await page.close()
                        except Exception:
                            pass

    def _build_search_url(
        self,
        city: str,
        price_min: int,
        price_max: int,
        query: str | None,
    ) -> str:
        slug = city.lower().replace(" ", "_")
        params: dict[str, Any] = {"seller_type": "1"}
        if price_min > 0:
            params["pmin"] = price_min
        if price_max < 99_000_000:
            params["pmax"] = price_max
        if query:
            params["q"] = query
        return f"https://www.avito.ru/{slug}/avtomobili?{urlencode(params)}"

    def _item_has_url(self, item: dict) -> bool:
        return bool(item.get("url"))

    async def _extract_items(self, page: Page) -> list[dict]:
        """Извлекает сырые объявления из JSON-LD, inline JSON, data-атрибутов."""
        items: list[dict] = []
        try:
            items = await page.evaluate("""
                () => {
                    const out = [];
                    const scripts = document.querySelectorAll('script[type="application/ld+json"]');
                    scripts.forEach(s => {
                        try {
                            const data = JSON.parse(s.textContent || '{}');
                            const arr = Array.isArray(data) ? data : [data];
                            arr.forEach(d => {
                                if (d && (d['@type'] === 'Vehicle' || d['@type'] === 'Offer' || d.offers)) {
                                    out.push(d);
                                }
                            });
                        } catch (e) {}
                    });
                    return out;
                }
            """)
        except Exception:
            pass

        if not items:
            try:
                items = await page.evaluate("""
                    () => {
                        const out = [];
                        document.querySelectorAll('[data-marker="item"]').forEach(el => {
                            const a = el.querySelector('a[data-marker="item-title"], a[itemprop="url"], a[href*="/avtomobili/"]') || el.querySelector('a');
                            const title = el.querySelector('[data-marker="item-title"], [itemprop="name"]');
                            const price = el.querySelector('[data-marker="item-price"], [itemprop="price"]');
                            const img = el.querySelector('img[data-marker="item-image"], img');
                            if (a && a.href) {
                                out.push({
                                    url: a.href,
                                    title: title ? title.textContent.trim() : a.textContent.trim(),
                                    price: price ? price.textContent.trim() : '',
                                    image_url: img ? (img.src || img.dataset.src) : null
                                });
                            }
                        });
                        return out;
                    }
                """)
            except Exception:
                pass
        return items if isinstance(items, list) else []

    def _normalize_item(self, raw: dict) -> dict:
        title = self._first_text(raw, "name", "title", "model", "brand", "headline")
        if not title:
            title = str(raw.get("title", "")).strip()

        url = raw.get("url") or raw.get("offers", {}).get("url") or ""
        price_val = self._extract_price(raw)
        price_str = f"{price_val:,} ₽".replace(",", " ") if price_val else ""
        year = self._extract_year(title, raw)
        mileage = raw.get("mileage") or raw.get("mileageFromOdometer", {}).get("value")
        location = raw.get("availableAtOrFrom", {}).get("address", {}).get("addressLocality") if isinstance(raw.get("availableAtOrFrom"), dict) else raw.get("location", "")
        image_url = raw.get("image_url") or raw.get("image") or ""
        images = raw.get("images") or ([image_url] if image_url else [])
        published_at = raw.get("datePublished") or raw.get("published_at")
        description = raw.get("description") or ""
        external_id = self._extract_id(url)

        return {
            "source": "avito",
            "source_id": external_id,
            "_source_id": external_id,
            "title": title,
            "price": price_str,
            "_price_int": price_val or 0,
            "year": year,
            "_year": year,
            "mileage": mileage,
            "location": location or "",
            "url": url,
            "image_url": image_url,
            "images": images,
            "published_at": published_at,
            "date": str(published_at or _dt.date.today())[:10],
            "description": description,
            "seller": "",
            "seller_type": "private",
            "_photo_url": image_url,
            "_photos": len(images),
        }

    def _first_text(self, raw: dict, *keys: str) -> str:
        for key in keys:
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

    def _extract_price(self, raw: dict) -> int | None:
        for path in ("offers", "price", "priceSpecification"):
            val = raw.get(path)
            if isinstance(val, dict):
                price = val.get("price") or val.get("value")
                if isinstance(price, (int, float)):
                    return int(price)
                if isinstance(price, str):
                    digits = re.sub(r"\D", "", price)
                    if digits:
                        return int(digits)
        price = raw.get("price")
        if isinstance(price, (int, float)):
            return int(price)
        if isinstance(price, str):
            digits = re.sub(r"\D", "", price)
            if digits:
                return int(digits)
        return None

    def _extract_year(self, title: str, raw: dict) -> int | None:
        vehicle_date = raw.get("vehicleModelDate") or raw.get("productionDate")
        if vehicle_date:
            m = re.search(r"\b(19\d{2}|20\d{2})\b", str(vehicle_date))
            if m:
                return int(m.group(1))
        m = re.search(r"\b(19\d{2}|20\d{2})\b", title)
        if m:
            return int(m.group(1))
        return None

    def _extract_id(self, url: str) -> str:
        if not url:
            return ""
        m = re.search(r"_?(\d+)$", url)
        if m:
            return m.group(1)
        return ""


# ──────────────────────────────────────────────────────────────────────
# Удобная функция поиска (singleton manager)
# ──────────────────────────────────────────────────────────────────────
_avito_manager: AvitoBrowserManager | None = None
_manager_lock = asyncio.Lock()


async def get_avito_manager() -> AvitoBrowserManager:
    global _avito_manager
    if _avito_manager is None:
        async with _manager_lock:
            if _avito_manager is None:
                _avito_manager = AvitoBrowserManager()
    return _avito_manager


async def search_avito(
    city: str,
    price_min: int = 0,
    price_max: int = 99_000_000,
    query: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """Поиск Авито через серверный Playwright."""
    manager = await get_avito_manager()
    return await manager.search(city, price_min, price_max, query, limit)


async def stop_avito_manager() -> None:
    global _avito_manager
    if _avito_manager is not None:
        await _avito_manager.stop()
        _avito_manager = None
