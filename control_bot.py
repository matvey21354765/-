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


# ── Парсер Auto.ru ──────────────────────────────────────────────

def scrape_autoru(region: str, pages: int = 5, price_min: int = 0, price_max: int = 99_000_000) -> list[dict]:
    slug = AUTORU_SLUGS.get(region, region)
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
        try:
            import cloudscraper as _cs
            session = _cs.create_scraper(browser={"browser": "chrome", "platform": "windows"})
        except ImportError:
            session = _req.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9",
            })
    except ImportError:
        return []

    results = []
    today = datetime.date.today()

    for p in range(1, pages + 1):
        params = {
            "seller_group": "PRIVATE",
            "page": p,
        }
        if price_min > 0:
            params["price_from"] = price_min
        if price_max < 99_000_000:
            params["price_to"] = price_max

        url = f"https://auto.ru/{slug}/cars/used/"
        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code != 200:
                break

            # Auto.ru кладёт данные в JSON внутри тега <script>
            m = re.search(
                r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>',
                r.text, re.DOTALL
            )
            if m:
                try:
                    data = json.loads(m.group(1))
                    listing = (
                        data.get("listing", {}).get("data", {}).get("offers", [])
                        or data.get("search", {}).get("offers", {}).get("offers", [])
                    )
                    for offer in listing:
                        try:
                            vehicle = offer.get("vehicle_info", {})
                            mark = vehicle.get("mark_info", {}).get("name", "")
                            model = vehicle.get("model_info", {}).get("name", "")
                            year = offer.get("documents", {}).get("year", "")
                            title = f"{mark} {model} {year}".strip()
                            price = offer.get("price_info", {}).get("price", "")
                            price_str = f"{int(price):,} ₽".replace(",", " ") if price else ""
                            item_url = offer.get("url", "") or f"https://auto.ru/cars/used/sale/{offer.get('id','')}"
                            seller_type = offer.get("seller_type", "")
                            if seller_type == "COMMERCIAL":
                                continue
                            photos = len(offer.get("photos", []))
                            date_str = offer.get("additional_info", {}).get("creation_date", "")
                            days = 0
                            if date_str:
                                try:
                                    dt = datetime.datetime.fromisoformat(date_str[:10]).date()
                                    days = max(0, (today - dt).days)
                                except Exception:
                                    pass
                            desc = offer.get("description", "")[:300]
                            tech = offer.get("vehicle_info", {}).get("tech_param", {})
                            if tech and not desc:
                                engine = tech.get("engine_type", "")
                                hp = tech.get("power", "")
                                gearbox = tech.get("transmission", "")
                                parts = [p for p in [engine, f"{hp} л.с." if hp else "", gearbox] if p]
                                desc = ", ".join(parts)
                            photo_url = ""
                            photos_list = offer.get("photos", [])
                            if photos_list:
                                p0 = photos_list[0]
                                sizes = p0.get("sizes", {})
                                photo_url = sizes.get("1200x900", sizes.get("832x624", sizes.get("456x342", "")))
                            if title and item_url:
                                item = {
                                    "source": "autoru",
                                    "title": title,
                                    "price": price_str,
                                    "url": item_url,
                                    "date": str(today - datetime.timedelta(days=days)),
                                    "_photos": photos,
                                    "_days_on_site": days,
                                    "description": desc,
                                    "seller": "",
                                    "_photo_url": photo_url,
                                }
                                item["_hot_score"] = hot_score(item)
                                results.append(item)
                        except Exception:
                            pass
                    if listing:
                        time.sleep(random.uniform(1, 2))
                        continue
                except Exception:
                    pass

            # Fallback: HTML парсинг
            soup = _BS(r.text, "lxml")
            cards = soup.select("div[class*='ListingItem']") or soup.select("article[class*='listing-item']")
            if not cards:
                break

            for card in cards:
                try:
                    title_el = card.select_one("a[class*='title']") or card.select_one("h3")
                    title = title_el.get_text(strip=True) if title_el else ""
                    href = title_el.get("href", "") if title_el and title_el.name == "a" else ""
                    if not href:
                        link = card.select_one("a[href*='/cars/']")
                        href = link.get("href", "") if link else ""
                    item_url = href if href.startswith("http") else ("https://auto.ru" + href)

                    price_el = card.select_one("[class*='price']")
                    price = price_el.get_text(strip=True) if price_el else ""

                    if title and item_url:
                        item = {
                            "source": "autoru",
                            "title": title,
                            "price": price,
                            "url": item_url,
                            "date": str(today),
                            "_photos": 0,
                            "_days_on_site": 0,
                            "description": "",
                            "seller": "",
                        }
                        item["_hot_score"] = hot_score(item)
                        results.append(item)
                except Exception:
                    pass

            time.sleep(random.uniform(1, 2))
        except Exception as e:
            print(f"  [Auto.ru {region}] стр.{p}: {e}")
            break

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
        from bs4 import BeautifulSoup as _BS
        try:
            import cloudscraper as _cs
            session = _cs.create_scraper(browser={"browser": "chrome", "platform": "windows"})
        except ImportError:
            import requests as _req
            session = _req.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9",
            })
    except ImportError:
        return []

    results = []
    today = datetime.date.today()

    for p in range(1, pages + 1):
        url = f"https://kolesa.ru/cars/"
        params = {
            "city": slug,
            "seller": "private",
            "page": p,
        }
        if price_min > 0:
            params["price_from"] = price_min
        if price_max < 99_000_000:
            params["price_to"] = price_max

        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code != 200:
                break
            soup = _BS(r.text, "lxml")

            cards = (
                soup.select("div.a-list__item")
                or soup.select("[class*='listing-item']")
                or soup.select("article[data-id]")
            )
            if not cards:
                break

            for card in cards:
                try:
                    link = card.select_one("a.a-el-link") or card.select_one("a[href*='/cars/']")
                    title = link.get_text(strip=True) if link else ""
                    href = link.get("href", "") if link else ""
                    item_url = href if href.startswith("http") else ("https://kolesa.ru" + href)

                    price_el = card.select_one(".a-price__number") or card.select_one("[class*='price']")
                    price = price_el.get_text(strip=True) if price_el else ""

                    date_el = card.select_one(".a-info__date") or card.select_one("[class*='date']")
                    date_text = date_el.get_text(strip=True) if date_el else ""
                    date = parse_ru_date(date_text)
                    days = max(0, (today - date).days) if date else 0

                    desc_el = (
                        card.select_one(".a-descr")
                        or card.select_one("[class*='descr']")
                        or card.select_one("[class*='description']")
                        or card.select_one("p")
                    )
                    desc = desc_el.get_text(strip=True)[:300] if desc_el else ""

                    # Характеристики из тегов внутри карточки
                    params_el = card.select_one("[class*='params']") or card.select_one("[class*='spec']")
                    if params_el and not desc:
                        desc = params_el.get_text(separator=" | ", strip=True)[:300]

                    img_el = card.select_one("img[data-src]") or card.select_one("img[src]")
                    photo_url = ""
                    if img_el:
                        src = img_el.get("data-src") or img_el.get("src", "")
                        if src and src.startswith("http"):
                            photo_url = src

                    if title and item_url and "/cars/" in item_url:
                        item = {
                            "source": "kolesa",
                            "title": title,
                            "price": price,
                            "url": item_url,
                            "date": str(date) if date else date_text,
                            "_photos": 0,
                            "_days_on_site": days,
                            "description": desc,
                            "seller": "",
                            "_photo_url": photo_url,
                        }
                        item["_hot_score"] = hot_score(item)
                        results.append(item)
                except Exception:
                    pass

            time.sleep(random.uniform(1, 2))
        except Exception as e:
            print(f"  [Kolesa {region}] стр.{p}: {e}")
            break

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
        from bs4 import BeautifulSoup as _BS
        try:
            import cloudscraper as _cs
            session = _cs.create_scraper(browser={"browser": "chrome", "platform": "windows"})
        except ImportError:
            import requests as _req
            session = _req.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9",
            })
    except ImportError:
        return []

    results = []
    today = datetime.date.today()

    for p in range(1, pages + 1):
        url = f"https://bibika.ru/auto/{slug}/"
        params = {"page": p, "seller": "1"}  # seller=1 — частники
        if price_min > 0:
            params["price_min"] = price_min
        if price_max < 99_000_000:
            params["price_max"] = price_max

        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code != 200:
                break
            soup = _BS(r.text, "lxml")

            cards = soup.select(".auto-item") or soup.select("[class*='auto-item']") or soup.select("div[itemtype*='Product']")
            if not cards:
                break

            for card in cards:
                try:
                    link = card.select_one("a[href*='/auto/']") or card.select_one("h2 a") or card.select_one("h3 a")
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

                    if title and item_url:
                        item = {
                            "source": "bibika",
                            "title": title,
                            "price": price,
                            "url": item_url,
                            "date": str(date) if date else date_text,
                            "_photos": 0,
                            "_days_on_site": days,
                            "description": desc,
                            "seller": "",
                            "_photo_url": "",
                        }
                        item["_hot_score"] = hot_score(item)
                        results.append(item)
                except Exception:
                    pass

            time.sleep(random.uniform(1, 2))
        except Exception as e:
            print(f"  [Bibika {region}] стр.{p}: {e}")
            break

    return results


