"""
Playwright + Stealth парсер Авито.
Используется как резервный метод, когда HTTP/cloudscraper получает 429/439.
"""
import asyncio
import json
import os
import random
import re
from typing import Optional

from bs4 import BeautifulSoup


def _deep_get(obj, path: str, default=None):
    """Безопасно достаёт вложенный ключ по точечному пути."""
    if not isinstance(obj, dict):
        return default
    keys = path.split(".")
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def _find_items_recursive(obj, found=None):
    """Рекурсивно ищет в JSON структуры похожие на список объявлений."""
    if found is None:
        found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("items", "catalog", "result", "data") and isinstance(v, list) and v and isinstance(v[0], dict):
                # Проверяем, что элементы похожи на объявления Авито
                if any(x in v[0] for x in ("urlPath", "title", "priceDetailed", "id", "category")):
                    found.append(v)
            elif isinstance(v, (dict, list)):
                _find_items_recursive(v, found)
    elif isinstance(obj, list):
        for item in obj:
            _find_items_recursive(item, found)
    return found


def _extract_images_map(initial_data: dict) -> dict:
    """Ищет карту изображений itemsImages."""
    for path in (
        "initialState.catalog.itemsImages",
        "initialData.catalog.itemsImages",
        "catalog.itemsImages",
        "itemsImages",
        "pageProps.initialState.catalog.itemsImages",
        "pageProps.catalog.itemsImages",
        "props.initialState.catalog.itemsImages",
    ):
        val = _deep_get(initial_data, path)
        if isinstance(val, dict) and val:
            return val
    return {}


def _parse_time_days(time_info) -> int:
    """Переводит поле time Авито в _days_on_site."""
    if not isinstance(time_info, dict):
        return 0
    seconds = time_info.get("seconds", 0)
    if isinstance(seconds, (int, float)) and seconds > 0:
        return int(seconds / 86400)
    # fallback: ищем число дней в строке
    txt = str(time_info.get("title", "")).lower()
    m = re.search(r"(\d+)\s*(д|день|дня|дней)", txt)
    if m:
        return int(m.group(1))
    if "сегодня" in txt or "вчера" in txt or "час" in txt or "мин" in txt:
        return 0
    return 0


def _normalize_price(price_info) -> tuple[str, int]:
    """Возвращает (price_str, price_int) из priceDetailed/price."""
    if isinstance(price_info, dict):
        price_str = price_info.get("value", "") or price_info.get("fullName", "") or price_info.get("price", "")
    else:
        price_str = str(price_info)
    digits = re.sub(r"\D", "", str(price_str))
    return price_str, int(digits) if digits else 0


def _extract_year(parameters: list) -> int:
    if not isinstance(parameters, list):
        return 0
    for param in parameters:
        if not isinstance(param, dict):
            continue
        label = str(param.get("label", "")).lower()
        if "год" in label:
            val = param.get("value", "")
            m = re.search(r"(19\d{2}|20\d{2})", str(val))
            if m:
                return int(m.group(1))
    return 0


def _build_search_url(region: str, page: int, price_min: int, price_max: int,
                       sort_by_date: bool, brand: str) -> str:
    slug = region
    qs = ["seller_type=1"]
    if page > 1:
        qs.append(f"p={page}")
    if price_min > 0:
        qs.append(f"pmin={price_min}")
    if price_max < 99_000_000:
        qs.append(f"pmax={price_max}")
    if sort_by_date:
        qs.append("s=104")
    if brand:
        qs.append(f"q={brand}")
    return f"https://www.avito.ru/{slug}/avtomobili?" + "&".join(qs)


def _parse_item(raw: dict, images_map: dict) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    url_path = raw.get("urlPath") or raw.get("url") or ""
    if not url_path:
        return None
    if not url_path.startswith("http"):
        url = "https://www.avito.ru" + url_path
    else:
        url = url_path

    title = raw.get("title", "")
    if not title:
        # fallback: собираем из марки/модели/года
        parts = []
        for k in ("category",):
            cat = raw.get(k)
            if isinstance(cat, dict):
                name = cat.get("name")
                if name:
                    parts.append(name)
        title = " ".join(parts)
    if not title:
        return None

    price_info = raw.get("priceDetailed") or raw.get("price") or {}
    price_str, price_int = _normalize_price(price_info)

    year = _extract_year(raw.get("parameters", []))
    if not year and "title" in raw:
        m = re.search(r"\b(19\d{2}|20\d{2})\b", raw["title"])
        if m:
            year = int(m.group(1))

    days = _parse_time_days(raw.get("time"))

    item_id = str(raw.get("id", ""))
    photo_url = ""
    if item_id and isinstance(images_map, dict):
        imgs = images_map.get(item_id) or images_map.get(int(item_id))
        if imgs and isinstance(imgs, list) and imgs:
            first = imgs[0]
            if isinstance(first, dict):
                for size in ("1200x900", "1280x960", "640x480", "320x240", "url"):
                    if size in first and first[size]:
                        photo_url = first[size]
                        break
            elif isinstance(first, str):
                photo_url = first

    return {
        "title": title.strip(),
        "price": price_str,
        "url": url,
        "year": year,
        "_price_int": price_int,
        "_days_on_site": days,
        "_year": year,
        "source": "avito",
        "_photo_url": photo_url,
    }


