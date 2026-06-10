"""
broker.py — парсинг объявлений о продаже авто в Екатеринбурге
Источники: Авито, Авто.ру, Дром, Юла
"""

import json
import time
import datetime
import http.server
import threading
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUTPUT_FILE = "listings.json"
PORT = 8000
MIN_DAYS_POSTED = 14
MAX_PHOTOS = 3

HOT_WORDS = re.compile(
    r"\b(срочно|срочная|срочный|торг|торгуюсь|уступлю|снижу|скидка|дёшево|дешево|отдам)\b",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4,
    "май": 5, "мая": 5, "июн": 6, "июл": 7,
    "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def get(url: str, **kwargs) -> requests.Response | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, **kwargs)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  [!] GET {url[:80]} → {e}")
        return None


def parse_ru_date(text: str) -> datetime.date | None:
    """«10 июня», «10 июн.», «10.06.2024», «2024-06-10»."""
    text = text.strip()
    today = datetime.date.today()

    if "сегодня" in text.lower():
        return today
    if "вчера" in text.lower():
        return today - datetime.timedelta(days=1)

    # ISO / DD.MM.YYYY
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.datetime.strptime(text[:10], fmt).date()
        except ValueError:
            pass

    # «10 июня 2024» / «10 июн.»
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\.?\s*(\d{4})?", text, re.IGNORECASE)
    if m:
        day = int(m.group(1))
        mon = MONTHS.get(m.group(2)[:3].lower())
        year = int(m.group(3)) if m.group(3) else today.year
        if mon:
            try:
                return datetime.date(year, mon, day)
            except ValueError:
                pass
    return None


def days_ago(d: datetime.date | None) -> int:
    if d is None:
        return 0
    return max(0, (datetime.date.today() - d).days)


def hotness(title: str, desc: str, photos: int, days: int) -> float:
    score = (MAX_PHOTOS - min(photos, MAX_PHOTOS)) * 2.0
    score += days * 0.3
    if HOT_WORDS.search(title + " " + desc):
        score += 10.0
    return round(score, 2)


# ---------------------------------------------------------------------------
# Авито
# ---------------------------------------------------------------------------

def scrape_avito(pages: int = 5) -> list[dict]:
    base = "https://www.avito.ru/ekaterinburg/avtomobili"
    results = []
    for page in range(1, pages + 1):
        print(f"  Авито стр. {page}…")
        r = get(base, params={"p": page, "s": 104})
        if not r:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("[data-marker='item']")
        if not items:
            break
        for item in items:
            try:
                title_el = item.select_one("[itemprop='name']") or item.select_one("h3")
                title = title_el.get_text(strip=True) if title_el else ""

                link_el = item.select_one("a[href*='/ekaterinburg/']")
                url = ("https://www.avito.ru" + link_el["href"]) if link_el else ""

                price_el = item.select_one("[itemprop='price']") or item.select_one(
                    "[class*='price']"
                )
                price = (
                    price_el.get("content") or price_el.get_text(strip=True)
                    if price_el
                    else ""
                )

                date_el = item.select_one("[data-marker='item-date']") or item.select_one(
                    "span[class*='date']"
                )
                date_text = date_el.get_text(strip=True) if date_el else ""
                date = parse_ru_date(date_text)

                imgs = item.select("img[src]")
                photo_cnt = len([i for i in imgs if "avito" in i.get("src", "")])
                # иногда фото — атрибут data-src
                if photo_cnt == 0:
                    photo_cnt = len(item.select("[class*='photo']"))

                results.append(
                    {
                        "source": "avito",
                        "title": title,
                        "price": price,
                        "url": url,
                        "date": str(date) if date else date_text,
                        "_date_parsed": date,
                        "_photo_cnt": photo_cnt,
                        "description": "",
                    }
                )
            except Exception:
                pass
        time.sleep(1.5)
    print(f"  Авито: {len(results)} объявлений")
    return results


# ---------------------------------------------------------------------------
# Авто.ру
# ---------------------------------------------------------------------------