# ── Парсер Авито ────────────────────────────────────────────────

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


def scrape_avito(region: str, pages: int = 5, price_min: int = 0, price_max: int = 99_000_000) -> list[dict]:
    slug = AVITO_REGION_SLUGS.get(region, AVITO_SLUGS.get(region, region))
    try:
        from bs4 import BeautifulSoup as _BS
        try:
            import cloudscraper as _cs
            session = _cs.create_scraper(browser={"browser": "chrome", "platform": "windows"})
        except ImportError:
            import requests as _req
            session = _req.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Referer": "https://www.avito.ru/",
        })
    except ImportError:
        return []

    results = []
    today = datetime.date.today()

    for p in range(1, pages + 1):
        params = {"p": p, "seller_type": "1"}  # без сортировки по дате — все объявления
        if price_min > 0:
            params["pmin"] = price_min
        if price_max < 99_000_000:
            params["pmax"] = price_max

        url = f"https://www.avito.ru/{slug}/avtomobili"
        try:
            r = session.get(url, params=params, timeout=25)
            if "captcha" in r.text.lower() or "Доступ ограничен" in r.text:
                print(f"  [Авито] капча на стр.{p}")
                break

            soup = _BS(r.text, "lxml")
            cards = soup.select("[data-marker='item']")
            if not cards:
                break

            for card in cards:
                try:
                    title_el = card.select_one("[itemprop='name']") or card.select_one("h3")
                    title = title_el.get_text(strip=True) if title_el else ""

                    link_el = card.select_one(f"a[href*='/{slug}/']") or card.select_one("a[href*='/avtomobili/']")
                    href = link_el.get("href", "") if link_el else ""
                    item_url = ("https://www.avito.ru" + href) if href and href.startswith("/") else href

                    price_el = card.select_one("[itemprop='price']") or card.select_one("[class*='price']")
                    price = ""
                    if price_el:
                        price = price_el.get("content") or price_el.get_text(strip=True)

                    date_el = card.select_one("[data-marker='item-date']") or card.select_one("span[class*='date']")
                    date_text = date_el.get_text(strip=True) if date_el else ""
                    date = parse_ru_date(date_text)
                    days = max(0, (today - date).days) if date else 0

                    # Фото
                    img_el = card.select_one("img[src*='avito']") or card.select_one("img[data-src]")
                    photo_url = ""
                    if img_el:
                        src = img_el.get("src") or img_el.get("data-src", "")
                        if src and src.startswith("http"):
                            photo_url = src

                    desc_el = card.select_one("[class*='description']") or card.select_one("p")
                    desc = desc_el.get_text(strip=True)[:300] if desc_el else ""

                    if title and item_url:
                        item = {
                            "source": "avito",
                            "title": title,
                            "price": price,
                            "url": item_url,
                            "date": str(date) if date else date_text,
                            "_photos": 0,
                            "_days_on_site": days,
                            "description": desc,
                            "seller": "",
                            "_photo_url": photo_url,
                        }
                        item["_hot_score"] = hot_score(item)
                        results.append(item)
                except Exception:
                    pass

            time.sleep(random.uniform(2, 4))
        except Exception as e:
            print(f"  [Авито {region}] стр.{p}: {e}")
            break

    print(f"  [Авито] {len(results)} объявлений")
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
    await cb_change_settings.__wrapped__(
        type("cb", (), {"answer": lambda *a, **kw: None, "message": msg, "from_user": msg.from_user})(),
        state
    )
    await msg.answer("📍 Выбери город:", reply_markup=region_keyboard())
    await state.set_state(Setup.region)


