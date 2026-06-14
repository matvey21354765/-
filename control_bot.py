"""
Авто-брокер бот — публичная версия.
Каждый пользователь выбирает регион и бюджет, бот ищет частников ниже рынка.
"""

import asyncio
import json
import logging
import random
import re
import time
import datetime
import subprocess
from pathlib import Path
import os

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ── Токен ───────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8923014188:AAHvNW2B5fin2XCmbVhlaLNjWhLwI3JhZ90")
SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY", "b317ae63b4d847805e2f91a1dc073b40")

# ── Регионы ─────────────────────────────────────────────────────
REGIONS = {
    "ekaterinburg": "Екатеринбург",
    "moscow":       "Москва",
    "spb":          "Санкт-Петербург",
    "novosibirsk":  "Новосибирск",
    "kazan":        "Казань",
    "chelyabinsk":  "Челябинск",
    "ufa":          "Уфа",
    "krasnodar":    "Краснодар",
    "omsk":         "Омск",
    "tyumen":       "Тюмень",
    "perm":         "Пермь",
    "krasnoyarsk":  "Красноярск",
    "voronezh":     "Воронеж",
    "samara":       "Самара",
    "rostov":       "Ростов-на-Дону",
}

# Слаги для Auto.ru — используем область целиком, не только город
AUTORU_SLUGS = {
    "ekaterinburg": "sverdlovskaya_oblast",
    "moscow":       "moskva",
    "spb":          "sankt-peterburg",
    "novosibirsk":  "novosibirskaya_oblast",
    "kazan":        "tatarstan",
    "chelyabinsk":  "chelyabinskaya_oblast",
    "ufa":          "bashkortostan",
    "krasnodar":    "krasnodarskiy_kray",
    "omsk":         "omskaya_oblast",
    "tyumen":       "tyumenskaya_oblast",
    "perm":         "permskiy_kray",
    "krasnoyarsk":  "krasnoyarskiy_kray",
    "voronezh":     "voronezhskaya_oblast",
    "samara":       "samarskaya_oblast",
    "rostov":       "rostovskaya_oblast",
}

# Слаги для Авито — область целиком
AVITO_REGION_SLUGS = {
    "ekaterinburg": "sverdlovskaya_oblast",
    "moscow":       "moskva",
    "spb":          "sankt-peterburg",
    "novosibirsk":  "novosibirskaya_oblast",
    "kazan":        "tatarstan",
    "chelyabinsk":  "chelyabinskaya_oblast",
    "ufa":          "bashkortostan",
    "krasnodar":    "krasnodarskiy_kray",
    "omsk":         "omskaya_oblast",
    "tyumen":       "tyumenskaya_oblast",
    "perm":         "permskiy_kray",
    "krasnoyarsk":  "krasnoyarskiy_kray",
    "voronezh":     "voronezhskaya_oblast",
    "samara":       "samarskaya_oblast",
    "rostov":       "rostovskaya_oblast",
}

# Слаги для Дрома — область (geo-параметр)
DROM_GEO = {
    "ekaterinburg": 12,    # Свердловская область
    "moscow":       1,     # Москва и МО
    "spb":          2,     # СПб и ЛО
    "novosibirsk":  15,    # Новосибирская обл.
    "kazan":        23,    # Татарстан
    "chelyabinsk":  13,    # Челябинская обл.
    "ufa":          3,     # Башкортостан
    "krasnodar":    18,    # Краснодарский кр.
    "omsk":         16,    # Омская обл.
    "tyumen":       27,    # Тюменская обл.
    "perm":         8,     # Пермский кр.
    "krasnoyarsk":  24,    # Красноярский кр.
    "voronezh":     36,    # Воронежская обл.
    "samara":       26,    # Самарская обл.
    "rostov":       20,    # Ростовская обл.
}

# ── Дилерские признаки ──────────────────────────────────────────
DEALER_KEYWORDS = [
    "ооо", "зао", "пао", "ип ", "официальный дилер", "дилер",
    "автосалон", "автоцентр", "автохолдинг", "автогруп", "автогрупп",
    "trade-in", "трейд-ин", "trade in",
    "рольф", "major", "колёса даром", "автопланета", "автоград",
    "июль авто", "автоленд", "favorit", "фаворит", "авто плюс", "автоплюс",
    "fresh auto", "автобан", "автосфера", "арконт", "ключавто", "авилон",
    "петровский", "прагматика", "бизнес кар", "максимум авто", "мотус",
    "genser", "генсер", "ац урал", "восток авто", "сити авто",
    "кредит от", "автоподбор", "выкуп авто", "автовыкуп",
    "срочный выкуп", "выкупаем", "лизинг", "рассрочка от",
    "наш автосалон", "купить в кредит", "тест-драйв",
    "гарантия завода", "официальная гарантия",
    "автомагазин", "автодилер", "car dealer", "автошоу", "автовыставка",
    "новый автомобиль", "новый авто", "в наличии и под заказ",
    "звоните", "звони", "менеджер", "отдел продаж", "автосупермаркет",
]

HOT_WORDS = re.compile(
    r"(срочно|торг|уступлю|снижу|скидка|дёшево|дешево|продам быстро|срочная продажа"
    r"|срочно продам|срочно продаю|нужны деньги|уезжаю|переезжаю|не торгуюсь нет"
    r"|ниже рынка|ниже рыночной|выгодно|хорошая цена|торг при осмотре)",
    re.IGNORECASE,
)

MONTHS = {
    "янв":1,"фев":2,"мар":3,"апр":4,"май":5,"мая":5,
    "июн":6,"июл":7,"авг":8,"сен":9,"окт":10,"ноя":11,"дек":12,
}

# ── Пользователи ────────────────────────────────────────────────
USERS_DIR = Path("users")
USERS_DIR.mkdir(exist_ok=True)

# Мониторинг новых объявлений
MONITOR_INTERVAL = 15 * 60   # проверять каждые 5 минут
MONITOR_MIN_SAVINGS_PCT = 10  # показывать только если скидка от рынка ≥ 10%
_monitor_tasks: dict[int, asyncio.Task] = {}   # uid → Task


def user_dir(uid: int) -> Path:
    d = USERS_DIR / str(uid)
    d.mkdir(exist_ok=True)
    return d