def _parse_next_data_items(text: str) -> list[dict]:
    """Парсит __NEXT_DATA__ / __initialData__ из HTML."""
    items = []
    images_map = {}

    # __NEXT_DATA__
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        text, re.S,
    )
    if m:
        try:
            nd = json.loads(m.group(1))
        except Exception:
            nd = {}
        if nd:
            candidates = []
            for path in (
                "props.initialState.catalog.items",
                "props.pageProps.initialState.catalog.items",
                "initialState.catalog.items",
                "props.initialData.catalog.items",
                "props.pageProps.initialData.catalog.items",
                "props.pageProps.data.items",
                "props.pageProps.items",
                "props.pageProps.catalog.items",
                "props.pageProps.initialState.search.items",
                "props.pageProps.initialState.catalog.catalog.items",
            ):
                val = _deep_get(nd, path)
                if isinstance(val, list) and val:
                    candidates.append(val)
            if not candidates:
                candidates = _find_items_recursive(nd)
            if candidates:
                # Берём самый длинный список
                items = max(candidates, key=len)
            images_map = _extract_images_map(nd)

    # __initialData__
    if not items:
        m2 = re.search(
            r'<script[^>]*>window\.__initialData__\s*=\s*({.*?});?</script>',
            text, re.S,
        )
        if not m2:
            m2 = re.search(
                r'window\.__initialData__\s*=\s*({.*?});',
                text, re.S,
            )
        if m2:
            try:
                data = json.loads(m2.group(1))
            except Exception:
                data = {}
            if data:
                candidates = []
                for path in ("catalog.items", "items", "data.items"):
                    val = _deep_get(data, path)
                    if isinstance(val, list) and val:
                        candidates.append(val)
                if not candidates:
                    candidates = _find_items_recursive(data)
                if candidates:
                    items = max(candidates, key=len)
                if not images_map:
                    images_map = _extract_images_map(data)

    return [_parse_item(it, images_map) for it in items if _parse_item(it, images_map)]


def _parse_html_cards(text: str) -> list[dict]:
    """Запасной парсинг карточек через CSS-селекторы."""
    soup = BeautifulSoup(text, "lxml")
    results = []
    for card in soup.select('[data-marker="item"]'):
        try:
            a = card.select_one('a[data-marker="item-title"]') or card.find("a")
            if not a or not a.get("href"):
                continue
            href = a.get("href")
            url = href if href.startswith("http") else "https://www.avito.ru" + href
            title = a.get_text(strip=True)

            price_el = card.select_one('[itemprop="price"]') or card.select_one('[data-marker="item-price"]')
            price_str = price_el.get_text(strip=True) if price_el else ""
            price_int = int(re.sub(r"\D", "", price_str)) if price_str else 0

            year = 0
            title_year = re.search(r"\b(19\d{2}|20\d{2})\b", title)
            if title_year:
                year = int(title_year.group(1))

            img = card.select_one("img")
            photo_url = img.get("src") or img.get("data-src") if img else ""

            results.append({
                "title": title,
                "price": price_str,
                "url": url,
                "year": year,
                "_price_int": price_int,
                "_days_on_site": 0,
                "_year": year,
                "source": "avito",
                "_photo_url": photo_url,
            })
        except Exception:
            continue
    return results


async def _scrape_page(page, url: str) -> list[dict]:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(random.uniform(1.0, 2.0))
        # Прокрутка вниз для подгрузки карточек
        await page.evaluate("""() => {
            window.scrollTo(0, document.body.scrollHeight / 2);
        }""")
        await asyncio.sleep(random.uniform(0.5, 1.0))
        html = await page.content()
        items = _parse_next_data_items(html)
        if not items:
            items = _parse_html_cards(html)
        return items
    except Exception as e:
        print(f"  [pw-avito] ошибка загрузки {url}: {e}")
        return []


async def scrape_avito_playwright(
    region: str,
    pages: int = 3,
    price_min: int = 0,
    price_max: int = 99_000_000,
    sort_by_date: bool = False,
    brand: str = "",
    proxy_url: Optional[str] = None,
) -> list[dict]:
    """
    Парсит Авито через Playwright + stealth.
    Возвращает список объявлений в формате, совместимом с control_bot.py.
    """
    proxy_url = proxy_url or os.getenv("PROXY_URL") or os.getenv("AVITO_PROXY_URL") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")

    try:
        from playwright.async_api import async_playwright
        from playwright_stealth import stealth_async
    except ImportError as e:
        print(f"  [pw-avito] playwright/playwright_stealth не установлены: {e}")
        return []

    results = []
    seen_urls = set()

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    proxy_config = None
    if proxy_url:
        proxy_config = {"server": proxy_url}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--lang=ru-RU",
            ],
            proxy=proxy_config,
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=user_agent,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            permissions=["geolocation"],
        )
        page = await context.new_page()
        try:
            await stealth_async(page)
        except Exception as _e:
            print(f"  [pw-avito] stealth_async ошибка: {_e}")

        try:
            # Прогрев: главная страница Авито
            await page.goto("https://www.avito.ru/", wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(random.uniform(1.0, 2.0))
        except Exception as _e:
            print(f"  [pw-avito] прогрев главной: {_e}")

        for page_num in range(1, pages + 1):
            url = _build_search_url(region, page_num, price_min, price_max, sort_by_date, brand)
            print(f"  [pw-avito] загружаем {url}")
            batch = await _scrape_page(page, url)
            if not batch:
                break
            for it in batch:
                if it["url"] not in seen_urls:
                    seen_urls.add(it["url"])
                    results.append(it)
            print(f"  [pw-avito] страница {page_num}: {len(batch)} новых, всего {len(results)}")
            await asyncio.sleep(random.uniform(1.0, 2.5))

        await context.close()
        await browser.close()

    print(f"  [pw-avito] итого: {len(results)} объявлений")
    return results


if __name__ == "__main__":
    # Простой тест
    items = asyncio.run(scrape_avito_playwright("ekaterinburg", pages=1, price_max=100_000))
    print(f"test: {len(items)} items")
    for it in items[:3]:
        print(it)