@dp.message(Command("search"))
async def cmd_search(msg: Message):
    await do_search_for_user(msg.from_user.id, msg)


@dp.callback_query(F.data == "do_search")
async def cb_do_search(cb: CallbackQuery):
    await cb.answer()
    await do_search_for_user(cb.from_user.id, cb.message)


# ── Загрузка деталей объявления ─────────────────────────────────

def _fetch_listing_details(url: str, source: str) -> dict:
    """Загружает страницу объявления и вытаскивает фото + описание продавца."""
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }
        r = _req.get(url, headers=headers, timeout=15)
        soup = _BS(r.text, "lxml")

        photo_url = ""
        description = ""

        # og:image — самый надёжный источник фото на всех сайтах
        og = soup.select_one("meta[property='og:image']")
        if og:
            photo_url = og.get("content", "").strip()

        # Если og:image нет — ищем первую img в галерее
        if not photo_url:
            for img in soup.select("img[src]"):
                src = img.get("src", "")
                if src.startswith("http") and any(x in src for x in ["photo", "image", "img", "jpeg", "jpg", "png"]):
                    photo_url = src
                    break

        # Описание продавца — специфично для каждого источника
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
        if desc_el:
            description = desc_el.get_text(strip=True)

        # Убеждаемся что URL абсолютный
        if photo_url and not photo_url.startswith("http"):
            photo_url = "https:" + photo_url if photo_url.startswith("//") else ""

        return {
            "_photo_url": photo_url,
            "description": description[:500] if description else "",
        }
    except Exception:
        return {}