def load_settings(uid: int) -> dict:
    f = user_dir(uid) / "settings.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(uid: int, s: dict):
    f = user_dir(uid) / "settings.json"
    f.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def load_seen(uid: int) -> set:
    f = user_dir(uid) / "seen.json"
    if f.exists():
        try:
            return set(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def save_seen(uid: int, seen: set):
    f = user_dir(uid) / "seen.json"
    f.write_text(json.dumps(list(seen), ensure_ascii=False), encoding="utf-8")


def load_skipped(uid: int) -> set:
    f = user_dir(uid) / "skipped.json"
    if f.exists():
        try:
            return set(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def save_skipped(uid: int, skipped: set):
    f = user_dir(uid) / "skipped.json"
    f.write_text(json.dumps(list(skipped), ensure_ascii=False), encoding="utf-8")


# ── Фильтры ─────────────────────────────────────────────────────

def parse_price(s: str) -> int | None:
    digits = re.sub(r"[^\d]", "", str(s or ""))
    return int(digits) if digits else None


def is_dealer(item: dict) -> bool:
    text = (
        item.get("title", "") + " " +
        item.get("description", "") + " " +
        item.get("seller", "")
    ).lower()
    return any(k in text for k in DEALER_KEYWORDS)


def in_price_range(item: dict, price_min: int, price_max: int) -> bool:
    p = item.get("_price_int") or parse_price(item.get("price", ""))
    if not p:
        return True  # цена неизвестна — pmin/pmax в URL уже отфильтровали
    return price_min <= p <= price_max


def hot_score(item: dict) -> float:
    """Базовая оценка: срочность продажи + давность объявления."""
    title = item.get("title", "") + " " + item.get("description", "")
    days = item.get("_days_on_site", 0)
    score = 0.0
    if HOT_WORDS.search(title):
        score += 20.0
    score += min(days, 30) * 0.3
    return round(score, 2)


def _car_group_key(title: str) -> str:
    """Извлекает марку+модель+год для группировки (напр. 'toyota camry 2018')."""
    t = title.lower()
    # Убираем технические характеристики: 1.6 МТ, 156 000 км и т.п.
    t = re.sub(r'\d+[\.,]\d+\s*(л|at|mt|акп|мкп|амт)', '', t)
    t = re.sub(r'\d[\d\s]+км', '', t)
    # Год
    year_m = re.search(r'\b(20\d{2}|19\d{2})\b', t)
    year = year_m.group(1) if year_m else ""
    # Марка+модель — первые 2 слова
    words = re.sub(r'[^а-яёa-z\s]', ' ', t).split()
    brand_model = " ".join(words[:2]) if len(words) >= 2 else " ".join(words)
    return f"{brand_model} {year}".strip()


def rank_by_market_price(items: list[dict]) -> list[dict]:
    """
    Вычисляет рыночную цену по медиане внутри группы марка+модель+год (по всем площадкам).
    Устанавливает _savings_pct: сколько % ниже рынка. Чем больше — тем выгоднее.
    """
    from statistics import median

    groups: dict[str, list[int]] = {}
    for it in items:
        p = it.get("_price_int", 0)
        if p > 0:
            key = _car_group_key(it.get("title", ""))
            groups.setdefault(key, []).append(p)

    market: dict[str, float] = {k: median(v) for k, v in groups.items() if len(v) >= 2}

    for it in items:
        p = it.get("_price_int", 0)
        if p > 0:
            key = _car_group_key(it.get("title", ""))
            med = market.get(key, 0)
            if med > 0:
                savings_pct = round((1 - p / med) * 100, 1)  # положительный = ниже рынка
                it["_savings_pct"] = savings_pct
                it["_market_price"] = int(med)
                if savings_pct >= 25:
                    it["_hot_score"] = round(it.get("_hot_score", 0) + 50, 2)
                    it["_below_market"] = True
                elif savings_pct >= 10:
                    it["_hot_score"] = round(it.get("_hot_score", 0) + 25, 2)
                    it["_below_market"] = True
                elif savings_pct > 0:
                    it["_hot_score"] = round(it.get("_hot_score", 0) + 10, 2)

    return items


# ── Парсер Дрома ────────────────────────────────────────────────

def parse_ru_date(text: str):
    if not text:
        return None
    text = text.strip()
    today = datetime.date.today()
    low = text.lower()
    if "сегодня" in low: return today
    if "вчера" in low: return today - datetime.timedelta(days=1)
    m = re.search(r"(\d+)\s+дн", low)
    if m: return today - datetime.timedelta(days=int(m.group(1)))
    if re.search(r"\d+\s+(час|мин)", low): return today
    m = re.search(r"(\d{1,2})\s+([а-яё]+)", text, re.IGNORECASE)
    if m:
        day, mon_str = int(m.group(1)), MONTHS.get(m.group(2)[:3].lower())
        if mon_str:
            try: return datetime.date(today.year, mon_str, day)
            except ValueError: pass
    return None


def scrape_drom(region: str, pages: int = 15, price_min: int = 0, price_max: int = 99_000_000) -> list[dict]:
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
        import cloudscraper as _cs
        session = _cs.create_scraper(browser={"browser": "chrome", "platform": "windows"})
    except ImportError:
        try:
            import requests as _req
            from bs4 import BeautifulSoup as _BS
            session = _req.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
        except ImportError:
            return []

    results = []
    today = datetime.date.today()

    # Используем субдомен города — Дром автоматически показывает всю область
    base = f"https://{region}.drom.ru"

    for p in range(1, pages + 1):
        url = f"{base}/auto/all/" if p == 1 else f"{base}/auto/all/page{p}/"
        params = {}
        if price_min > 0:
            params["minprice"] = price_min
        if price_max < 99_000_000:
            params["maxprice"] = price_max

        try:
            r = session.get(url, params=params, timeout=20)
            soup = _BS(r.text, "lxml")
            cards = soup.select("div[data-ftid='bulls-list_bull']")
            if not cards:
                break

            for card in cards:
                try:
                    link = card.select_one("a[data-ftid='bull_title']") or card.select_one("h3 a")
                    title = link.get_text(strip=True) if link else ""
                    href = link.get("href", "") if link else ""
                    item_url = href if href.startswith("http") else (base + href)

                    price_el = card.select_one("span[data-ftid='bull_price']")
                    price = price_el.get_text(strip=True) if price_el else ""

                    seller_el = (
                        card.select_one("[data-ftid='bull_seller']")
                        or card.select_one("a[class*='seller']")
                        or card.select_one("span[class*='seller']")
                    )
                    seller = seller_el.get_text(strip=True) if seller_el else ""

                    desc_el = card.select_one("[data-ftid='bull_description']")
                    desc = desc_el.get_text(strip=True) if desc_el else ""

                    date_el = (
                        card.select_one("[data-ftid='bull_date']")
                        or card.select_one("time")
                    )
                    date_text = date_el.get_text(strip=True) if date_el else ""
                    date = parse_ru_date(date_text)
                    days = max(0, (today - date).days) if date else 0

                    photo_el = card.select_one("span[data-ftid='bull_images-count']")
                    photos_str = photo_el.get_text() if photo_el else ""
                    pm = re.search(r"\d+", photos_str)
                    photos = int(pm.group()) if pm else 0

                    photo_url = ""
                    for img_el in card.find_all("img"):
                        src = (img_el.get("data-src") or img_el.get("data-lazy-src") or
                               img_el.get("data-original") or img_el.get("src") or "")
                        if src and src.startswith("http") and len(src) > 20:
                            photo_url = src
                            break
                    # Также ищем в data-атрибутах карточки (Drom хранит фото в JSON)
                    if not photo_url:
                        card_str = str(card)
                        img_m = re.search(r'https?://[^"\']+(?:static|photo)[^"\']+\.(?:jpg|jpeg|webp)', card_str)
                        if img_m:
                            photo_url = img_m.group(0)

                    if title and item_url:
                        price_int = parse_price(price) or 0
                        item = {
                            "source": "drom",
                            "title": title,
                            "price": price,
                            "url": item_url,
                            "date": str(date) if date else date_text,
                            "_photos": photos,
                            "_days_on_site": days,
                            "description": desc,
                            "seller": seller,
                            "_photo_url": photo_url,
                            "_price_int": price_int,
                        }
                        item["_hot_score"] = hot_score(item)
                        results.append(item)
                except Exception:
                    pass

            time.sleep(0.2)
        except Exception as e:
            print(f"  [Дром {region}] стр.{p}: {e}")
            break

    return results


# ── Auto.ru geo IDs для API ──────────────────────────────────────
AUTORU_GEO_IDS = {
    "ekaterinburg": [56],    # Свердловская обл.
    "moscow":       [1],     # Москва
    "spb":          [10174], # Санкт-Петербург
    "novosibirsk":  [65],    # Новосибирская обл.
    "kazan":        [11119], # Татарстан
    "chelyabinsk":  [56088], # Челябинская обл.
    "ufa":          [102],   # Башкортостан
    "krasnodar":    [35],    # Краснодарский кр.
    "omsk":         [66],    # Омская обл.
    "tyumen":       [61],    # Тюменская обл.
    "perm":         [51],    # Пермский кр.
    "krasnoyarsk":  [54],    # Красноярский кр.
    "voronezh":     [193],   # Воронежская обл.
    "samara":       [11162], # Самарская обл.
    "rostov":       [39],    # Ростовская обл.
}

# ── Парсер Auto.ru ──────────────────────────────────────────────

def _autoru_parse_offers(data: dict, today) -> list[dict]:
    """Парсит список объявлений из JSON Auto.ru."""
    results = []
    listing = (
        data.get("listing", {}).get("data", {}).get("offers", [])
        or data.get("search", {}).get("offers", {}).get("offers", [])
        or data.get("offers", [])
    )
    for offer in listing:
        try:
            vehicle = offer.get("vehicle_info", {})
            mark = vehicle.get("mark_info", {}).get("name", "")
            model = vehicle.get("model_info", {}).get("name", "")
            year = offer.get("documents", {}).get("year", "")
            title = f"{mark} {model} {year}".strip()
            price_val = offer.get("price_info", {}).get("price", "")
            price_str = f"{int(price_val):,} ₽".replace(",", " ") if price_val else ""
            item_url = offer.get("url", "") or f"https://auto.ru/cars/used/sale/{offer.get('id', '')}"
            if offer.get("seller_type") == "COMMERCIAL":
                continue
            photos_list = offer.get("photos", [])
            photo_url = ""
            if photos_list:
                sizes = photos_list[0].get("sizes", {})
                photo_url = sizes.get("1200x900") or sizes.get("832x624") or sizes.get("456x342") or ""
            days = 0
            date_str = offer.get("additional_info", {}).get("creation_date", "")
            if date_str:
                try:
                    dt = datetime.datetime.fromisoformat(date_str[:10]).date()
                    days = max(0, (today - dt).days)
                except Exception:
                    pass
            desc = offer.get("description", "")[:300]
            tech = vehicle.get("tech_param", {})
            if tech and not desc:
                parts = [x for x in [tech.get("engine_type",""), f"{tech.get('power','')} л.с." if tech.get("power") else "", tech.get("transmission","")] if x]
                desc = ", ".join(parts)
            if title and item_url:
                price_int = int(price_val) if price_val else 0
                item = {
                    "source": "autoru", "title": title, "price": price_str,
                    "url": item_url, "date": str(today - datetime.timedelta(days=days)),
                    "_photos": len(photos_list), "_days_on_site": days,
                    "description": desc, "seller": "", "_photo_url": photo_url,
                    "_price_int": price_int,
                }
                item["_hot_score"] = hot_score(item)
                results.append(item)
        except Exception:
            pass
    return results


def _autoru_parse_html(text: str, today) -> list[dict]:
    """Извлекает объявления из HTML Auto.ru (__INITIAL_STATE__ или regex)."""
    results = []

    # Метод 1: window.__INITIAL_STATE__
    for marker in ("window.__INITIAL_STATE__=", "window.__INITIAL_STATE__ ="):
        idx = text.find(marker)
        if idx == -1:
            continue
        brace_start = text.find("{", idx)
        if brace_start == -1:
            continue
        script_end = text.find("</script>", brace_start)
        json_str = text[brace_start:script_end].rstrip("; \n\r") if script_end != -1 else text[brace_start:brace_start + 800_000]
        try:
            data = json.loads(json_str)
            found = _autoru_parse_offers(data, today)
            if found:
                print(f"  [Auto.ru] __INITIAL_STATE__: {len(found)} объявлений")
                return found
        except Exception as e:
            print(f"  [Auto.ru] __INITIAL_STATE__ json error: {e}")

    # Метод 2: regex по паттернам Auto.ru в сыром HTML/JSON
    # Auto.ru URLs: https://auto.ru/cars/used/sale/brand/model/id/
    seen_urls: set[str] = set()
    for m in re.finditer(
        r'"url"\s*:\s*"(https://auto\.ru/cars/[^"]{10,120})"'
        r'.*?"price"\s*:\s*(\d{4,9})',
        text, re.DOTALL
    ):
        item_url = m.group(1)
        price_val = int(m.group(2))
        if item_url in seen_urls or price_val < 10_000:
            continue
        seen_urls.add(item_url)
        # Ищем марку/модель/год в блоке вокруг этого матча
        ctx_start = max(0, m.start() - 800)
        ctx = text[ctx_start:m.end()]
        mark_m = re.search(r'"mark_info"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"', ctx)
        model_m = re.search(r'"model_info"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"', ctx)
        year_m = re.search(r'"year"\s*:\s*(\d{4})', ctx)
        mark = mark_m.group(1) if mark_m else ""
        model = model_m.group(1) if model_m else ""
        year = year_m.group(1) if year_m else ""
        title = f"{mark} {model} {year}".strip() or "Авто на Auto.ru"
        price_str = f"{price_val:,} ₽".replace(",", " ")
        photo_m = re.search(r'"1200x900"\s*:\s*"([^"]+)"', ctx)
        photo_url = photo_m.group(1).replace("\\/", "/") if photo_m else ""
        item = {
            "source": "autoru", "title": title, "price": price_str,
            "url": item_url, "date": str(today),
            "_photos": 1 if photo_url else 0, "_days_on_site": 0,
            "description": "", "seller": "", "_photo_url": photo_url,
            "_price_int": price_val,
        }
        item["_hot_score"] = hot_score(item)
        results.append(item)

    if results:
        print(f"  [Auto.ru] regex HTML: {len(results)} объявлений")
    return results


def scrape_autoru(region: str, pages: int = 5, price_min: int = 0, price_max: int = 99_000_000) -> list[dict]:
    slug = AUTORU_SLUGS.get(region, region)
    geo_ids = AUTORU_GEO_IDS.get(region, [])
    try:
        import requests as _req
    except ImportError:
        return []

    results = []
    today = datetime.date.today()

    # Метод 1: AJAX API Auto.ru (наиболее надёжный, возвращает JSON)
    headers_ajax = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json,*/*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://auto.ru",
        "Referer": f"https://auto.ru/{slug}/cars/used/",
        "x-client-app": "autoru-frontend-application",
        "x-page-request-id": "ajax",
    }

    for p in range(1, pages + 1):
        body: dict = {
            "category": "cars", "section": "used",
            "seller_type": ["PRIVATE"], "page": p, "page_size": 37,
            "sort": "fresh_relevance_1-desc",
            "output_type": "list",
        }
        if geo_ids:
            body["geo_id"] = geo_ids
        if price_min > 0:
            body["price_from"] = price_min
        if price_max < 99_000_000:
            body["price_to"] = price_max

        batch = []
        html_url = f"https://auto.ru/{slug}/cars/used/?seller_group=PRIVATE&page={p}&sort=fresh_relevance_1-desc"
        if price_min > 0:
            html_url += f"&price_from={price_min}"
        if price_max < 99_000_000:
            html_url += f"&price_to={price_max}"

        # Метод 1: ScraperAPI render=true — JS выполняется, __INITIAL_STATE__ заполняется
        if not batch and SCRAPER_API_KEY:
            try:
                r3 = _req.get("http://api.scraperapi.com", params={
                    "api_key": SCRAPER_API_KEY, "url": html_url,
                    "country_code": "ru", "render": "true", "wait": "1500",
                }, timeout=40)
                print(f"  [Auto.ru] ScraperAPI render стр.{p}: HTTP {r3.status_code}, {len(r3.text):,}б")
                if r3.status_code == 200 and len(r3.text) > 100_000:
                    batch = _autoru_parse_html(r3.text, today)
            except Exception as e:
                print(f"  [Auto.ru] ScraperAPI render: {e}")

        # Метод 2: ScraperAPI без render (быстрее)
        if not batch and SCRAPER_API_KEY:
            try:
                r4 = _req.get("http://api.scraperapi.com", params={
                    "api_key": SCRAPER_API_KEY, "url": html_url,
                    "country_code": "ru", "premium": "true",
                }, timeout=60)
                print(f"  [Auto.ru] ScraperAPI HTML стр.{p}: HTTP {r4.status_code}, {len(r4.text):,}б")
                if r4.status_code == 200 and len(r4.text) > 50_000:
                    batch = _autoru_parse_html(r4.text, today)
            except Exception as e:
                print(f"  [Auto.ru] ScraperAPI HTML: {e}")

        # Метод 3: ScraperAPI → AJAX POST
        if not batch and SCRAPER_API_KEY:
            try:
                r = _req.post(
                    "http://api.scraperapi.com/",
                    params={"api_key": SCRAPER_API_KEY, "url": "https://auto.ru/-/ajax/desktop/listing/", "country_code": "ru"},
                    data=json.dumps(body), headers={"Content-Type": "application/json"}, timeout=40
                )
                print(f"  [Auto.ru] ScraperAPI AJAX стр.{p}: HTTP {r.status_code}, {len(r.text):,}б")
                if r.status_code == 200:
                    try:
                        batch = _autoru_parse_offers(r.json(), today)
                    except Exception:
                        batch = _autoru_parse_html(r.text, today)
            except Exception as e:
                print(f"  [Auto.ru] ScraperAPI AJAX: {e}")

        print(f"  [Auto.ru] стр.{p}: итого {len(batch)} объявлений")
        if not batch:
            break
        results.extend(batch)
        time.sleep(0.2)

    print(f"  [Auto.ru] итого {len(results)} объявлений")
    return results


# ── Парсер Kolesa.ru ────────────────────────────────────────────

KOLESA_SLUGS = {
    "ekaterinburg": "ekaterinburg",
    "moscow":       "moskva",
    "spb":          "sankt-peterburg",
    "novosibirsk":  "novosibirsk",
    "kazan":        "kazan",
    "chelyabinsk":  "chelyabinsk",
    "ufa":          "ufa",
    "krasnodar":    "krasnodar",
    "omsk":         "omsk",
    "tyumen":       "tyumen",
    "perm":         "perm",
    "krasnoyarsk":  "krasnoyarsk",
    "voronezh":     "voronezh",
    "samara":       "samara",
    "rostov":       "rostov-na-donu",
}


def scrape_kolesa(region: str, pages: int = 5, price_min: int = 0, price_max: int = 99_000_000) -> list[dict]:
    slug = KOLESA_SLUGS.get(region, region)
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
    except ImportError:
        return []

    results = []
    today = datetime.date.today()
    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*",
    })

    for p in range(1, pages + 1):
        # Kolesa.ru: частники = seller=1, регион через city[]
        url = f"https://kolesa.ru/cars/"
        params: dict = {"city[]": slug, "seller": "1", "page": p}
        if price_min > 0:
            params["price[from]"] = price_min
        if price_max < 99_000_000:
            params["price[to]"] = price_max

        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code != 200:
                break
            soup = _BS(r.text, "lxml")

            # Kolesa использует article.a-card или div.a-list__item
            cards = (
                soup.select("article.a-card")
                or soup.select("div.a-list__item")
                or soup.select("div[class*='listing-item']")
                or soup.select("li[data-id]")
            )
            if not cards:
                print(f"  [Kolesa {region}] стр.{p}: нет карточек (HTTP {r.status_code}, {len(r.text)} байт)")
                break

            for card in cards:
                try:
                    link = (
                        card.select_one("a.a-card__title")
                        or card.select_one("a[class*='title']")
                        or card.select_one("h5 a")
                        or card.select_one("h2 a")
                        or card.select_one("a[href*='/cars/']")
                    )
                    title = link.get_text(strip=True) if link else ""
                    href = link.get("href", "") if link else ""
                    item_url = href if href.startswith("http") else ("https://kolesa.ru" + href)

                    price_el = (
                        card.select_one(".a-card__price")
                        or card.select_one("[class*='price']")
                    )
                    price = price_el.get_text(strip=True) if price_el else ""

                    date_el = card.select_one("[class*='date']") or card.select_one("time")
                    date_text = date_el.get_text(strip=True) if date_el else ""
                    date_obj = parse_ru_date(date_text)
                    days = max(0, (today - date_obj).days) if date_obj else 0

                    desc_el = (
                        card.select_one(".a-card__description")
                        or card.select_one("[class*='descr']")
                        or card.select_one("[class*='description']")
                    )
                    desc = desc_el.get_text(strip=True)[:300] if desc_el else ""

                    img_el = card.select_one("img[data-src]") or card.select_one("img[src]")
                    photo_url = ""
                    if img_el:
                        src = img_el.get("data-src") or img_el.get("src", "")
                        if src and src.startswith("http"):
                            photo_url = src

                    if title and item_url and "kolesa.ru" in item_url:
                        price_int = parse_price(price) or 0
                        item = {
                            "source": "kolesa", "title": title, "price": price,
                            "url": item_url, "date": str(date_obj) if date_obj else date_text,
                            "_photos": 0, "_days_on_site": days,
                            "description": desc, "seller": "", "_photo_url": photo_url,
                            "_price_int": price_int,
                        }
                        item["_hot_score"] = hot_score(item)
                        results.append(item)
                except Exception:
                    pass

            time.sleep(0.2)
        except Exception as e:
            print(f"  [Kolesa {region}] стр.{p}: {e}")
            break

    print(f"  [Kolesa] {len(results)} объявлений")
    return results


# ── Парсер Bibika.ru ─────────────────────────────────────────────

BIBIKA_REGIONS = {
    "ekaterinburg": "ekaterinburg",
    "moscow":       "moscow",
    "spb":          "spb",
    "novosibirsk":  "novosibirsk",
    "kazan":        "kazan",
    "chelyabinsk":  "chelyabinsk",
    "ufa":          "ufa",
    "krasnodar":    "krasnodar",
    "omsk":         "omsk",
    "tyumen":       "tyumen",
    "perm":         "perm",
    "krasnoyarsk":  "krasnoyarsk",
    "voronezh":     "voronezh",
    "samara":       "samara",
    "rostov":       "rostov",
}


def scrape_bibika(region: str, pages: int = 3, price_min: int = 0, price_max: int = 99_000_000) -> list[dict]:
    slug = BIBIKA_REGIONS.get(region, region)
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
    except ImportError:
        return []

    results = []
    today = datetime.date.today()
    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })

    for p in range(1, pages + 1):
        # bibika.ru: частные объявления
        url = f"https://bibika.ru/auto/{slug}/"
        params: dict = {"page": p, "private": "1"}
        if price_min > 0:
            params["price_from"] = price_min
        if price_max < 99_000_000:
            params["price_to"] = price_max

        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code != 200:
                print(f"  [Bibika {region}] HTTP {r.status_code}")
                break
            soup = _BS(r.text, "lxml")

            cards = (
                soup.select("div.bull-item")
                or soup.select(".auto-item")
                or soup.select("article.car-item")
                or soup.select("div[itemtype*='Product']")
                or soup.select("[class*='auto-item']")
            )
            if not cards:
                print(f"  [Bibika {region}] стр.{p}: нет карточек")
                break

            for card in cards:
                try:
                    link = (
                        card.select_one("a[href*='/auto/']")
                        or card.select_one("h2 a")
                        or card.select_one("h3 a")
                    )
                    title = link.get_text(strip=True) if link else ""
                    href = link.get("href", "") if link else ""
                    item_url = href if href.startswith("http") else ("https://bibika.ru" + href)

                    price_el = card.select_one("[class*='price']") or card.select_one("[itemprop='price']")
                    price = price_el.get("content") or price_el.get_text(strip=True) if price_el else ""

                    date_el = card.select_one("[class*='date']") or card.select_one("time")
                    date_text = date_el.get_text(strip=True) if date_el else ""
                    date = parse_ru_date(date_text)
                    days = max(0, (today - date).days) if date else 0

                    desc_el = (
                        card.select_one("[class*='descr']")
                        or card.select_one("[class*='description']")
                        or card.select_one("[itemprop='description']")
                        or card.select_one("p")
                    )
                    desc = desc_el.get_text(strip=True)[:300] if desc_el else ""

                    if title and item_url and "bibika.ru" in item_url:
                        price_int = parse_price(price) or 0
                        item = {
                            "source": "bibika", "title": title, "price": price,
                            "url": item_url, "date": str(date) if date else date_text,
                            "_photos": 0, "_days_on_site": days,
                            "description": desc, "seller": "", "_photo_url": "",
                            "_price_int": price_int,
                        }
                        item["_hot_score"] = hot_score(item)
                        results.append(item)
                except Exception:
                    pass

            time.sleep(0.2)
        except Exception as e:
            print(f"  [Bibika {region}] стр.{p}: {e}")
            break

    print(f"  [Bibika] {len(results)} объявлений")
    return results


# ── Парсер Авито ────────────────────────────────────────────────

# Слаги для Авито — городской слаг для URL
AVITO_SLUGS = {
    "ekaterinburg": "ekaterinburg",
    "moscow":       "moskva",
    "spb":          "sankt-peterburg",
    "novosibirsk":  "novosibirsk",
    "kazan":        "kazan",
    "chelyabinsk":  "chelyabinsk",
    "ufa":          "ufa",
    "krasnodar":    "krasnodar",
    "omsk":         "omsk",
    "tyumen":       "tyumen",
    "perm":         "perm",
    "krasnoyarsk":  "krasnoyarsk",
    "voronezh":     "voronezh",
    "samara":       "samara",
    "rostov":       "rostov-na-donu",
}

# ID локаций для Авито API
AVITO_LOCATION_IDS = {
    "ekaterinburg": 621940,
    "moscow":       637640,
    "spb":          638582,
    "novosibirsk":  661122,
    "kazan":        621133,
    "chelyabinsk":  1282,
    "ufa":          1281,
    "krasnodar":    13579,
    "omsk":         665066,
    "tyumen":       641900,
    "perm":         656049,
    "krasnoyarsk":  641901,
    "voronezh":     621890,
    "samara":       621540,
    "rostov":       621900,
}



def _avito_scraperapi(url: str) -> "requests.Response | None":
    """Запрашивает страницу через ScraperAPI с JS-рендером и ждёт 5 секунд."""
    try:
        import requests as _req
        r = _req.get("http://api.scraperapi.com", params={
            "api_key": SCRAPER_API_KEY,
            "url": url,
            "render": "true",
            "wait": "5000",
            "country_code": "ru",
        }, timeout=120)
        return r
    except Exception as e:
        print(f"  [ScraperAPI] ошибка: {e}")
        return None


def _avito_extract_links(text: str, slug: str) -> list[str]:
    """Извлекает URL объявлений Авито прямо из HTML текста по паттерну href."""
    pattern = rf'href="(/{re.escape(slug)}/[a-z0-9_/-]+-\d{{5,}})"'
    hrefs = re.findall(pattern, text)
    seen = set()
    result = []
    for h in hrefs:
        if h not in seen:
            seen.add(h)
            result.append("https://www.avito.ru" + h)
    return result


def _avito_price_from_item(it: dict) -> tuple[str, int]:
    """Извлекает цену из объекта Авито. Возвращает (строка, число)."""
    # Пробуем все возможные пути к цене
    for key in ("priceDetailed", "price", "priceInfo"):
        info = it.get(key)
        if not info:
            continue
        if isinstance(info, (int, float)) and info > 0:
            return f"{int(info):,} ₽".replace(",", " "), int(info)
        if isinstance(info, dict):
            for val_key in ("value", "number", "amount", "price"):
                v = info.get(val_key)
                if v and isinstance(v, (int, float)) and v > 0:
                    return f"{int(v):,} ₽".replace(",", " "), int(v)
            # Попробуем text поле
            text = info.get("valueText") or info.get("text") or ""
            digits = re.sub(r"[^\d]", "", str(text))
            if digits and len(digits) >= 4:
                return text, int(digits)
    return "", 0


def _avito_item_from_json(it: dict, today) -> dict | None:
    """Преобразует объект Авито JSON в dict объявления. Возвращает None для дилеров."""
    try:
        title = it.get("title", "")
        url_path = it.get("urlPath") or it.get("url", "")
        if not url_path:
            return None
        item_url = ("https://www.avito.ru" + url_path) if url_path.startswith("/") else url_path
        if not title or "avito.ru" not in item_url:
            return None

        # Фильтр дилеров по типу продавца в JSON
        seller_obj = it.get("seller") or it.get("user") or {}
        if isinstance(seller_obj, dict):
            seller_type = (
                seller_obj.get("type") or
                seller_obj.get("accountType") or
                seller_obj.get("sellerType") or ""
            ).lower()
            # company, shop, dealer, pro, business — дилеры
            if any(t in seller_type for t in ("company", "shop", "dealer", "pro", "business", "commercial")):
                return None
        # Доп. проверка по названию продавца
        seller_name = ""
        if isinstance(seller_obj, dict):
            seller_name = seller_obj.get("name") or seller_obj.get("title") or ""

        price_str, price_int = _avito_price_from_item(it)

        images = (it.get("images") or
                  it.get("gallery", {}).get("images", []) or
                  it.get("photos", []) or [])
        photo_url = ""
        if images and isinstance(images, list):
            img = images[0]
            if isinstance(img, dict):
                # Пробуем все известные ключи CDN Авито по убыванию размера
                photo_url = (img.get("864x648") or img.get("1280x960") or
                             img.get("640x480") or img.get("432x324") or
                             img.get("320x240") or img.get("url") or
                             img.get("src") or
                             next((v for v in img.values() if isinstance(v, str) and "avito" in v), "") or
                             next((v for v in img.values() if isinstance(v, str)), ""))
            elif isinstance(img, str):
                photo_url = img
        if photo_url and photo_url.startswith("//"):
            photo_url = "https:" + photo_url

        item = {
            "source": "avito", "title": title,
            "price": price_str, "url": item_url, "date": str(today),
            "_photos": len(images), "_days_on_site": 0,
            "description": (it.get("description") or "")[:300],
            "seller": seller_name, "_photo_url": photo_url,
            "_price_int": price_int,  # для точной фильтрации по цене
        }
        item["_hot_score"] = hot_score(item)
        return item
    except Exception:
        return None


def _avito_find_items_in_json(obj, depth=0) -> list:
    """Рекурсивно ищет массив объявлений в JSON Авито."""
    if depth > 10 or not isinstance(obj, (dict, list)):
        return []
    if isinstance(obj, list):
        if len(obj) >= 2 and isinstance(obj[0], dict):
            sample = obj[0]
            url_fields = {"title", "urlPath", "url", "name"}
            price_fields = {"price", "priceDetailed", "priceInfo"}
            if (url_fields & sample.keys()) and (price_fields & sample.keys() or "id" in sample):
                return obj
        for x in obj:
            r = _avito_find_items_in_json(x, depth + 1)
            if r:
                return r
        return []
    if isinstance(obj, dict):
        for key in ("items", "catalog", "listing", "offers", "data", "list", "ads", "cars"):
            val = obj.get(key)
            if isinstance(val, list) and len(val) >= 2:
                sample = val[0] if val else {}
                if isinstance(sample, dict) and {"title", "urlPath", "url", "name", "id"} & sample.keys():
                    return val
        for v in obj.values():
            r = _avito_find_items_in_json(v, depth + 1)
            if r:
                return r
    return []


def _parse_avito_html(text: str, slug: str, today) -> list[dict]:
    """Парсит HTML страницы Авито: __NEXT_DATA__, HTML-карточки, ссылки."""
    try:
        from bs4 import BeautifulSoup as _BS
    except ImportError:
        return []

    results = []
    soup = _BS(text, "lxml")

    # 1. __NEXT_DATA__ (Next.js SSR)
    nd = soup.find("script", {"id": "__NEXT_DATA__"})
    if nd and nd.string:
        try:
            data = json.loads(nd.string)
            items_raw = _avito_find_items_in_json(data)
            print(f"  [Авито] __NEXT_DATA__ найден, items_raw={len(items_raw)}")
            for it in items_raw:
                item = _avito_item_from_json(it, today)
                if item:
                    results.append(item)
            if results:
                return results
        except Exception as e:
            print(f"  [Авито] __NEXT_DATA__ ошибка: {e}")

    # 1b. Любой <script> тег с "items":[ или "catalog":[
    if not results:
        for sc in soup.find_all("script"):
            sc_text = sc.string or ""
            if len(sc_text) < 500:
                continue
            for marker in ('"items":[{', '"catalog":[{', '"listing":[{', '"offers":[{'):
                if marker not in sc_text:
                    continue
                idx = sc_text.find(marker) + len(marker) - 2  # позиция [
                chunk = sc_text[idx:]
                depth = 0
                end = 0
                in_str = False
                esc = False
                for i, ch in enumerate(chunk):
                    if esc:
                        esc = False
                        continue
                    if ch == '\\' and in_str:
                        esc = True
                        continue
                    if ch == '"':
                        in_str = not in_str
                        continue
                    if not in_str:
                        if ch == '[':
                            depth += 1
                        elif ch == ']':
                            depth -= 1
                            if depth == 0:
                                end = i + 1
                                break
                if end:
                    try:
                        arr = json.loads(chunk[:end])
                        if isinstance(arr, list) and len(arr) >= 2:
                            for it in arr:
                                item = _avito_item_from_json(it, today)
                                if item:
                                    results.append(item)
                    except Exception:
                        pass
            if results:
                print(f"  [Авито] script-JSON: {len(results)} объявлений")
                return results

    # 2. HTML карточки с data-marker="item"
    cards = soup.select("[data-marker='item']")
    print(f"  [Авито] HTML cards={len(cards)}")
    for card in cards:
        try:
            link = (
                card.select_one("a[data-marker='item-title']")
                or card.select_one(f"a[href*='/{slug}/']")
                or card.select_one("a[href*='/avtomobili/']")
                or card.select_one("a[href]")
            )
            href = link.get("href", "") if link else ""
            item_url = ("https://www.avito.ru" + href) if href.startswith("/") else href
            if not item_url or "avito.ru" not in item_url:
                continue

            title_el = (
                card.select_one("[data-marker='item-title']")
                or card.select_one("[itemprop='name']")
                or card.select_one("h3")
                or card.select_one("h2")
            )
            title = title_el.get_text(strip=True) if title_el else ""

            price_el = (
                card.select_one("[itemprop='price']")
                or card.select_one("[data-marker='item-price']")
                or card.select_one("[class*='price']")
            )
            price = ""
            if price_el:
                price = price_el.get("content") or price_el.get_text(strip=True)

            photo_url = ""
            for img_el in card.find_all("img"):
                src = (img_el.get("src") or img_el.get("data-src") or
                       img_el.get("data-lazy-src") or img_el.get("data-original") or "")
                if src.startswith("//"):
                    src = "https:" + src
                if src.startswith("http") and len(src) > 20:
                    photo_url = src
                    break
            # Fallback: regex по сырому тексту карточки
            if not photo_url:
                card_str = str(card)
                img_m = re.search(r'((?:https?:)?//[^"\']+(?:img\.avito|avito\.st|cdn-avito)[^"\']+\.(?:jpg|jpeg|webp))', card_str)
                if img_m:
                    raw = img_m.group(1)
                    photo_url = ("https:" + raw) if raw.startswith("//") else raw

            if title:
                price_int = parse_price(price)
                item = {
                    "source": "avito", "title": title, "price": price,
                    "url": item_url, "date": str(today),
                    "_photos": 0, "_days_on_site": 0,
                    "description": "", "seller": "", "_photo_url": photo_url,
                    "_price_int": price_int,
                }
                item["_hot_score"] = hot_score(item)
                results.append(item)
        except Exception:
            pass

    if results:
        return results

    # 3. Regex по "urlPath" + "title" прямо в тексте скриптов
    has_urlpath = '"urlPath"' in text
    print(f"  [Авито] в тексте: urlPath={has_urlpath}, размер={len(text):,}")
    # Извлекаем urlPath + title (не пересекаем границу объекта [^}])
    url_title_pairs = re.findall(
        r'"urlPath"\s*:\s*"(/[^"]{10,})"[^}]{0,600}"title"\s*:\s*"([^"]{5,100})"',
        text
    )
    if not url_title_pairs:
        # Попробуем title → urlPath (порядок может быть обратным)
        url_title_pairs = [
            (u, t) for t, u in re.findall(
                r'"title"\s*:\s*"([^"]{5,100})"[^}]{0,600}"urlPath"\s*:\s*"(/[^"]{10,})"',
                text
            )
        ]
    print(f"  [Авито] url+title пар: {len(url_title_pairs)}")

    # Строим карту urlPath → цена: ищем ценовые поля в широком окне вокруг urlPath
    price_map: dict[str, int] = {}

    def _find_price_in_window(window: str) -> int:
        """Пробует все известные форматы цены Авито. Возвращает 0 если не нашёл."""
        patterns = [
            r'"priceDetailed"\s*:\s*\{[^}]{0,100}"value"\s*:\s*(\d{5,8})',
            r'"price"\s*:\s*\{[^}]{0,100}"value"\s*:\s*(\d{5,8})',
            r'"priceInfo"\s*:\s*\{[^}]{0,100}"value"\s*:\s*(\d{5,8})',
            r'"price"\s*:\s*(\d{5,8})',       # цена как прямое число
            r'"valueText"\s*:\s*"([\d \s]+)\s*[₽р]"',  # "1 200 000 ₽"
            r'"valueText"\s*:\s*"(\d[\d\s]+)"',
        ]
        for pat in patterns:
            pm = re.search(pat, window)
            if pm:
                raw = re.sub(r'\D', '', pm.group(1))
                if raw:
                    v = int(raw)
                    if 10_000 < v < 99_000_000:
                        return v
        return 0

    # Карта urlPath → фото (ищем CDN-ссылки рядом с urlPath)
    photo_map: dict[str, str] = {}
    for m in re.finditer(r'"urlPath"\s*:\s*"(/[^"]{10,})"', text):
        upath = m.group(1).split("?")[0]
        window_start = max(0, m.start() - 1000)
        window_end = min(len(text), m.end() + 4000)
        window = text[window_start:window_end]
        v = _find_price_in_window(window)
        if v:
            price_map[upath] = v
        # Ищем фото CDN Авито — могут быть //img.avito.st/... (без схемы) или https://...
        img_m = re.search(
            r'"(?:864x648|640x480|320x240|url)"\s*:\s*"((?:https?:)?//[^"]{10,}(?:avito|img)[^"]{5,}\.(?:jpg|jpeg|webp|png))"',
            window
        )
        if img_m:
            raw_url = img_m.group(1).replace("\\/", "/")
            photo_map[upath] = ("https:" + raw_url) if raw_url.startswith("//") else raw_url
        else:
            # Более широкий поиск по любому img.avito CDN
            img_m2 = re.search(r'((?:https?:)?//[^"\']{5,}(?:img\.avito|avito\.st|cdn-avito)[^"\']{5,}\.(?:jpg|jpeg|webp))', window)
            if img_m2:
                raw_url = img_m2.group(1)
                photo_map[upath] = ("https:" + raw_url) if raw_url.startswith("//") else raw_url

    sample_prices = list(price_map.values())[:5]
    print(f"  [Авито] цен найдено: {len(price_map)}, фото: {len(photo_map)}, примеры: {sample_prices}")
    seen_urls: set = set()
    for url_path, title in url_title_pairs[:80]:
        if not url_path.startswith("/") or len(url_path) < 10:
            continue
        if any(skip in url_path for skip in ("/profile/", "/user/", "/search?", "/avtomobili?", "/category/")):
            continue
        url_path = url_path.split("?")[0]
        item_url = "https://www.avito.ru" + url_path
        if item_url in seen_urls:
            continue
        seen_urls.add(item_url)
        price_int = price_map.get(url_path, 0)
        price = f"{price_int:,} ₽".replace(",", " ") if price_int else ""
        photo_url = photo_map.get(url_path, "")
        item = {
            "source": "avito", "title": title, "price": price,
            "url": item_url, "date": str(today),
            "_photos": 1 if photo_url else 0, "_days_on_site": 0,
            "description": "", "seller": "", "_photo_url": photo_url,
            "_price_int": price_int,
        }
        item["_hot_score"] = hot_score(item)
        results.append(item)

    print(f"  [Авито] regex итого: {len(results)}")
    return results


def _scrape_avito_direct(slug: str, pages: int, price_min: int, price_max: int, today) -> list[dict]:
    """Прямой запрос к Авито без ScraperAPI (мобильный User-Agent)."""
    try:
        import requests as _req
    except ImportError:
        return []

    results = []
    session = _req.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,*/*",
        "Referer": "https://www.avito.ru/",
    })

    for p in range(1, pages + 1):
        url = f"https://www.avito.ru/{slug}/avtomobili"
        params: dict = {"p": p}
        if price_min > 0:
            params["pmin"] = price_min
        if price_max < 99_000_000:
            params["pmax"] = price_max
        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code != 200:
                print(f"  [Авито прямой] HTTP {r.status_code}")
                break
            batch = _parse_avito_html(r.text, slug, today)
            if not batch:
                break
            results.extend(batch)
            time.sleep(0.3)
        except Exception as e:
            print(f"  [Авито прямой] стр.{p}: {e}")
            break

    return results


