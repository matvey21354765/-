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
    "ооо", "зао", "пао", "автосалон", "официальный дилер", "дилер",
    "автоцентр", "trade-in", "трейд-ин", "автохолдинг",
    "рольф", "major", "колёса даром", "автопланета", "автоград",
    "июль", "автоленд", "favorit", "фаворит", "авто плюс", "автоплюс",
    "fresh auto", "автобан", "автосфера", "арконт", "ключавто", "авилон",
    "петровский", "прагматика", "бизнес кар", "максимум авто", "мотус",
    "genser", "генсер", "ац урал", "восток авто", "сити авто",
    "кредит от", "автоподбор", "выкуп авто", "автовыкуп",
    "срочный выкуп", "выкупаем", "лизинг", "рассрочка от",
    "наш автосалон", "купить в кредит", "тест-драйв",
    "гарантия на автомобиль", "официальная гарантия",
]

HOT_WORDS = re.compile(
    r"(срочно|торг|уступлю|снижу|скидка|дёшево|дешево|продам быстро|срочная продажа)",
    re.IGNORECASE,
)

MONTHS = {
    "янв":1,"фев":2,"мар":3,"апр":4,"май":5,"мая":5,
    "июн":6,"июл":7,"авг":8,"сен":9,"окт":10,"ноя":11,"дек":12,
}

# ── Пользователи ────────────────────────────────────────────────
USERS_DIR = Path("users")
USERS_DIR.mkdir(exist_ok=True)


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
    p = parse_price(item.get("price", ""))
    if p is None:
        return True
    return price_min <= p <= price_max


def hot_score(item: dict) -> float:
    """Оценка привлекательности: ниже рынка = выше."""
    title = item.get("title", "")
    photos = item.get("_photos", 0)
    days = item.get("_days_on_site", 0)
    score = 0.0
    if HOT_WORDS.search(title):
        score += 15.0
    # Мало фото = меньше уверенности = возможно срочная продажа
    if photos == 0:
        score += 3.0
    # Давно висит = мотивированный продавец
    score += min(days, 30) * 0.5
    return round(score, 2)


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

                    img_el = card.select_one("img[data-src]") or card.select_one("img[src]")
                    photo_url = ""
                    if img_el:
                        src = img_el.get("data-src") or img_el.get("src", "")
                        if src and src.startswith("http") and "drom" in src:
                            photo_url = src

                    if title and item_url:
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
                        }
                        item["_hot_score"] = hot_score(item)
                        results.append(item)
                except Exception:
                    pass

            time.sleep(random.uniform(1, 2))
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
                item = {
                    "source": "autoru", "title": title, "price": price_str,
                    "url": item_url, "date": str(today - datetime.timedelta(days=days)),
                    "_photos": len(photos_list), "_days_on_site": days,
                    "description": desc, "seller": "", "_photo_url": photo_url,
                }
                item["_hot_score"] = hot_score(item)
                results.append(item)
        except Exception:
            pass
    return results


