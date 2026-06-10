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
    r"(срочно|срочная|срочный|торг|торгуюсь|уступлю|снижу|скидка|дёшево|дешево|отдам)",
    re.IGNORECASE,
)

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4,
    "май": 5, "мая": 5, "июн": 6, "июл": 7,
    "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}

# Единая сессия для переиспользования TCP-соединений и хранения куков
SESSION = requests.Session()
SESSION.headers.update(BASE_HEADERS)


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def get(url: str, extra_headers: dict | None = None, **kwargs) -> requests.Response | None:
    """GET через общую сессию. extra_headers добавляются поверх BASE_HEADERS."""
    try:
        hdrs = {**BASE_HEADERS, **(extra_headers or {})}
        r = SESSION.get(url, headers=hdrs, timeout=20, **kwargs)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  [!] GET {url[:80]} → {e}")
        return None


def parse_ru_date(text: str) -> datetime.date | None:
    """Парсит «10 июня», «10 июн.», «10.06.2024», «2024-06-10», «вчера»."""
    if not text:
        return None
    text = text.strip()
    today = datetime.date.today()

    low = text.lower()
    if "сегодня" in low:
        return today
    if "вчера" in low:
        return today - datetime.timedelta(days=1)
    # «N дней назад»
    m = re.search(r"(\d+)\s+дн", low)
    if m:
        return today - datetime.timedelta(days=int(m.group(1)))
    # «N часов/минут назад» → сегодня
    if re.search(r"\d+\s+(час|мин|секунд)", low):
        return today

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            s = re.sub(r"\s+", "", text)[:10]
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
                d = datetime.date(year, mon, day)
                # если дата в будущем — прошлый год
                if d > today:
                    d = d.replace(year=year - 1)
                return d
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
# Авито — обходим 429 через cookie-сессию и задержки
# ---------------------------------------------------------------------------

def scrape_avito(pages: int = 5) -> list[dict]:
    base = "https://www.avito.ru/ekaterinburg/avtomobili"
    results = []

    # Сначала «прогреваем» сессию главной страницей, чтобы получить куки
    print("  Авито: инициализация сессии…")
    get("https://www.avito.ru/")
    time.sleep(2)

    for page in range(1, pages + 1):
        print(f"  Авито стр. {page}…")
        r = get(base, params={"p": page, "s": 104})
        if not r:
            # 429 — ждём и пробуем ещё раз
            print("  Авито: пауза 15 сек после блокировки…")
            time.sleep(15)
            r = get(base, params={"p": page, "s": 104})
            if not r:
                break

        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("[data-marker='item']")
        if not items:
            print("  Авито: объявления не найдены (возможно, блокировка)")
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
                    if price_el else ""
                )

                date_el = (
                    item.select_one("[data-marker='item-date']")
                    or item.select_one("span[class*='date']")
                )
                date_text = date_el.get_text(strip=True) if date_el else ""
                date = parse_ru_date(date_text)

                # Количество фото — ищем счётчик вида «12 фото»
                photo_cnt = 0
                photo_counter = item.select_one("[class*='iva-item-photo']")
                if photo_counter:
                    m = re.search(r"(\d+)", photo_counter.get_text())
                    if m:
                        photo_cnt = int(m.group(1))
                if photo_cnt == 0:
                    photo_cnt = len(item.select("img[src*='avito']"))

                results.append({
                    "source": "avito",
                    "title": title,
                    "price": price,
                    "url": url,
                    "date": str(date) if date else date_text,
                    "_date_parsed": date,
                    "_photo_cnt": photo_cnt,
                    "description": "",
                })
            except Exception:
                pass

        time.sleep(3)  # вежливая пауза между страницами

    print(f"  Авито: {len(results)} объявлений")
    return results


# ---------------------------------------------------------------------------
# Авто.ру
# ---------------------------------------------------------------------------