def scrape_avito(region: str, pages: int = 5, price_min: int = 0, price_max: int = 99_000_000, sort_by_date: bool = False) -> list[dict]:
    """
    Использует ScraperAPI БЕЗ render — Авито отдаёт SSR-HTML с data-marker='item' карточками.
    render=true требует premium аккаунта ScraperAPI и возвращает 500.
    """
    slug = AVITO_SLUGS.get(region, region)
    today = datetime.date.today()

    if not SCRAPER_API_KEY:
        print("  [Авито] нет SCRAPER_API_KEY")
        return []

    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
    except ImportError:
        return []

    results = []

    for p in range(1, pages + 1):
        # Чистый URL без фильтров — только так Авито отдаёт SSR-HTML с данными
        # owner[]=1 меняет структуру ответа и данные пропадают
        qs_parts = []
        if p > 1:
            qs_parts.append(f"p={p}")
        if price_min > 0:
            qs_parts.append(f"pmin={price_min}")
        if price_max < 99_000_000:
            qs_parts.append(f"pmax={price_max}")
        if sort_by_date:
            qs_parts.append("s=104")   # Авито: сортировка по дате (новые сверху)
        qs_parts.append("cd=1")  # только частные объявления
        url = f"https://www.avito.ru/{slug}/avtomobili"
        url += "?" + "&".join(qs_parts)

        try:
            r = _req.get("http://api.scraperapi.com", params={
                "api_key": SCRAPER_API_KEY,
                "url": url,
                "country_code": "ru",
            }, timeout=40)

            if r.status_code != 200:
                print(f"  [Авито] стр.{p}: HTTP {r.status_code}")
                break

            text = r.text
            size = len(text)
            has_items = 'data-marker="item"' in text
            has_urlpath = '"urlPath"' in text
            print(f"  [Авито] стр.{p}: {size:,}б, items={has_items}, urlPath={has_urlpath}, url={url[:60]}")

            # Если ответ с ценами маленький — пробуем premium прокси для цен
            if not has_urlpath and (price_min > 0 or price_max < 99_000_000):
                print(f"  [Авито] пробую premium ScraperAPI с ценами...")
                r_prem = _req.get("http://api.scraperapi.com", params={
                    "api_key": SCRAPER_API_KEY,
                    "url": url,
                    "country_code": "ru",
                    "premium": "true",
                }, timeout=60)
                if r_prem.status_code == 200 and '"urlPath"' in r_prem.text:
                    text = r_prem.text
                    has_items = 'data-marker="item"' in text
                    has_urlpath = True
                    print(f"  [Авито] premium с ценами: {len(text):,}б, items={has_items}")
                else:
                    print(f"  [Авито] premium не помог ({r_prem.status_code}), fallback без цен...")
                    r2 = _req.get("http://api.scraperapi.com", params={
                        "api_key": SCRAPER_API_KEY,
                        "url": f"https://www.avito.ru/{slug}/avtomobili",
                        "country_code": "ru",
                    }, timeout=40)
                    if r2.status_code == 200 and '"urlPath"' in r2.text:
                        text = r2.text
                        has_items = 'data-marker="item"' in text
                        print(f"  [Авито] fallback без цен: {len(text):,}б, items={has_items}")

            if not has_items and '"urlPath"' not in text:
                print(f"  [Авито] стр.{p}: нет данных, стоп")
                break

            # Парсим HTML страницы через универсальную функцию
            batch = _parse_avito_html(text, slug, today)
            print(f"  [Авито] стр.{p}: распаршено {len(batch)} объявлений")

            # Если цены не найдены через JSON — пробуем regex по сырому тексту
            has_prices = any(it.get("_price_int", 0) > 0 for it in batch)
            if batch and not has_prices:
                # Строим карту urlPath → цена из regex по тексту
                price_map: dict[str, int] = {}
                for m in re.finditer(
                    r'"urlPath"\s*:\s*"(/[^"]+)"[^}]{0,600}?"value"\s*:\s*(\d{4,8})',
                    text, re.DOTALL
                ):
                    url_p, val = m.group(1), int(m.group(2))
                    if 10_000 < val < 99_000_000:
                        price_map[url_p] = val
                print(f"  [Авито] regex цены: {len(price_map)} найдено")
                for it in batch:
                    if it.get("_price_int", 0) == 0:
                        path = it["url"].replace("https://www.avito.ru", "")
                        if path in price_map:
                            v = price_map[path]
                            it["_price_int"] = v
                            it["price"] = f"{v:,} ₽".replace(",", " ")

            if batch:
                results.extend(batch)

            time.sleep(0.3)
        except Exception as e:
            print(f"  [Авито] стр.{p}: {e}")
            break

    print(f"  [Авито] итого {len(results)} объявлений")
    return results