def scrape_autoru(region: str, pages: int = 5, price_min: int = 0, price_max: int = 99_000_000) -> list[dict]:
    slug = AUTORU_SLUGS.get(region, region)
    geo_ids = AUTORU_GEO_IDS.get(region, [])
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
        "Accept": "application/json,text/html,*/*",
        "Referer": "https://auto.ru/",
        "x-client-app": "autoru-frontend-application",
        "x-requested-with": "fetch",
    })

    # Сначала пробуем JSON API (быстро и надёжно)
    api_url = "https://auto.ru/-/ajax/desktop/listing/"
    for p in range(1, pages + 1):
        body: dict = {
            "category": "cars",
            "section": "used",
            "seller_type": ["PRIVATE"],
            "page": p,
            "page_size": 37,
            "sort": "fresh_relevance_1-desc",
        }
        if geo_ids:
            body["geo_id"] = geo_ids
        if price_min > 0:
            body["price_from"] = price_min
        if price_max < 99_000_000:
            body["price_to"] = price_max
        try:
            r = session.post(api_url, json=body, timeout=20)
            if r.status_code == 200:
                try:
                    data = r.json()
                    batch = _autoru_parse_offers(data, today)
                    if batch:
                        results.extend(batch)
                        time.sleep(random.uniform(0.5, 1))
                        continue
                except Exception:
                    pass
            # Fallback: ScraperAPI + HTML
            if SCRAPER_API_KEY:
                url = f"https://auto.ru/{slug}/cars/used/?seller_group=PRIVATE&page={p}"
                if price_min > 0:
                    url += f"&price_from={price_min}"
                if price_max < 99_000_000:
                    url += f"&price_to={price_max}"
                r2 = _req.get("http://api.scraperapi.com", params={
                    "api_key": SCRAPER_API_KEY, "url": url, "country_code": "ru",
                }, timeout=30)
                if r2.status_code == 200:
                    # Ищем __INITIAL_STATE__ в HTML
                    text = r2.text
                    idx = text.find("window.__INITIAL_STATE__")
                    if idx != -1:
                        brace_start = text.find("{", idx)
                        if brace_start != -1:
                            # Найдём конец объекта по скрипт-тегу
                            script_end = text.find("</script>", brace_start)
                            json_str = text[brace_start:script_end].rstrip("; \n\r")
                            try:
                                data = json.loads(json_str)
                                batch = _autoru_parse_offers(data, today)
                                results.extend(batch)
                            except Exception:
                                pass
            break
        except Exception as e:
            print(f"  [Auto.ru {region}] стр.{p}: {e}")
            break

    print(f"  [Auto.ru] {len(results)} объявлений")
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
                        item = {
                            "source": "kolesa", "title": title, "price": price,
                            "url": item_url, "date": str(date_obj) if date_obj else date_text,
                            "_photos": 0, "_days_on_site": days,
                            "description": desc, "seller": "", "_photo_url": photo_url,
                        }
                        item["_hot_score"] = hot_score(item)
                        results.append(item)
                except Exception:
                    pass

            time.sleep(random.uniform(1, 2))
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
                        item = {
                            "source": "bibika", "title": title, "price": price,
                            "url": item_url, "date": str(date) if date else date_text,
                            "_photos": 0, "_days_on_site": days,
                            "description": desc, "seller": "", "_photo_url": "",
                        }
                        item["_hot_score"] = hot_score(item)
                        results.append(item)
                except Exception:
                    pass

            time.sleep(random.uniform(1, 2))
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