def scrape_autoru(pages: int = 5) -> list[dict]:
    results = []
    for page in range(1, pages + 1):
        print(f"  Авто.ру стр. {page}…")
        api_url = "https://auto.ru/-/ajax/desktop/listing/"
        r = get(
            api_url,
            extra_headers={
                "x-requested-with": "XMLHttpRequest",
                "Referer": "https://auto.ru/cars/used/sale/ekaterinburg/",
            },
            params={
                "category": "cars",
                "section": "used",
                "geo_id": 54,
                "page": page,
                "page_size": 37,
            },
        )

        data = []
        if r:
            try:
                data = r.json().get("offers", [])
            except Exception:
                pass

        # Запасной вариант — парсить HTML
        if not data:
            url = (
                "https://auto.ru/cars/used/sale/ekaterinburg/"
                if page == 1
                else f"https://auto.ru/cars/used/sale/ekaterinburg/?page={page}"
            )
            r2 = get(url)
            if r2:
                soup = BeautifulSoup(r2.text, "html.parser")
                for script in soup.find_all("script"):
                    txt = script.string or ""
                    m = re.search(r'"offers"\s*:\s*(\[.+?\])\s*,\s*"pagination"', txt, re.DOTALL)
                    if m:
                        try:
                            data = json.loads(m.group(1))
                        except Exception:
                            pass
                        break

        if not data:
            break

        for offer in data:
            try:
                docs = offer.get("documents", {})
                vehicle = offer.get("vehicle_info", {})
                mark = vehicle.get("mark_info", {}).get("name", "")
                model = vehicle.get("model_info", {}).get("name", "")
                year = docs.get("year", "")
                title = f"{mark} {model} {year}".strip() or offer.get("name", "")

                price = str(offer.get("price_info", {}).get("RUR", ""))
                sale_id = offer.get("saleId") or offer.get("id", "")
                url_item = offer.get("url", "") or (
                    f"https://auto.ru/cars/used/sale/{sale_id}/" if sale_id else ""
                )

                ts = (
                    offer.get("additional_info", {}).get("fresh_date")
                    or offer.get("creation_date")
                )
                date = datetime.date.fromtimestamp(int(str(ts)[:10])) if ts else None

                photos = offer.get("photo_urls") or offer.get("state", {}).get("image_urls", [])
                photo_cnt = len(photos) if isinstance(photos, list) else 0

                results.append({
                    "source": "autoru",
                    "title": title,
                    "price": price,
                    "url": url_item,
                    "date": str(date) if date else "",
                    "_date_parsed": date,
                    "_photo_cnt": photo_cnt,
                    "description": offer.get("description", ""),
                })
            except Exception:
                pass

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
        containers = soup.select("div[data-ftid='bulls-list_bull']")
        if not containers:
            break

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

                # Дром иногда пишет «10 июня» без года
                date = parse_ru_date(date_text)

                # Количество фото — ищем счётчик «12 фото» или иконки
                photo_cnt = 0
                photo_el = card.select_one("span[data-ftid='bull_images-count']")
                if photo_el:
                    m = re.search(r"(\d+)", photo_el.get_text())
                    if m:
                        photo_cnt = int(m.group(1))
                if photo_cnt == 0:
                    photo_cnt = len(card.select("img"))

                results.append({
                    "source": "drom",
                    "title": title,
                    "price": price,
                    "url": url_item,
                    "date": str(date) if date else date_text,
                    "_date_parsed": date,
                    "_photo_cnt": photo_cnt,
                    "description": "",
                })
            except Exception:
                pass

        time.sleep(1.5)

    print(f"  Дром: {len(results)} объявлений")
    return results


# ---------------------------------------------------------------------------
# Юла
# ---------------------------------------------------------------------------

def scrape_youla(pages: int = 3) -> list[dict]:
    results = []
    cursor = None
    for page in range(pages):
        print(f"  Юла стр. {page + 1}…")
        params: dict = {
            "category_slug": "avtomobili",
            "city_slug": "ekaterinburg",
            "limit": 48,
        }
        if cursor:
            params["cursor"] = cursor

        r = get(
            "https://youla.ru/api/products",
            extra_headers={"Accept": "application/json"},
            params=params,
        )
        if not r:
            break
        try:
            js = r.json()
        except Exception:
            break

        items = js.get("data", {}).get("products", []) or js.get("products", [])
        if not items:
            break

        cursor = js.get("data", {}).get("cursor") or js.get("cursor")

        for item in items:
            try:
                title = item.get("name", "")
                price = str(item.get("price", {}).get("product_price", ""))
                url_item = "https://youla.ru" + (item.get("uri") or item.get("url", ""))
                date_ts = item.get("date_created") or item.get("published_at")
                date = datetime.date.fromtimestamp(int(date_ts)) if date_ts else None
                photos = item.get("images") or []
                photo_cnt = len(photos) if isinstance(photos, list) else 0

                results.append({
                    "source": "youla",
                    "title": title,
                    "price": price,
                    "url": url_item,
                    "date": str(date) if date else "",
                    "_date_parsed": date,
                    "_photo_cnt": photo_cnt,
                    "description": item.get("description", ""),
                })
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
    skipped_photos = 0
    skipped_days = 0
    no_date = 0

    for item in items:
        photos = item.get("_photo_cnt", 0)
        date = item.pop("_date_parsed", None)
        d = days_ago(date)

        if date is None:
            no_date += 1

        if photos > MAX_PHOTOS:
            skipped_photos += 1
            continue
        if d < MIN_DAYS_POSTED:
            skipped_days += 1
            continue

        score = hotness(item.get("title", ""), item.get("description", ""), photos, d)
        item["_photos"] = photos
        item["_days_on_site"] = d
        item["_hot_score"] = score
        result.append(item)

    result.sort(key=lambda x: x["_hot_score"], reverse=True)
    print(
        f"\nФильтрация: исходно {len(items)}, "
        f"пропущено (много фото): {skipped_photos}, "
        f"пропущено (мало дней / нет даты): {skipped_days}, "
        f"без даты всего: {no_date}, "
        f"прошло фильтр: {len(result)}"
    )
    return result


def save(listings: list[dict]) -> None:
    Path(OUTPUT_FILE).write_text(
        json.dumps(listings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Сохранено → {OUTPUT_FILE}  ({len(listings)} объявлений)")


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