def scrape_autoru(pages: int = 5) -> list[dict]:
    base = "https://auto.ru/cars/used/sale/ekaterinburg/"
    results = []
    for page in range(1, pages + 1):
        print(f"  Авто.ру стр. {page}…")
        url = base if page == 1 else f"{base}?page={page}"
        r = get(url)
        if not r:
            break
        soup = BeautifulSoup(r.text, "html.parser")

        # Авто.ру — React-приложение; начальное состояние рендерится в JSON внутри <script>
        scripts = soup.find_all("script")
        data = []
        for s in scripts:
            txt = s.string or ""
            if "listing" in txt and "saleId" in txt:
                # Ищем массив объявлений
                m = re.search(r'"offers"\s*:\s*(\[.*?\])\s*,\s*"', txt, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group(1))
                    except Exception:
                        pass
                    break

        if not data:
            # Попробуем API
            api_url = (
                "https://auto.ru/-/ajax/desktop/listing/"
                if page == 1
                else f"https://auto.ru/-/ajax/desktop/listing/?page={page}"
            )
            ar = get(
                api_url,
                headers={**HEADERS, "x-requested-with": "XMLHttpRequest"},
                params={
                    "category": "cars",
                    "section": "used",
                    "geo_id": 54,  # Екатеринбург
                    "page": page,
                    "page_size": 37,
                },
            )
            if ar:
                try:
                    data = ar.json().get("offers", [])
                except Exception:
                    pass

        for offer in data:
            try:
                docs = offer.get("documents", {})
                vehicle = offer.get("vehicle_info", {})
                mark = vehicle.get("mark_info", {}).get("name", "")
                model = vehicle.get("model_info", {}).get("name", "")
                year = docs.get("year", "")
                title = f"{mark} {model} {year}".strip()

                price = offer.get("price_info", {}).get("RUR", "")
                sale_id = offer.get("saleId") or offer.get("id", "")
                url_path = offer.get("url", "") or (
                    f"https://auto.ru/cars/used/sale/{sale_id}/" if sale_id else ""
                )

                ts = offer.get("additional_info", {}).get("fresh_date") or offer.get(
                    "creation_date"
                )
                if ts:
                    date = datetime.date.fromtimestamp(int(str(ts)[:10]))
                else:
                    date = None

                photos = offer.get("photo_urls") or offer.get("state", {}).get(
                    "image_urls", []
                )
                photo_cnt = len(photos) if isinstance(photos, list) else 0

                desc = offer.get("description", "")

                results.append(
                    {
                        "source": "autoru",
                        "title": title,
                        "price": str(price),
                        "url": url_path,
                        "date": str(date) if date else "",
                        "_date_parsed": date,
                        "_photo_cnt": photo_cnt,
                        "description": desc,
                    }
                )
            except Exception:
                pass
        if not data:
            break
        time.sleep(1.5)
    print(f"  Авто.ру: {len(results)} объявлений")
    return results


# ---------------------------------------------------------------------------
# Дром
# ---------------------------------------------------------------------------

def scrape_drom(pages: int = 5) -> list[dict]:
    base = "https://ekaterinburg.drom.ru/auto/all/"
    results = []
    for page in range(1, pages + 1):
        print(f"  Дром стр. {page}…")
        url = base if page == 1 else f"{base}page{page}/"
        r = get(url)
        if not r:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("a[data-ftid='bull_title']")
        if not cards:
            # Альтернативный селектор
            cards = soup.select("div[data-ftid='bulls-list_bull']")

        if not cards:
            break

        containers = soup.select("div[data-ftid='bulls-list_bull']")
        for card in containers:
            try:
                link = card.select_one("a[data-ftid='bull_title']")
                title = link.get_text(strip=True) if link else ""
                href = link["href"] if link else ""
                url_item = href if href.startswith("http") else "https://www.drom.ru" + href

                price_el = card.select_one("span[data-ftid='bull_price']")
                price = price_el.get_text(strip=True) if price_el else ""

                date_el = card.select_one("span[data-ftid='bull_date-created']")
                date_text = date_el.get_text(strip=True) if date_el else ""
                date = parse_ru_date(date_text)

                imgs = card.select("img")
                photo_cnt = len(imgs)

                results.append(
                    {
                        "source": "drom",
                        "title": title,
                        "price": price,
                        "url": url_item,
                        "date": str(date) if date else date_text,
                        "_date_parsed": date,
                        "_photo_cnt": photo_cnt,
                        "description": "",
                    }
                )
            except Exception:
                pass
        time.sleep(1.5)
    print(f"  Дром: {len(results)} объявлений")
    return results