def _avito_item_from_json(it: dict, today) -> dict | None:
    """Преобразует один объект из JSON Авито в dict объявления."""
    try:
        title = it.get("title", "")
        url_path = it.get("urlPath") or it.get("url", "")
        if not url_path:
            return None
        item_url = ("https://www.avito.ru" + url_path) if url_path.startswith("/") else url_path
        if not title or "avito.ru" not in item_url:
            return None

        price_info = it.get("priceDetailed") or it.get("price") or {}
        if isinstance(price_info, dict):
            price_val = price_info.get("value") or price_info.get("number") or 0
            price = f"{int(price_val):,} ₽".replace(",", " ") if price_val else ""
        else:
            price = str(price_info) if price_info else ""

        images = it.get("images") or it.get("gallery", {}).get("images", []) or []
        photo_url = ""
        if images and isinstance(images, list):
            img = images[0]
            if isinstance(img, dict):
                photo_url = (img.get("864x648") or img.get("640x480") or
                             img.get("320x240") or img.get("url") or
                             next(iter(img.values()), ""))
            elif isinstance(img, str):
                photo_url = img
        if photo_url and photo_url.startswith("//"):
            photo_url = "https:" + photo_url

        item = {
            "source": "avito", "title": title,
            "price": price, "url": item_url, "date": str(today),
            "_photos": len(images), "_days_on_site": 0,
            "description": (it.get("description") or "")[:300],
            "seller": "", "_photo_url": photo_url,
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
        if len(obj) >= 3 and isinstance(obj[0], dict):
            sample = obj[0]
            if ("title" in sample or "urlPath" in sample) and ("price" in sample or "priceDetailed" in sample):
                return obj
        for x in obj:
            r = _avito_find_items_in_json(x, depth + 1)
            if r:
                return r
        return []
    if isinstance(obj, dict):
        for key in ("items", "catalog", "listing", "offers", "data"):
            val = obj.get(key)
            if isinstance(val, list) and len(val) >= 3:
                sample = val[0] if val else {}
                if isinstance(sample, dict) and ("title" in sample or "urlPath" in sample):
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

            img_el = card.select_one("img[src]") or card.select_one("img[data-src]")
            photo_url = ""
            if img_el:
                src = img_el.get("src") or img_el.get("data-src") or ""
                if src.startswith("http"):
                    photo_url = src

            if title:
                item = {
                    "source": "avito", "title": title, "price": price,
                    "url": item_url, "date": str(today),
                    "_photos": 0, "_days_on_site": 0,
                    "description": "", "seller": "", "_photo_url": photo_url,
                }
                item["_hot_score"] = hot_score(item)
                results.append(item)
        except Exception:
            pass

    if results:
        return results

    # 3. Последний шанс: извлекаем ссылки на объявления из HTML (href-паттерн)
    urls = _avito_extract_links(text, slug)
    print(f"  [Авито] href-ссылки: {len(urls)}")
    for item_url in urls[:50]:
        item = {
            "source": "avito", "title": "Авто на Авито", "price": "",
            "url": item_url, "date": str(today),
            "_photos": 0, "_days_on_site": 0,
            "description": "", "seller": "", "_photo_url": "",
        }
        item["_hot_score"] = hot_score(item)
        results.append(item)

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
            time.sleep(random.uniform(1.5, 2.5))
        except Exception as e:
            print(f"  [Авито прямой] стр.{p}: {e}")
            break

    return results


def scrape_avito(region: str, pages: int = 5, price_min: int = 0, price_max: int = 99_000_000) -> list[dict]:
    slug = AVITO_SLUGS.get(region, region)
    today = datetime.date.today()

    # 1. Прямой запрос (иногда работает с мобильным UA)
    direct = _scrape_avito_direct(slug, 1, price_min, price_max, today)
    if direct and len(direct) >= 5:
        print(f"  [Авито прямой] {len(direct)} объявлений")
        # Берём больше страниц
        for p in range(2, pages + 1):
            try:
                import requests as _req
                session = _req.Session()
                session.headers.update({
                    "User-Agent": "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 Mobile Safari/537.36",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                })
                url = f"https://www.avito.ru/{slug}/avtomobili"
                params: dict = {"p": p}
                if price_min > 0:
                    params["pmin"] = price_min
                if price_max < 99_000_000:
                    params["pmax"] = price_max
                r = session.get(url, params=params, timeout=20)
                batch = _parse_avito_html(r.text, slug, today)
                if not batch:
                    break
                direct.extend(batch)
                time.sleep(random.uniform(1, 2))
            except Exception:
                break
        print(f"  [Авито прямой итого] {len(direct)} объявлений")
        return direct

    # 2. ScraperAPI с render=true + wait=5000
    if SCRAPER_API_KEY:
        results = []
        for p in range(1, pages + 1):
            url = f"https://www.avito.ru/{slug}/avtomobili?p={p}"
            if price_min > 0:
                url += f"&pmin={price_min}"
            if price_max < 99_000_000:
                url += f"&pmax={price_max}"

            r = _avito_scraperapi(url)
            if r is None or r.status_code != 200:
                print(f"  [Авито ScraperAPI] стр.{p}: HTTP {r.status_code if r else 'err'}")
                break

            status = r.status_code
            text = r.text
            has_items = 'data-marker="item"' in text
            has_nd = "__NEXT_DATA__" in text
            print(f"  [Авито ScraperAPI] стр.{p}: HTTP {status} {len(text)}б, items={has_items} next={has_nd}")
            # Выводим кусок HTML для диагностики на первой странице
            if p == 1:
                snippet = text[:300].replace("\n", " ")
                print(f"  [Авито HTML начало]: {snippet}")

            batch = _parse_avito_html(text, slug, today)
            if not batch:
                break
            results.extend(batch)
            time.sleep(random.uniform(2, 3))

        if results:
            print(f"  [Авито ScraperAPI итого] {len(results)} объявлений")
            return results

    print(f"  [Авито] 0 объявлений — все методы исчерпаны")
    return []



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
        await msg.answer(
            f"👋 Привет! Твои настройки:\n"
            f"📍 Регион: {region_name}\n"
            f"💰 Бюджет: {pmin:,} – {pmax:,} ₽\n\n"
            f"/search — найти объявления\n"
            f"/settings — изменить настройки",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Найти авто", callback_data="do_search")],
                [InlineKeyboardButton(text="⚙️ Изменить настройки", callback_data="change_settings")],
            ])
        )
    else:
        await msg.answer(
            "👋 Привет! Я ищу автомобили от частных лиц по цене ниже рынка.\n\n"
            "Для начала выбери регион поиска:"
        )
        await msg.answer("📍 Выбери город:", reply_markup=region_keyboard())
        await state.set_state(Setup.region)


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
async def cmd_settings(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("📍 Выбери город:", reply_markup=region_keyboard())
    await state.set_state(Setup.region)


ALL_SOURCES = ["drom", "autoru", "kolesa", "bibika", "avito"]
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


SOURCE_TAGS = {
    "autoru": "🟠 Auto.ru",
    "kolesa": "🟢 Kolesa",
    "bibika": "🟣 Bibika",
    "avito":  "🔴 Авито",
    "drom":   "🔵 Дром",
}

# Кеш результатов поиска: uid -> list[dict]
_search_cache: dict[int, list[dict]] = {}


async def send_batch(chat_id: int, uid: int, offset: int):
    """Отправляет 10 объявлений из кеша начиная с offset."""
    items = _search_cache.get(uid, [])
    if not items or offset >= len(items):
        await bot.send_message(chat_id, "✅ Объявления закончились. Нажми /search для нового поиска.")
        return

    batch = items[offset:offset + 10]
    # Подгружаем фото/описание для объявлений из второй страницы и далее
    to_enrich = [i for i in batch if not i.get("_enriched")]
    if to_enrich:
        loop = asyncio.get_event_loop()
        details_list = await asyncio.gather(
            *[loop.run_in_executor(None, _fetch_and_check, i["url"], i.get("source", "")) for i in to_enrich]
        )
        ei = 0
        for i, item in enumerate(batch):
            if not item.get("_enriched"):
                d = details_list[ei] or {}
                item["_enriched"] = True
                if d.get("_photo_url"):
                    item["_photo_url"] = d["_photo_url"]
                if d.get("description"):
                    item["description"] = d["description"]
                ei += 1

    total = len(items)
    for item in batch:
        url = item.get("url", "")
        sid = url_to_id(url)
        days = item.get("_days_on_site", 0)
        days_str = "сегодня" if days == 0 else f"{days} дн. назад"
        score = item.get("_hot_score", 0)
        hot_tag = " 🔥" if score >= 15 else " ⭐" if score >= 5 else ""
        source_tag = SOURCE_TAGS.get(item.get("source", ""), "🔵")

        caption = (
            f"{source_tag} {item.get('title', '')}{hot_tag}\n"
            f"💰 {item.get('price', '—')}\n"
            f"📅 {days_str}"
        )
        if item.get("description"):
            caption += f"\n\n📝 {item['description'][:500]}"

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔗 Открыть", url=url),
            InlineKeyboardButton(text="❌ Скрыть", callback_data=f"hide|{sid}|{uid}"),
        ]])

        photo_url = item.get("_photo_url", "")
        if photo_url:
            try:
                await bot.send_photo(chat_id, photo=photo_url, caption=caption, reply_markup=kb)
                continue
            except Exception:
                pass
        await bot.send_message(chat_id, caption, reply_markup=kb)

    next_offset = offset + 10
    if next_offset < total:
        await bot.send_message(
            chat_id,
            f"Показано {min(next_offset, total)} из {total}. Листай дальше:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"➡️ Ещё 10 объявлений", callback_data=f"page|{uid}|{next_offset}"),
            ]])
        )
    else:
        await bot.send_message(chat_id, f"✅ Показаны все {total} объявлений. /search — новый поиск.")

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
        "drom":   lambda: scrape_drom(region, pages=20, price_min=pmin, price_max=pmax),
        "autoru": lambda: scrape_autoru(region, pages=10, price_min=pmin, price_max=pmax),
        "kolesa": lambda: scrape_kolesa(region, pages=10, price_min=pmin, price_max=pmax),
        "bibika": lambda: scrape_bibika(region, pages=5, price_min=pmin, price_max=pmax),
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

    suitable = [
        i for i in items
        if not is_dealer(i)
        and in_price_range(i, pmin, pmax)
        and i.get("url")
        and i["url"] not in skipped
    ]
    suitable.sort(key=lambda x: (-x.get("_hot_score", 0), x.get("_days_on_site", 999)))

    if not suitable:
        await reply_to.answer(
            f"😔 Не нашёл частников в {region_name} по твоему бюджету.\n\n"
            f"Попробуй расширить диапазон цен: /settings"
        )
        return

    await reply_to.answer(f"🔎 Проверяю {min(len(suitable), 25)} объявлений и загружаю фото...")
    suitable = await enrich_and_filter(suitable, max_check=25)

    if not suitable:
        await reply_to.answer("😔 Все найденные объявления уже сняты с продажи. Попробуй позже.")
        return

    _search_cache[uid] = suitable
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


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "🤖 *Авто-брокер — поиск авто ниже рынка*\n\n"
        "Команды:\n"
        "/start — начало работы\n"
        "/search — найти авто по твоим настройкам\n"
        "/settings — изменить регион и бюджет\n"
        "/help — помощь\n\n"
        "🔥 — объявления с признаками срочной продажи (торг, срочно, уступлю)\n"
        "⭐ — объявления давно висят — продавец мотивирован",
        parse_mode="Markdown"
    )


async def main():
    logging.basicConfig(level=logging.WARNING)
    print("✅ Авто-брокер бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