async def enrich_items(items: list[dict]) -> list[dict]:
    """Параллельно загружает фото и описание для каждого объявления."""
    loop = asyncio.get_event_loop()
    details_list = await asyncio.gather(
        *[loop.run_in_executor(None, _fetch_listing_details, i["url"], i.get("source", "")) for i in items]
    )
    for item, details in zip(items, details_list):
        if details.get("_photo_url"):
            item["_photo_url"] = details["_photo_url"]
        if details.get("description"):
            item["description"] = details["description"]
    return items


# Фразы которые означают что объявление снято
_REMOVED_MARKERS_TEXT = [
    "снят с продажи", "снято с продажи", "объявление снято",
    "объявление не найдено", "объявление недоступно", "объявление удалено",
    "продажа завершена", "не существует", "страница не найдена",
    "listing not found", "offer not found", "объявление архивировано",
    "объявление заблокировано", "sold out", "is sold",
    # Дром — специфичные маркеры в статичном HTML
    '"isSold":true', '"sold":true', '"status":"sold"', '"status":"inactive"',
    'data-bulletin-status="sold"', 'bulletin-sold', '"isArchived":true',
]

_REMOVED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def _check_url_active(url: str) -> bool:
    """Возвращает True если объявление ещё активно."""
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
        r = _req.get(url, headers=_REMOVED_HEADERS, timeout=12, allow_redirects=True)
        if r.status_code in (404, 410):
            return False
        text = r.text
        text_lower = text.lower()

        # Проверяем текстовые маркеры
        if any(m in text_lower for m in _REMOVED_MARKERS_TEXT):
            return False

        # Для Дрома: кнопка "Позвонить" заблокирована у снятых объявлений
        if "drom.ru" in url:
            soup = _BS(text, "lxml")
            # Если нет кнопки звонка или она disabled — снято
            call_btn = soup.select_one("button[data-ftid='bull_header_call-button']")
            if call_btn and call_btn.get("disabled"):
                return False
            # Проверяем JSON в теге script
            for sc in soup.select("script"):
                sc_text = sc.string or ""
                if any(m in sc_text for m in ['"isSold":true', '"sold":true', '"isArchived":true']):
                    return False

        return True
    except Exception:
        return True


async def filter_active(items: list[dict], max_check: int = 30) -> list[dict]:
    """Проверяет до max_check объявлений на актуальность параллельно."""
    loop = asyncio.get_event_loop()
    to_check = items[:max_check]
    rest = items[max_check:]

    results = await asyncio.gather(
        *[loop.run_in_executor(None, _check_url_active, i["url"]) for i in to_check]
    )
    active = [item for item, ok in zip(to_check, results) if ok]
    return active + rest  # остаток не проверяем, вернём как есть


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
    # Подгружаем фото/описание параллельно для всей партии
    to_enrich = [i for i in batch if not i.get("_enriched")]
    if to_enrich:
        enriched = await enrich_items(to_enrich)
        ei = 0
        for i, item in enumerate(batch):
            if not item.get("_enriched"):
                enriched[ei]["_enriched"] = True
                batch[i] = enriched[ei]
                _search_cache[uid][offset + i] = enriched[ei]
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

    await reply_to.answer(f"🔍 Ищу авто в {region_name} ({pmin:,}–{pmax:,} ₽)...\nЭто займёт ~1 минуту.")

    skipped = load_skipped(uid)

    loop = asyncio.get_event_loop()
    drom_items, autoru_items, kolesa_items, bibika_items, avito_items = await asyncio.gather(
        loop.run_in_executor(None, lambda: scrape_drom(region, pages=30, price_min=pmin, price_max=pmax)),
        loop.run_in_executor(None, lambda: scrape_autoru(region, pages=15, price_min=pmin, price_max=pmax)),
        loop.run_in_executor(None, lambda: scrape_kolesa(region, pages=15, price_min=pmin, price_max=pmax)),
        loop.run_in_executor(None, lambda: scrape_bibika(region, pages=10, price_min=pmin, price_max=pmax)),
        loop.run_in_executor(None, lambda: scrape_avito(region, pages=10, price_min=pmin, price_max=pmax)),
    )
    items = drom_items + autoru_items + kolesa_items + bibika_items + avito_items

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

    await reply_to.answer(f"🔎 Проверяю актуальность {min(len(suitable), 50)} объявлений...")
    suitable = await filter_active(suitable, max_check=50)

    if not suitable:
        await reply_to.answer("😔 Все найденные объявления уже сняты с продажи. Попробуй позже.")
        return

    _search_cache[uid] = suitable
    await reply_to.answer(f"✅ Найдено {len(suitable)} объявлений!\n📸 Загружаю фото и описания первых 10...")
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