# ── FSM состояния ────────────────────────────────────────────────

class Setup(StatesGroup):
    region = State()
    price_min = State()
    price_max = State()


# ── Бот ─────────────────────────────────────────────────────────

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# URL-ID маппинг для кнопок
_id_to_url: dict[str, str] = {}
_url_to_id: dict[str, str] = {}
_id_counter = 0


def url_to_id(url: str) -> str:
    global _id_counter
    if url not in _url_to_id:
        _id_counter += 1
        sid = str(_id_counter)
        _url_to_id[url] = sid
        _id_to_url[sid] = url
    return _url_to_id[url]


def id_to_url(sid: str) -> str:
    return _id_to_url.get(sid, sid)


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔍 Найти авто"), KeyboardButton(text="🔔 Уведомления")],
        [KeyboardButton(text="⭐ Избранное"),  KeyboardButton(text="⚙️ Настройки")],
    ],
    resize_keyboard=True,
    persistent=True,
)


def region_keyboard():
    rows = []
    items = list(REGIONS.items())
    for i in range(0, len(items), 2):
        row = []
        for slug, name in items[i:i+2]:
            row.append(InlineKeyboardButton(text=name, callback_data=f"region|{slug}"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    s = load_settings(msg.from_user.id)
    if s.get("region"):
        region_name = REGIONS.get(s["region"], s["region"])
        pmin = s.get("price_min", 0)
        pmax = s.get("price_max", 99_000_000)
        mon = "🟢" if s.get("monitor_enabled") else "🔴"
        await msg.answer(
            f"👋 Привет! Твои настройки:\n"
            f"📍 Регион: {region_name}\n"
            f"💰 Бюджет: {pmin:,} – {pmax:,} ₽\n"
            f"🔔 Мониторинг: {mon}",
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        await msg.answer(
            "👋 Привет! Я ищу автомобили от частных лиц по цене ниже рынка.\n\n"
            "Для начала выбери регион поиска:",
            reply_markup=MAIN_KEYBOARD,
        )
        await msg.answer("📍 Выбери город:", reply_markup=region_keyboard())
        await state.set_state(Setup.region)


def _notify_keyboard(s: dict) -> InlineKeyboardMarkup:
    enabled = s.get("monitor_enabled", False)
    interval = s.get("monitor_interval_min", 5)
    min_pct = s.get("monitor_min_savings_pct", 10)
    toggle_text = "🔕 Выключить мониторинг" if enabled else "🔔 Включить мониторинг"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data="notify_toggle")],
        [
            InlineKeyboardButton(text=f"⏱ Каждые {interval} мин", callback_data="notify_interval"),
        ],
        [
            InlineKeyboardButton(text=f"📉 Скидка от {min_pct}%", callback_data="notify_pct"),
        ],
        [InlineKeyboardButton(text="⭐ Моё избранное", callback_data="notify_favs")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="notify_back")],
    ])