# ---------------------------------------------------------------------------
# Юла
# ---------------------------------------------------------------------------

def scrape_youla(pages: int = 3) -> list[dict]:
    """Юла частично рендерится на сервере; используем публичный API."""
    results = []
    api = "https://youla.ru/api/products"
    cursor = None
    for page in range(pages):
        print(f"  Юла стр. {page + 1}…")
        params = {
            "category_slug": "avtomobili",
            "city_slug": "ekaterinburg",
            "limit": 48,
        }
        if cursor:
            params["cursor"] = cursor
        r = get(api, params=params, headers={**HEADERS, "Accept": "application/json"})
        if not r:
            break
        try:
            js = r.json()
        except Exception:
            break

        items = js.get("data", {}).get("products", []) or js.get("products", [])
        if not items:
            # Попробуем GraphQL-подобный эндпоинт
            break
        cursor = js.get("data", {}).get("cursor") or js.get("cursor")

        for item in items:
            try:
                title = item.get("name", "")
                price = str(item.get("price", {}).get("product_price", ""))
                url_item = "https://youla.ru" + (item.get("uri") or item.get("url", ""))
                date_ts = item.get("date_created") or item.get("published_at")
                date = (
                    datetime.date.fromtimestamp(int(date_ts)) if date_ts else None
                )
                photos = item.get("images") or []
                photo_cnt = len(photos) if isinstance(photos, list) else 0
                desc = item.get("description", "")

                results.append(
                    {
                        "source": "youla",
                        "title": title,
                        "price": price,
                        "url": url_item,
                        "date": str(date) if date else "",
                        "_date_parsed": date,
                        "_photo_cnt": photo_cnt,
                        "description": desc,
                    }
                )
            except Exception:
                pass

        if not cursor:
            break
        time.sleep(1.0)
    print(f"  Юла: {len(results)} объявлений")
    return results


# ---------------------------------------------------------------------------
# Сбор, фильтрация, оценка
# ---------------------------------------------------------------------------

def fetch_all() -> list[dict]:
    all_items: list[dict] = []
    scrapers = [
        ("Авито",   scrape_avito),
        ("Авто.ру", scrape_autoru),
        ("Дром",    scrape_drom),
        ("Юла",     scrape_youla),
    ]
    for name, fn in scrapers:
        print(f"\n[{name}]")
        try:
            all_items.extend(fn())
        except Exception as e:
            print(f"  [!] Ошибка {name}: {e}")
    return all_items


def filter_and_score(items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        photos = item.get("_photo_cnt", 0)
        date = item.get("_date_parsed")
        # Удаляем нессериализуемый объект даты перед сохранением
        item.pop("_date_parsed", None)

        d = days_ago(date)
        if photos > MAX_PHOTOS:
            continue
        if d < MIN_DAYS_POSTED:
            continue

        title = item.get("title", "")
        desc = item.get("description", "")
        score = hotness(title, desc, photos, d)

        item["_photos"] = photos
        item["_days_on_site"] = d
        item["_hot_score"] = score
        result.append(item)

    result.sort(key=lambda x: x["_hot_score"], reverse=True)
    print(f"\nПосле фильтрации: {len(result)} объявлений")
    return result


# ---------------------------------------------------------------------------
# Сохранение
# ---------------------------------------------------------------------------

def save(listings: list[dict]) -> None:
    Path(OUTPUT_FILE).write_text(
        json.dumps(listings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Сохранено → {OUTPUT_FILE}")


# ---------------------------------------------------------------------------
# Веб-сервер с CORS
# ---------------------------------------------------------------------------

class CORSHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/listings.json"):
            self.path = "/listings.json"
        super().do_GET()

    def log_message(self, fmt, *args):
        print(f"[HTTP] {fmt % args}")


def start_server() -> None:
    server = http.server.HTTPServer(("0.0.0.0", PORT), CORSHandler)
    print(f"Веб-сервер: http://localhost:{PORT}/listings.json")
    server.serve_forever()


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main():
    print("=== Broker: авто Екатеринбург (Авито / Авто.ру / Дром / Юла) ===")
    items = fetch_all()
    listings = filter_and_score(items)
    save(listings)

    thread = threading.Thread(target=start_server, daemon=True)
    thread.start()

    print("\nНажмите Ctrl+C для остановки.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nОстановка.")


if __name__ == "__main__":
    main()