@dp.callback_query(F.data == "notify_settings")
async def cb_notify_settings(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    enabled = s.get("monitor_enabled", False)
    status = "✅ Включён" if enabled else "❌ Выключен"
    interval = s.get("monitor_interval_min", 5)
    min_pct = s.get("monitor_min_savings_pct", 10)
    await cb.message.answer(
        f"🔔 *Настройки уведомлений*\n\n"
        f"Статус: {status}\n"
        f"Интервал проверки: каждые {interval} мин\n"
        f"Минимальная скидка: {min_pct}% ниже рынка\n\n"
        f"Бот проверяет Авито и присылает уведомление когда появляются выгодные авто.",
        parse_mode="Markdown",
        reply_markup=_notify_keyboard(s),
    )


@dp.callback_query(F.data == "notify_toggle")
async def cb_notify_toggle(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    enabled = not s.get("monitor_enabled", False)
    s["monitor_enabled"] = enabled
    save_settings(uid, s)
    if enabled:
        _start_monitor(uid)
        region_name = REGIONS.get(s.get("region", ""), s.get("region", ""))
        await cb.message.answer(
            f"✅ *Мониторинг включён!*\n\nБуду присылать новые авто в {region_name} ниже рынка.\n"
            f"Интервал: каждые {s.get('monitor_interval_min', 15)} мин.",
            parse_mode="Markdown",
            reply_markup=_notify_keyboard(s),
        )
    else:
        _stop_monitor(uid)
        await cb.message.answer(
            "🔕 Мониторинг выключен.",
            reply_markup=_notify_keyboard(s),
        )


@dp.callback_query(F.data == "notify_interval")
async def cb_notify_interval(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    current = s.get("monitor_interval_min", 5)
    options = [5, 10, 15, 30, 60]
    next_val = options[(options.index(current) + 1) % len(options)] if current in options else 15
    s["monitor_interval_min"] = next_val
    save_settings(uid, s)
    if s.get("monitor_enabled"):
        _stop_monitor(uid)
        _start_monitor(uid)
    await cb.message.edit_reply_markup(reply_markup=_notify_keyboard(s))


@dp.callback_query(F.data == "notify_pct")
async def cb_notify_pct(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    current = s.get("monitor_min_savings_pct", 10)
    options = [5, 10, 15, 20, 25]
    next_val = options[(options.index(current) + 1) % len(options)] if current in options else 10
    s["monitor_min_savings_pct"] = next_val
    save_settings(uid, s)
    await cb.message.edit_reply_markup(reply_markup=_notify_keyboard(s))


@dp.callback_query(F.data == "notify_favs")
async def cb_notify_favs(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    fav_file = user_dir(uid) / "favorites.json"
    if not fav_file.exists():
        await cb.message.answer("⭐ У тебя пока нет сохранённых объявлений.")
        return
    favs = json.loads(fav_file.read_text(encoding="utf-8"))
    if not favs:
        await cb.message.answer("⭐ Список избранного пуст.")
        return
    lines = [f"• {it.get('title','')} — {it.get('price','?')}\n  {it.get('url','')}" for it in favs[-10:]]
    await cb.message.answer(f"⭐ *Избранное* ({len(favs)} шт.):\n\n" + "\n\n".join(lines), parse_mode="Markdown")


@dp.callback_query(F.data == "notify_back")
async def cb_notify_back(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    s = load_settings(uid)
    region = s.get("region", "")
    region_name = REGIONS.get(region, region)
    pmin = s.get("price_min", 0)
    pmax = s.get("price_max", 99_000_000)
    await cb.message.answer(
        f"👋 Твои настройки:\n📍 {region_name}\n💰 {pmin:,}–{pmax:,} ₽",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Найти авто", callback_data="do_search")],
            [InlineKeyboardButton(text="⚙️ Изменить настройки", callback_data="change_settings")],
            [InlineKeyboardButton(text="🔔 Уведомления", callback_data="notify_settings")],
        ])
    )


@dp.callback_query(F.data == "change_settings")
async def cb_change_settings(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.answer("📍 Выбери город:", reply_markup=region_keyboard())
    await state.set_state(Setup.region)


@dp.callback_query(F.data.startswith("region|"), Setup.region)
async def cb_region(cb: CallbackQuery, state: FSMContext):
    slug = cb.data.split("|", 1)[1]
    await state.update_data(region=slug)
    await cb.answer(f"✅ {REGIONS.get(slug, slug)}")
    await cb.message.answer(
        f"📍 Регион: {REGIONS.get(slug, slug)}\n\n"
        f"💰 Теперь введи минимальную цену в рублях\n"
        f"(например: 300000 или 0 для любой цены):"
    )
    await state.set_state(Setup.price_min)


@dp.message(Setup.price_min)
async def fsm_price_min(msg: Message, state: FSMContext):
    digits = re.sub(r"[^\d]", "", msg.text or "")
    pmin = int(digits) if digits else 0
    await state.update_data(price_min=pmin)
    await msg.answer(
        f"✅ Минимальная цена: {pmin:,} ₽\n\n"
        f"💰 Теперь введи максимальную цену\n"
        f"(например: 1000000):"
    )
    await state.set_state(Setup.price_max)


@dp.message(Setup.price_max)
async def fsm_price_max(msg: Message, state: FSMContext):
    digits = re.sub(r"[^\d]", "", msg.text or "")
    pmax = int(digits) if digits else 99_000_000
    data = await state.get_data()
    region = data.get("region", "ekaterinburg")
    pmin = data.get("price_min", 0)

    s = {"region": region, "price_min": pmin, "price_max": pmax}
    save_settings(msg.from_user.id, s)
    await state.clear()

    region_name = REGIONS.get(region, region)
    await msg.answer(
        f"✅ Настройки сохранены!\n\n"
        f"📍 Регион: {region_name}\n"
        f"💰 Бюджет: {pmin:,} – {pmax:,} ₽\n\n"
        f"Нажми кнопку чтобы найти авто:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Найти авто", callback_data="do_search")],
        ])
    )


@dp.message(Command("settings"))
@dp.message(F.text == "⚙️ Настройки")
async def cmd_settings(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("📍 Выбери город:", reply_markup=region_keyboard())
    await state.set_state(Setup.region)


ALL_SOURCES = ["drom", "autoru", "avito"]
SOURCE_NAMES = {
    "drom":   "🔵 Дром",
    "autoru": "🟠 Auto.ru",
    "kolesa": "🟢 Kolesa",
    "bibika": "🟣 Bibika",
    "avito":  "🔴 Авито",
}


def sources_keyboard(enabled: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for src in ALL_SOURCES:
        check = "✅" if src in enabled else "☐"
        rows.append([InlineKeyboardButton(
            text=f"{check} {SOURCE_NAMES[src]}",
            callback_data=f"toggle_src|{src}"
        )])
    rows.append([
        InlineKeyboardButton(text="🌐 Все площадки", callback_data="src_all"),
        InlineKeyboardButton(text="🔍 Искать", callback_data="start_search"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _get_enabled_sources(s: dict) -> list[str]:
    """Возвращает список включённых площадок, по умолчанию — все."""
    enabled = s.get("sources", [])
    if not enabled:
        return list(ALL_SOURCES)
    return enabled


@dp.message(Command("search"))
@dp.message(F.text == "🔍 Найти авто")
async def cmd_search(msg: Message):
    uid = msg.from_user.id
    s = load_settings(uid)
    if not s.get("region"):
        await msg.answer("Сначала настрой поиск: /start")
        return
    enabled = _get_enabled_sources(s)
    await msg.answer("Выбери площадки для поиска:", reply_markup=sources_keyboard(enabled))


@dp.callback_query(F.data.startswith("toggle_src|"))
async def cb_toggle_src(cb: CallbackQuery):
    src = cb.data.split("|")[1]
    uid = cb.from_user.id
    s = load_settings(uid)
    enabled = list(_get_enabled_sources(s))
    if src in enabled:
        if len(enabled) > 1:  # оставляем хотя бы одну
            enabled.remove(src)
    else:
        enabled.append(src)
    s["sources"] = enabled
    save_settings(uid, s)
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=sources_keyboard(enabled))


@dp.callback_query(F.data == "src_all")
async def cb_src_all(cb: CallbackQuery):
    uid = cb.from_user.id
    s = load_settings(uid)
    s["sources"] = list(ALL_SOURCES)
    save_settings(uid, s)
    await cb.answer("Все площадки включены")
    await cb.message.edit_reply_markup(reply_markup=sources_keyboard(ALL_SOURCES))


@dp.callback_query(F.data == "do_search")
async def cb_do_search(cb: CallbackQuery):
    uid = cb.from_user.id
    s = load_settings(uid)
    if not s.get("region"):
        await cb.answer()
        await cb.message.answer("Сначала настрой поиск: /start")
        return
    await cb.answer()
    enabled = _get_enabled_sources(s)
    await cb.message.answer("Выбери площадки для поиска:", reply_markup=sources_keyboard(enabled))


@dp.callback_query(F.data == "start_search")
async def cb_start_search(cb: CallbackQuery):
    await cb.answer()
    await do_search_for_user(cb.from_user.id, cb.message)


# ── Загрузка деталей объявления ─────────────────────────────────

# Плейсхолдеры Дрома которые не являются фото машины
_DROM_PLACEHOLDER_URLS = ["drom.ru/img/app", "/placeholder", "mascot", "no-photo", "nophoto", "default"]

# Маркеры снятого объявления в HTML/JSON страницы
_SOLD_MARKERS = [
    "снят с продажи", "снято с продажи", "объявление снято",
    "объявление не найдено", "объявление недоступно", "объявление удалено",
    '"isSold":true', '"sold":true', '"status":"sold"', '"status":"inactive"',
    '"isArchived":true', 'bulletin-sold', 'data-bulletin-status="sold"',
    "listing not found", "offer not found",
]

_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def _fetch_and_check(url: str, source: str) -> dict | None:
    """
    Один запрос на страницу объявления:
    - проверяет активность (None = снято)
    - возвращает фото и описание
    """
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
        r = _req.get(url, headers=_FETCH_HEADERS, timeout=12, allow_redirects=True)
        if r.status_code in (404, 410):
            return None
        text = r.text

        # Проверяем маркеры снятого объявления
        text_lower = text.lower()
        if any(m.lower() in text_lower for m in _SOLD_MARKERS):
            return None

        soup = _BS(text, "lxml")

        # Фото: og:image
        photo_url = ""
        og = soup.select_one("meta[property='og:image']")
        if og:
            photo_url = og.get("content", "").strip()
        # Отфильтровываем плейсхолдеры (логотип Дрома, хомяка и т.п.)
        if photo_url and any(p in photo_url for p in _DROM_PLACEHOLDER_URLS):
            photo_url = ""
        # Если og:image не подошёл — берём первую img с cdn/s.
        if not photo_url:
            for img in soup.select("img[src]"):
                src = img.get("src", "")
                if src.startswith("http") and any(x in src for x in ["s.auto.", "cdn", "photos", "images"]):
                    photo_url = src
                    break
        if photo_url and not photo_url.startswith("http"):
            photo_url = "https:" + photo_url if photo_url.startswith("//") else ""

        # Описание продавца
        if source == "drom":
            desc_el = (
                soup.select_one("[data-ftid='bull_description']")
                or soup.select_one("[data-ftid='item_description']")
                or soup.select_one("div[class*='comment']")
                or soup.select_one("div[class*='description']")
            )
        elif source == "avito":
            desc_el = (
                soup.select_one("div[itemprop='description']")
                or soup.select_one("[data-marker='item-view/item-description']")
                or soup.select_one("div[class*='description-text']")
            )
        else:
            desc_el = (
                soup.select_one("div[class*='description']")
                or soup.select_one("p[class*='description']")
                or soup.select_one("[itemprop='description']")
            )
        description = desc_el.get_text(strip=True)[:500] if desc_el else ""

        return {"_photo_url": photo_url, "description": description}
    except Exception:
        return {}  # Ошибка сети — считаем активным, без деталей


async def enrich_and_filter(items: list[dict], max_check: int = 25) -> list[dict]:
    """
    Параллельно загружает страницы топ-N объявлений,
    фильтрует снятые и обогащает фото+описанием.
    """
    loop = asyncio.get_event_loop()
    to_check = items[:max_check]
    rest = items[max_check:]

    results = await asyncio.gather(
        *[loop.run_in_executor(None, _fetch_and_check, i["url"], i.get("source", "")) for i in to_check]
    )
    active = []
    for item, details in zip(to_check, results):
        if details is None:
            continue  # снято
        item["_enriched"] = True
        if details.get("_photo_url"):
            item["_photo_url"] = details["_photo_url"]
        if details.get("description"):
            item["description"] = details["description"]
        active.append(item)

    return active + rest


async def _ensure_photo(item: dict) -> None:
    """Для объявлений без фото/описания — загружает страницу и вытаскивает данные."""
    need_photo = not item.get("_photo_url")
    need_desc = not item.get("description")
    if not need_photo and not need_desc:
        return
    source = item.get("source", "")
    url = item.get("url", "")
    if not url:
        return

    loop = asyncio.get_event_loop()

    def _fetch() -> tuple[str, str]:
        photo, desc = "", ""
        try:
            import requests as _req
            if source == "avito" and SCRAPER_API_KEY:
                r = _req.get("http://api.scraperapi.com", params={
                    "api_key": SCRAPER_API_KEY,
                    "url": url,
                    "country_code": "ru",
                }, timeout=12)
                if r.status_code != 200:
                    return photo, desc
                text = r.text
                if need_photo:
                    for pat in [
                        r'"(?:864x648|1280x960|640x480)"\s*:\s*"((?:https?:)?//[^"]+\.avito\.st/[^"]+\.(?:jpg|jpeg|webp))"',
                        r'"((?:https?:)?//[0-9]+\.img\.avito\.st/[^"]+\.(?:jpg|jpeg|webp))"',
                    ]:
                        m = re.search(pat, text)
                        if m:
                            raw = m.group(1).replace("\\/", "/")
                            photo = ("https:" + raw) if raw.startswith("//") else raw
                            break
                if need_desc:
                    dm = re.search(r'"description"\s*:\s*"([^"]{20,})"', text)
                    if dm:
                        desc = dm.group(1).replace("\\n", " ").replace('\\"', '"')[:400]
            elif source in ("drom", "autoru"):
                r = _req.get(url, timeout=10, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                })
                if r.status_code != 200:
                    return photo, desc
                if need_photo:
                    m = re.search(r'"(?:1200x900|832x624|1000x750)"\s*:\s*"([^"]+)"', r.text)
                    if m:
                        photo = m.group(1).replace("\\/", "/")
                if need_desc:
                    dm = re.search(r'"description"\s*:\s*"([^"]{20,})"', r.text)
                    if dm:
                        desc = dm.group(1).replace("\\n", " ")[:400]
        except Exception:
            pass
        return photo, desc

    photo, desc = await loop.run_in_executor(None, _fetch)
    if photo:
        item["_photo_url"] = photo
    if desc and not item.get("description"):
        item["description"] = desc


SOURCE_TAGS = {
    "autoru": "🟠 Auto.ru",
    "kolesa": "🟢 Kolesa",
    "bibika": "🟣 Bibika",
    "avito":  "🔴 Авито",
    "drom":   "🔵 Дром",
}

# Кеш результатов поиска: uid -> list[dict]
_search_cache: dict[int, list[dict]] = {}


def _save_cache(uid: int, items: list[dict]):
    try:
        f = user_dir(uid) / "last_search.json"
        f.write_text(json.dumps(items, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass


def _load_cache(uid: int) -> list[dict]:
    try:
        f = user_dir(uid) / "last_search.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


async def send_batch(chat_id: int, uid: int, offset: int):
    """Отправляет 10 объявлений из кеша начиная с offset."""
    items = _search_cache.get(uid) or _load_cache(uid)
    if items:
        _search_cache[uid] = items  # восстанавливаем в память после перезапуска
    if not items or offset >= len(items):
        await bot.send_message(chat_id, "✅ Объявления закончились. Нажми /search для нового поиска.")
        return

    batch = items[offset:offset + 10]
    total = len(items)

    async def _send_item(item: dict):
        url = item.get("url", "")
        sid = url_to_id(url)
        days = item.get("_days_on_site", 0)
        days_str = "сегодня" if days == 0 else f"{days} дн. назад"
        score = item.get("_hot_score", 0)
        hot_tag = " 🔥" if score >= 15 else " ⭐" if score >= 5 else ""
        source_tag = SOURCE_TAGS.get(item.get("source", ""), "🔵")

        price_line = item.get("price", "—") or "—"
        if item.get("_below_market") and item.get("_market_price"):
            market = item["_market_price"]
            pct = item.get("_savings_pct", 0)
            price_line += f"  🔻 рынок ~{market:,} ₽ (-{pct}%)".replace(",", " ")

        caption = (
            f"{source_tag} {item.get('title', '')}{hot_tag}\n"
            f"💰 {price_line}\n"
            f"📅 {days_str}"
        )
        if item.get("description"):
            caption += f"\n\n📝 {item['description'][:500]}"

        source = item.get("source", "")
        phone_hint = "📞 Позвонить" if source in ("avito", "drom") else "📞 Контакт"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔗 Открыть", url=url),
                InlineKeyboardButton(text="⭐ Сохранить", callback_data=f"fav|{sid}|{uid}"),
            ],
            [
                InlineKeyboardButton(text="❌ Скрыть", callback_data=f"hide|{sid}|{uid}"),
                InlineKeyboardButton(text="📋 Похожие", callback_data=f"sim|{sid}|{uid}"),
            ],
        ])

        photo_url = item.get("_photo_url", "")
        if photo_url:
            # Пробуем отправить по URL напрямую
            try:
                await bot.send_photo(chat_id, photo=photo_url, caption=caption, reply_markup=kb)
                return
            except Exception:
                pass
            # Если не вышло — скачиваем байты и шлём файлом
            try:
                import requests as _req
                from aiogram.types import BufferedInputFile
                resp = _req.get(photo_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code == 200 and len(resp.content) > 2000:
                    photo_bytes = BufferedInputFile(resp.content, filename="photo.jpg")
                    await bot.send_photo(chat_id, photo=photo_bytes, caption=caption, reply_markup=kb)
                    return
            except Exception:
                pass
        await bot.send_message(chat_id, caption, reply_markup=kb)

    # Предзагружаем фото (до 6 одновременно), потом отправляем по очереди
    sem = asyncio.Semaphore(6)
    async def _prefetch(it):
        async with sem:
            try:
                await asyncio.wait_for(_ensure_photo(it), timeout=8)
            except Exception:
                pass
    await asyncio.gather(*[_prefetch(it) for it in batch])
    for item in batch:
        await _send_item(item)
        await asyncio.sleep(0.05)

    next_offset = offset + 10
    if next_offset < total:
        await bot.send_message(
            chat_id,
            f"Показано {min(next_offset, total)} из {total}:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"➡️ Ещё {min(10, total - next_offset)} объявлений", callback_data=f"page|{uid}|{next_offset}"),
            ]])
        )
    else:
        await bot.send_message(
            chat_id,
            f"✅ Показаны все {total} объявлений.",
            reply_markup=MAIN_KEYBOARD,
        )

    # Сохраняем показанные в seen
    seen = load_seen(uid)
    for item in batch:
        seen.add(item.get("url", ""))
    save_seen(uid, seen)


async def do_search_for_user(uid: int, reply_to):
    s = load_settings(uid)
    if not s.get("region"):
        await reply_to.answer("Сначала настрой поиск: /start")
        return

    region = s["region"]
    pmin = s.get("price_min", 0)
    pmax = s.get("price_max", 99_000_000)
    region_name = REGIONS.get(region, region)
    enabled_sources = _get_enabled_sources(s)

    src_labels = " ".join(SOURCE_TAGS.get(src, src) for src in enabled_sources)
    await reply_to.answer(f"🔍 Ищу в {region_name} ({pmin:,}–{pmax:,} ₽)\n{src_labels}")

    skipped = load_skipped(uid)
    loop = asyncio.get_event_loop()

    scraper_map = {
        "drom":   lambda: scrape_drom(region, pages=8, price_min=pmin, price_max=pmax),
        "autoru": lambda: scrape_autoru(region, pages=4, price_min=pmin, price_max=pmax),
        "avito":  lambda: scrape_avito(region, pages=5, price_min=pmin, price_max=pmax),
    }
    tasks = [loop.run_in_executor(None, scraper_map[src]) for src in enabled_sources if src in scraper_map]
    results = await asyncio.gather(*tasks)

    items = []
    stat_parts = []
    for src, batch in zip([s for s in enabled_sources if s in scraper_map], results):
        items.extend(batch)
        if batch:
            stat_parts.append(f"{SOURCE_TAGS.get(src, src)}: {len(batch)}")

    if stat_parts:
        await reply_to.answer("📊 " + " | ".join(stat_parts))

    dealer_count = sum(1 for i in items if is_dealer(i))
    price_count = sum(1 for i in items if not is_dealer(i) and not in_price_range(i, pmin, pmax))
    print(f"  [поиск] всего={len(items)}, дилеров={dealer_count}, вне бюджета={price_count}")
    # Дедупликация по URL
    seen_u: set[str] = set()
    deduped: list[dict] = []
    for i in items:
        u = i.get("url", "")
        if u and u not in seen_u:
            seen_u.add(u)
            deduped.append(i)
    items = deduped

    def _budget_ok(item: dict) -> bool:
        """Фильтр: машины без цены пропускаем только если год соответствует бюджету."""
        pi = item.get("_price_int", 0)
        if pi > 0:
            return True  # цена известна — пусть in_price_range решает
        year_m = re.search(r'\b(20\d{2})\b', item.get("title", ""))
        if not year_m:
            return True  # год неизвестен, пропускаем
        year = int(year_m.group(1))
        # При бюджете < 500к: только машины до 2015 года без цены пропускаем
        if pmax < 500_000 and year >= 2015:
            return False
        # При бюджете < 1.5М: машины 2022+ без цены = скорее всего дилер
        if pmax < 1_500_000 and year >= 2022:
            return False
        return True

    suitable = [
        i for i in items
        if not is_dealer(i)
        and _budget_ok(i)
        and in_price_range(i, pmin, pmax)
        and i.get("url")
        and i["url"] not in skipped
    ]
    suitable = rank_by_market_price(suitable)
    # Сортировка: сначала самые выгодные (максимальная скидка от рынка),
    # потом по горячим ключевым словам, потом по цене
    suitable.sort(key=lambda x: (
        -x.get("_savings_pct", 0),       # скидка от рынка (больше = лучше)
        -x.get("_hot_score", 0),          # срочность/горячесть
        x.get("_price_int", 999_999_999)  # цена (дешевле = лучше)
    ))

    if not suitable:
        await reply_to.answer(
            f"😔 Не нашёл частников в {region_name} по твоему бюджету.\n\n"
            f"Попробуй расширить диапазон цен: /settings",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    _search_cache[uid] = suitable
    _save_cache(uid, suitable)
    await reply_to.answer(f"✅ Найдено {len(suitable)} актуальных объявлений!")
    await send_batch(reply_to.chat.id, uid, 0)


@dp.callback_query(F.data.startswith("page|"))
async def cb_page(cb: CallbackQuery):
    _, uid_s, offset_s = cb.data.split("|")
    uid = int(uid_s)
    offset = int(offset_s)
    await cb.answer()
    await send_batch(cb.message.chat.id, uid, offset)


@dp.callback_query(F.data.startswith("hide|"))
async def cb_hide(cb: CallbackQuery):
    parts = cb.data.split("|")
    sid = parts[1]
    uid = int(parts[2]) if len(parts) > 2 else cb.from_user.id
    url = id_to_url(sid)
    skipped = load_skipped(uid)
    skipped.add(url)
    save_skipped(uid, skipped)
    await cb.answer("Скрыто")
    await cb.message.delete()


@dp.callback_query(F.data.startswith("fav|"))
async def cb_fav(cb: CallbackQuery):
    parts = cb.data.split("|")
    sid = parts[1]
    uid = int(parts[2]) if len(parts) > 2 else cb.from_user.id
    # Сохраняем в избранное (файл favorites.json)
    fav_file = user_dir(uid) / "favorites.json"
    favs = json.loads(fav_file.read_text(encoding="utf-8")) if fav_file.exists() else []
    url = id_to_url(sid)
    items = _search_cache.get(uid) or _load_cache(uid)
    item = next((it for it in items if it.get("url") == url), None)
    if item and url not in [f.get("url") for f in favs]:
        favs.append(item)
        fav_file.write_text(json.dumps(favs, ensure_ascii=False, default=str), encoding="utf-8")
        await cb.answer("⭐ Добавлено в избранное!")
    else:
        await cb.answer("Уже в избранном")


@dp.callback_query(F.data.startswith("sim|"))
async def cb_similar(cb: CallbackQuery):
    parts = cb.data.split("|")
    sid = parts[1]
    uid = int(parts[2]) if len(parts) > 2 else cb.from_user.id
    url = id_to_url(sid)
    items = _search_cache.get(uid) or _load_cache(uid)
    item = next((it for it in items if it.get("url") == url), None)
    if not item:
        await cb.answer("Объявление не найдено")
        return
    key = _car_group_key(item.get("title", ""))
    similar = [it for it in items if _car_group_key(it.get("title", "")) == key and it.get("url") != url]
    if not similar:
        await cb.answer("Похожих объявлений не найдено")
        return
    await cb.answer(f"Найдено похожих: {len(similar)}")
    lines = []
    for it in similar[:5]:
        lines.append(f"• {it.get('title','')} — {it.get('price','?')} [{it.get('source','')}]")
        lines.append(f"  {it.get('url','')}")
    await cb.message.answer("🔍 Похожие объявления:\n\n" + "\n".join(lines))


@dp.message(Command("favorites"))
@dp.message(F.text == "⭐ Избранное")
async def cmd_favorites(msg: Message):
    uid = msg.from_user.id
    fav_file = user_dir(uid) / "favorites.json"
    if not fav_file.exists():
        await msg.answer("⭐ У тебя пока нет сохранённых объявлений.")
        return
    favs = json.loads(fav_file.read_text(encoding="utf-8"))
    if not favs:
        await msg.answer("⭐ Список избранного пуст.")
        return
    lines = []
    for it in favs[-20:]:
        lines.append(f"• {it.get('title','')} — {it.get('price','?')}\n  {it.get('url','')}")
    await msg.answer(f"⭐ Избранное ({len(favs)} шт.):\n\n" + "\n\n".join(lines[-10:]))


@dp.message(Command("test_avito"))
async def cmd_test_avito(msg: Message):
    """Диагностика Авито — присылает что именно возвращает ScraperAPI."""
    import requests as _req
    uid = msg.from_user.id
    s = load_settings(uid)
    region = s.get("region", "chelyabinsk")
    slug = AVITO_SLUGS.get(region, region)
    url = f"https://www.avito.ru/{slug}/avtomobili"

    await msg.answer(f"🔬 Тестирую Авито для {REGIONS.get(region, region)}...\nURL: {url}")

    def _stat(r, name: str) -> str:
        t = r.text
        has_items = 'data-marker="item"' in t
        has_nd = '__NEXT_DATA__' in t
        has_block = "captcha" in t.lower() or "Доступ ограничен" in t or "blocked" in t.lower()
        links = len(re.findall(rf'href="/{re.escape(slug)}/[a-z0-9_/-]+-\d{{5,}}"', t))
        return (
            f"📡 {name}:\n"
            f"HTTP {r.status_code} | {len(t):,} байт\n"
            f"data-marker: {'ДА ✅' if has_items else 'нет ❌'}\n"
            f"__NEXT_DATA__: {'ДА ✅' if has_nd else 'нет ❌'}\n"
            f"ссылки на авто: {links} шт\n"
            f"блокировка: {'ДА ⛔' if has_block else 'нет'}"
        )

    try:
        r_direct = _req.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 Mobile Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }, timeout=15)
        await msg.answer(_stat(r_direct, "Прямой запрос"))
    except Exception as e:
        await msg.answer(f"Прямой запрос ошибка: {str(e)[:200]}")

    if not SCRAPER_API_KEY:
        await msg.answer("SCRAPER_API_KEY не задан!")
        return

    try:
        await msg.answer("Пробую ScraperAPI без render...")
        r1 = _req.get("http://api.scraperapi.com", params={
            "api_key": SCRAPER_API_KEY, "url": url, "country_code": "ru",
        }, timeout=30)
        await msg.answer(_stat(r1, "ScraperAPI без render"))
    except Exception as e:
        await msg.answer(f"ScraperAPI без render ошибка: {str(e)[:200]}")

    try:
        await msg.answer("Пробую ScraperAPI render=true + wait=5000 (до 90 сек)...")
        r2 = _req.get("http://api.scraperapi.com", params={
            "api_key": SCRAPER_API_KEY, "url": url,
            "render": "true", "wait": "5000", "country_code": "ru",
        }, timeout=120)
        await msg.answer(_stat(r2, "ScraperAPI render=true"))
    except Exception as e:
        await msg.answer(f"ScraperAPI render ошибка: {str(e)[:200]}")

    await msg.answer("✅ Диагностика завершена. Пришли эти результаты разработчику.")

async def cmd_help(msg: Message):
    await msg.answer(
        "🤖 *Авто-брокер — поиск авто ниже рынка*\n\n"
        "Команды:\n"
        "/start — начало работы\n"
        "/search — найти авто по твоим настройкам\n"
        "/monitor — авто-мониторинг новых авто ниже рынка (вкл/выкл)\n"
        "/favorites — сохранённые объявления\n"
        "/settings — изменить регион и бюджет\n"
        "/help — помощь\n\n"
        "🔥 — объявления с признаками срочной продажи (торг, срочно, уступлю)\n"
        "⭐ — объявления давно висят — продавец мотивирован",
        parse_mode="Markdown"
    )


async def _monitor_loop(uid: int):
    """Фоновая задача одного пользователя — делегирует в глобальный монитор."""
    # Просто держим флаг, глобальный монитор сам опрашивает всех активных
    while True:
        await asyncio.sleep(3600)


# ── Глобальный монитор — один цикл на всех пользователей ─────────
GLOBAL_POLL_SEC = 120   # опрос каждые 2 минуты

async def _send_monitor_item(uid: int, it: dict):
    """Отправляет одно объявление пользователю из монитора."""
    url = it.get("url", "")
    sid = url_to_id(url)
    pct = it.get("_savings_pct", 0)
    market = it.get("_market_price", 0)
    price_line = it.get("price", "—") or "—"
    if market:
        price_line += f"  🔻 рынок ~{market:,} ₽ (-{pct}%)".replace(",", " ")
    caption = (
        f"🔔 {it.get('title', '')}\n"
        f"💰 {price_line}\n"
        f"📅 только что на Авито"
    )
    if it.get("description"):
        caption += f"\n📝 {it['description'][:250]}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔗 Открыть", url=url),
            InlineKeyboardButton(text="⭐ Сохранить", callback_data=f"fav|{sid}|{uid}"),
        ],
        [
            InlineKeyboardButton(text="❌ Скрыть", callback_data=f"hide|{sid}|{uid}"),
        ],
    ])
    photo_url = it.get("_photo_url", "")
    sent = False
    if photo_url:
        try:
            await bot.send_photo(uid, photo=photo_url, caption=caption, reply_markup=kb)
            sent = True
        except Exception:
            pass
        if not sent:
            try:
                import requests as _req
                from aiogram.types import BufferedInputFile
                resp = _req.get(photo_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code == 200 and len(resp.content) > 2000:
                    await bot.send_photo(uid, photo=BufferedInputFile(resp.content, "photo.jpg"), caption=caption, reply_markup=kb)
                    sent = True
            except Exception:
                pass
    if not sent:
        await bot.send_message(uid, caption, reply_markup=kb)


async def _global_monitor_loop():
    """Единый глобальный цикл — раз в 2 минуты опрашивает Авито для всех активных пользователей."""
    print("  [глоб.монитор] запущен")
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(GLOBAL_POLL_SEC)
        try:
            # Собираем всех пользователей с включённым мониторингом
            if not USERS_DIR.exists():
                continue
            active_users: list[dict] = []
            for user_path in USERS_DIR.iterdir():
                if not (user_path.is_dir() and user_path.name.isdigit()):
                    continue
                try:
                    sf = user_path / "settings.json"
                    if not sf.exists():
                        continue
                    s = json.loads(sf.read_text(encoding="utf-8"))
                    if s.get("monitor_enabled") and s.get("region"):
                        active_users.append({"uid": int(user_path.name), **s})
                except Exception:
                    pass

            if not active_users:
                continue

            # Группируем по региону — один запрос на регион
            by_region: dict[str, list[dict]] = {}
            for u in active_users:
                by_region.setdefault(u["region"], []).append(u)

            for region, users in by_region.items():
                try:
                    # Скрапим Авито по дате (новые сверху), 1 страница — достаточно для свежих
                    raw = await loop.run_in_executor(
                        None,
                        lambda r=region: scrape_avito(r, pages=1, sort_by_date=True)
                    )
                    if not raw:
                        continue

                    # Для каждого пользователя фильтруем индивидуально
                    for u in users:
                        uid = u["uid"]
                        pmin = u.get("price_min", 0)
                        pmax = u.get("price_max", 99_000_000)
                        min_pct = u.get("monitor_min_savings_pct", MONITOR_MIN_SAVINGS_PCT)

                        seen = load_seen(uid)
                        skipped = load_skipped(uid)

                        new_items = [
                            it for it in raw
                            if it.get("url")
                            and it["url"] not in seen
                            and it["url"] not in skipped
                            and not is_dealer(it)
                            and in_price_range(it, pmin, pmax)
                        ]
                        if not new_items:
                            continue

                        # Считаем рыночную цену
                        cached = _search_cache.get(uid) or _load_cache(uid)
                        pool = rank_by_market_price(cached + new_items)
                        new_urls = {x["url"] for x in new_items}

                        new_below = sorted(
                            [it for it in pool
                             if it.get("url") in new_urls
                             and it.get("_below_market")
                             and it.get("_savings_pct", 0) >= min_pct],
                            key=lambda x: -x.get("_savings_pct", 0)
                        )
                        if not new_below:
                            # Обновляем seen даже без выгодных — чтобы не дублировать
                            seen.update(it["url"] for it in new_items)
                            save_seen(uid, seen)
                            continue

                        region_name = REGIONS.get(region, region)
                        print(f"  [монитор] uid={uid} регион={region_name}: {len(new_below)} новых выгодных")

                        # Шапка-уведомление
                        await bot.send_message(
                            uid,
                            f"🔔 *{region_name}* — {len(new_below)} новых авто ниже рынка!",
                            parse_mode="Markdown",
                        )
                        # Шлём каждое объявление (максимум 5)
                        for it in new_below[:5]:
                            await _send_monitor_item(uid, it)
                            await asyncio.sleep(0.3)

                        seen.update(it["url"] for it in new_items)
                        save_seen(uid, seen)

                except Exception as e:
                    print(f"  [глоб.монитор] регион={region}: {e}")

        except Exception as e:
            print(f"  [глоб.монитор] ошибка цикла: {e}")


def _start_monitor(uid: int):
    # Глобальный монитор уже запущен в main(), здесь просто сохраняем задачу-заглушку
    if uid not in _monitor_tasks or _monitor_tasks[uid].done():
        task = asyncio.get_event_loop().create_task(_monitor_loop(uid))
        _monitor_tasks[uid] = task


def _stop_monitor(uid: int):
    task = _monitor_tasks.pop(uid, None)
    if task and not task.done():
        task.cancel()


@dp.message(Command("monitor"))
@dp.message(F.text == "🔔 Уведомления")
async def cmd_monitor(msg: Message):
    """Включить/выключить автомониторинг новых объявлений ниже рынка."""
    uid = msg.from_user.id
    s = load_settings(uid)
    if not s.get("region"):
        await msg.answer("Сначала настрой регион и бюджет: /start")
        return

    enabled = s.get("monitor_enabled", False)
    if enabled:
        # Выключаем
        s["monitor_enabled"] = False
        save_settings(uid, s)
        _stop_monitor(uid)
        await msg.answer(
            "🔕 Автомониторинг выключен.\n\n"
            "Напиши /monitor чтобы снова включить."
        )
    else:
        # Включаем
        s["monitor_enabled"] = True
        save_settings(uid, s)
        _start_monitor(uid)
        region_name = REGIONS.get(s["region"], s["region"])
        pmin = s.get("price_min", 0)
        pmax = s.get("price_max", 99_000_000)
        await msg.answer(
            f"✅ *Автомониторинг включён!*\n\n"
            f"🔔 Буду проверять Авито каждые 2 минуты.\n"
            f"Регион: {region_name}\n"
            f"Бюджет: {pmin:,}–{pmax:,} ₽\n"
            f"Показываю только авто на 10%+ ниже рынка.\n\n"
            f"Напиши /monitor снова чтобы выключить.",
            parse_mode="Markdown",
        )


async def main():
    logging.basicConfig(level=logging.WARNING)
    print("✅ Авто-брокер бот запущен!")

    # Единый глобальный монитор — опрашивает всех активных пользователей каждые 2 минуты
    asyncio.get_event_loop().create_task(_global_monitor_loop())
    print(f"  [монитор] глобальный цикл запущен (интервал {GLOBAL_POLL_SEC}с)")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
