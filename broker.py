"""
broker.py — парсинг объявлений о продаже авто в Екатеринбурге
Источники: Авито, Авто.ру, Дром, Юла
Использует Playwright (реальный браузер Chromium) для обхода антибот-защиты.
"""

import json
import time
import random
import datetime
import http.server
import threading
import re
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext

OUTPUT_FILE = "listings.json"
PORT = 8000
MIN_DAYS_POSTED = 14
MAX_PHOTOS = 3

HOT_WORDS = re.compile(
    r"(срочно|срочная|срочный|торг|торгуюсь|уступлю|снижу|скидка|дёшево|дешево|отдам)",
    re.IGNORECASE,
)

MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4,
    "май": 5, "мая": 5, "июн": 6, "июл": 7,
    "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def parse_ru_date(text: str) -> datetime.date | None:
    if not text:
        return None
    text = text.strip()
    today = datetime.date.today()
    low = text.lower()

    if "сегодня" in low:
        return today
    if "вчера" in low:
        return today - datetime.timedelta(days=1)
    m = re.search(r"(\d+)\s+дн", low)
    if m:
        return today - datetime.timedelta(days=int(m.group(1)))
    if re.search(r"\d+\s+(час|мин|секунд)", low):
        return today

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.datetime.strptime(text[:10], fmt).date()
        except ValueError:
            pass

    m = re.search(r"(\d{1,2})\s+([а-яё]+)\.?\s*(\d{4})?", text, re.IGNORECASE)
    if m:
        day = int(m.group(1))
        mon = MONTHS.get(m.group(2)[:3].lower())
        year = int(m.group(3)) if m.group(3) else today.year
        if mon:
            try:
                d = datetime.date(year, mon, day)
                if d > today:
                    d = d.replace(year=year - 1)
                return d
            except ValueError:
                pass
    return None


def days_ago(d: datetime.date | None) -> int:
    # Если дата неизвестна — считаем объявление достаточно старым, не отфильтровываем
    if d is None:
        return MIN_DAYS_POSTED
    return max(0, (datetime.date.today() - d).days)


def hotness(title: str, desc: str, photos: int, days: int) -> float:
    score = (MAX_PHOTOS - min(photos, MAX_PHOTOS)) * 2.0
    score += days * 0.3
    if HOT_WORDS.search(title + " " + desc):
        score += 10.0
    return round(score, 2)


def human_delay(lo: float = 1.0, hi: float = 3.0) -> None:
    time.sleep(random.uniform(lo, hi))


def make_context(playwright) -> tuple:
    """Создаёт браузер и контекст с русскими настройками."""
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ],
    )
    context = browser.new_context(
        viewport={"width": 1366, "height": 768},
        locale="ru-RU",
        timezone_id="Asia/Yekaterinburg",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        extra_http_headers={
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    # Скрываем признаки автоматизации
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
        window.chrome = {runtime: {}};
    """)
    return browser, context


# ---------------------------------------------------------------------------
# Авито
# ---------------------------------------------------------------------------

def scrape_avito(context: BrowserContext, pages: int = 5) -> list[dict]:
    results = []
    page = context.new_page()
    try:
        for p in range(1, pages + 1):
            print(f"  Авито стр. {p}…")
            url = f"https://www.avito.ru/ekaterinburg/avtomobili?p={p}&s=104"
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            human_delay(2, 4)

            # Проверяем капчу
            if "captcha" in page.url or page.query_selector("div[class*='firewall']"):
                print("  [!] Авито: обнаружена капча, пропускаем")
                break

            items = page.query_selector_all("[data-marker='item']")
            if not items:
                print("  Авито: объявления не найдены")
                break

            for item in items:
                try:
                    title_el = item.query_selector("[itemprop='name']") or item.query_selector("h3")
                    title = title_el.inner_text().strip() if title_el else ""

                    link_el = item.query_selector("a[href*='/ekaterinburg/']")
                    href = link_el.get_attribute("href") if link_el else ""
                    url_item = ("https://www.avito.ru" + href) if href else ""

                    price_el = item.query_selector("[itemprop='price']") or item.query_selector("[class*='price']")
                    price = ""
                    if price_el:
                        price = price_el.get_attribute("content") or price_el.inner_text().strip()

                    date_el = item.query_selector("[data-marker='item-date']") or item.query_selector("span[class*='date']")
                    date_text = date_el.inner_text().strip() if date_el else ""
                    date = parse_ru_date(date_text)

                    # Счётчик фото
                    photo_cnt = 0
                    photo_el = item.query_selector("[class*='photo-count'], [class*='iva-item-photo']")
                    if photo_el:
                        m = re.search(r"(\d+)", photo_el.inner_text())
                        if m:
                            photo_cnt = int(m.group(1))
                    if photo_cnt == 0:
                        photo_cnt = len(item.query_selector_all("img[src*='avito']"))

                    results.append({
                        "source": "avito",
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

            human_delay(2, 5)
    finally:
        page.close()

    print(f"  Авито: {len(results)} объявлений")
    return results


# ---------------------------------------------------------------------------
# Авто.ру
# ---------------------------------------------------------------------------

def scrape_autoru(context: BrowserContext, pages: int = 5) -> list[dict]:
    results = []
    page = context.new_page()
    try:
        for p in range(1, pages + 1):
            print(f"  Авто.ру стр. {p}…")
            url = (
                "https://auto.ru/cars/used/sale/ekaterinburg/"
                if p == 1
                else f"https://auto.ru/cars/used/sale/ekaterinburg/?page={p}"
            )
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            human_delay(2, 4)

            # Извлекаем данные из window.__INITIAL_STATE__
            raw = page.evaluate("""
                () => {
                    try {
                        const s = window.__INITIAL_STATE__;
                        if (s && s.listing && s.listing.data && s.listing.data.offers) {
                            return JSON.stringify(s.listing.data.offers);
                        }
                    } catch(e) {}
                    return null;
                }
            """)

            offers = []
            if raw:
                try:
                    offers = json.loads(raw)
                except Exception:
                    pass

            # Запасной вариант — HTML-карточки
            if not offers:
                cards = page.query_selector_all(".ListingItem")
                for card in cards:
                    try:
                        title_el = card.query_selector(".ListingItem__title")
                        title = title_el.inner_text().strip() if title_el else ""
                        link_el = card.query_selector("a.ListingItem__link")
                        href = link_el.get_attribute("href") if link_el else ""
                        price_el = card.query_selector(".ListingItem__price")
                        price = price_el.inner_text().strip() if price_el else ""
                        date_el = card.query_selector(".ListingItem__date")
                        date_text = date_el.inner_text().strip() if date_el else ""
                        date = parse_ru_date(date_text)
                        imgs = card.query_selector_all("img")
                        photo_cnt = len(imgs)
                        results.append({
                            "source": "autoru",
                            "title": title,
                            "price": price,
                            "url": href,
                            "date": str(date) if date else date_text,
                            "_date_parsed": date,
                            "_photo_cnt": photo_cnt,
                            "description": "",
                        })
                    except Exception:
                        pass
                if not cards:
                    break
            else:
                for offer in offers:
                    try:
                        docs = offer.get("documents", {})
                        vehicle = offer.get("vehicle_info", {})
                        mark = vehicle.get("mark_info", {}).get("name", "")
                        model = vehicle.get("model_info", {}).get("name", "")
                        year = docs.get("year", "")
                        title = f"{mark} {model} {year}".strip()
                        price = str(offer.get("price_info", {}).get("RUR", ""))
                        url_item = offer.get("url", "")
                        ts = (
                            offer.get("additional_info", {}).get("fresh_date")
                            or offer.get("creation_date")
                        )
                        date = datetime.date.fromtimestamp(int(str(ts)[:10])) if ts else None
                        photos = offer.get("photo_urls") or []
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

            human_delay(2, 4)
    finally:
        page.close()

    print(f"  Авто.ру: {len(results)} объявлений")
    return results


# ---------------------------------------------------------------------------
# Дром
# ---------------------------------------------------------------------------

def scrape_drom(context: BrowserContext, pages: int = 5) -> list[dict]:
    results = []
    page = context.new_page()
    try:
        for p in range(1, pages + 1):
            print(f"  Дром стр. {p}…")
            url = (
                "https://ekaterinburg.drom.ru/auto/all/"
                if p == 1
                else f"https://ekaterinburg.drom.ru/auto/all/page{p}/"
            )
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            human_delay(1.5, 3)

            # Пробуем несколько вариантов селекторов — Дром периодически меняет вёрстку
            containers = (
                page.query_selector_all("div[data-ftid='bulls-list_bull']")
                or page.query_selector_all("article.css-1nuvnlx")
                or page.query_selector_all("div.css-1nuvnlx")
                or page.query_selector_all("[class*='bull_']")
            )

            if not containers:
                # Последняя попытка — ищем любые ссылки на объявления
                links = page.query_selector_all("a[href*='drom.ru/auto']")
                for link in links:
                    href = link.get_attribute("href") or ""
                    title = link.inner_text().strip()
                    if title and href:
                        results.append({
                            "source": "drom",
                            "title": title,
                            "price": "",
                            "url": href,
                            "date": "",
                            "_date_parsed": None,
                            "_photo_cnt": 0,
                            "description": "",
                        })
                if not links:
                    break
                human_delay(1.5, 3)
                continue

            # Отладка: показываем HTML первой карточки чтобы видеть актуальные атрибуты
            if containers and p == 1:
                try:
                    sample = containers[0].inner_html()
                    print(f"  [debug] первая карточка (первые 400 симв.):\n  {sample[:400]}")
                except Exception:
                    pass

            for card in containers:
                try:
                    link = (
                        card.query_selector("a[data-ftid='bull_title']")
                        or card.query_selector("h3 a")
                        or card.query_selector("a[class*='title']")
                        or card.query_selector("a")
                    )
                    title = link.inner_text().strip() if link else ""
                    href = link.get_attribute("href") if link else ""
                    url_item = href if href and href.startswith("http") else ("https://ekaterinburg.drom.ru" + (href or ""))

                    price_el = (
                        card.query_selector("span[data-ftid='bull_price']")
                        or card.query_selector("[class*='price']")
                    )
                    price = price_el.inner_text().strip() if price_el else ""

                    # Дром хранит дату в разных местах — перебираем все варианты
                    date_text = ""
                    for date_sel in [
                        "span[data-ftid='bull_date-created']",
                        "span[data-ftid='bull_date']",
                        "[class*='date-created']",
                        "[class*='dateCreated']",
                        "time",
                        "[class*='date']",
                    ]:
                        date_el = card.query_selector(date_sel)
                        if date_el:
                            t = date_el.get_attribute("datetime") or date_el.inner_text().strip()
                            if t:
                                date_text = t
                                break
                    date = parse_ru_date(date_text)

                    photo_cnt = 0
                    for photo_sel in [
                        "span[data-ftid='bull_images-count']",
                        "[class*='images-count']",
                        "[class*='photo-count']",
                        "[class*='photosCount']",
                    ]:
                        photo_el = card.query_selector(photo_sel)
                        if photo_el:
                            m = re.search(r"(\d+)", photo_el.inner_text())
                            if m:
                                photo_cnt = int(m.group(1))
                                break
                    if photo_cnt == 0:
                        photo_cnt = len(card.query_selector_all("img"))

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

            human_delay(1.5, 3)
    finally:
        page.close()

    print(f"  Дром: {len(results)} объявлений")
    return results


# ---------------------------------------------------------------------------
# Юла
# ---------------------------------------------------------------------------

def scrape_youla(context: BrowserContext, pages: int = 3) -> list[dict]:
    results = []
    page = context.new_page()
    try:
        for p in range(1, pages + 1):
            print(f"  Юла стр. {p}…")
            url = (
                "https://youla.ru/ekaterinburg/avtomobili"
                if p == 1
                else f"https://youla.ru/ekaterinburg/avtomobili?page={p}"
            )
            page.goto(url, wait_until="networkidle", timeout=40000)
            human_delay(2, 4)

            # Пробуем извлечь данные из Redux-стора
            raw = page.evaluate("""
                () => {
                    try {
                        const s = window.__REDUX_STATE__ || window.__INITIAL_STATE__;
                        if (s) return JSON.stringify(s);
                    } catch(e) {}
                    return null;
                }
            """)

            if raw:
                try:
                    js = json.loads(raw)
                    # Ищем массив товаров в любом ключе
                    def find_products(obj, depth=0):
                        if depth > 5:
                            return []
                        if isinstance(obj, list) and obj and isinstance(obj[0], dict) and "name" in obj[0]:
                            return obj
                        if isinstance(obj, dict):
                            for v in obj.values():
                                r = find_products(v, depth + 1)
                                if r:
                                    return r
                        return []

                    products = find_products(js)
                    for item in products:
                        title = item.get("name", "")
                        price_data = item.get("price", {})
                        price = str(price_data.get("product_price", price_data) if isinstance(price_data, dict) else price_data)
                        uri = item.get("uri") or item.get("url", "")
                        url_item = ("https://youla.ru" + uri) if uri and not uri.startswith("http") else uri
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
                    if products:
                        human_delay(2, 3)
                        continue
                except Exception:
                    pass

            # Отладка: выводим первые 500 символов body чтобы понять структуру
            if p == 1:
                try:
                    body_preview = page.evaluate("() => document.body.innerHTML.slice(0, 500)")
                    print(f"  [debug Юла] body preview:\n  {body_preview}")
                except Exception:
                    pass

            # Запасной вариант — HTML-карточки (перебираем все известные варианты)
            cards = (
                page.query_selector_all("div[class*='product_item']")
                or page.query_selector_all("li[class*='ProductItem']")
                or page.query_selector_all("[data-test*='product']")
                or page.query_selector_all("article")
                or page.query_selector_all("[class*='SnippetCard']")
                or page.query_selector_all("[class*='snippet']")
                or page.query_selector_all("ul[class*='list'] > li")
            )
            if not cards:
                print("  Юла: карточки не найдены (сайт мог изменить вёрстку)")
                break

            for card in cards:
                try:
                    link = card.query_selector("a")
                    href = link.get_attribute("href") if link else ""
                    title_el = card.query_selector("[class*='title'], h3, h2")
                    title = title_el.inner_text().strip() if title_el else (link.inner_text().strip() if link else "")
                    price_el = card.query_selector("[class*='price']")
                    price = price_el.inner_text().strip() if price_el else ""
                    url_item = ("https://youla.ru" + href) if href and not href.startswith("http") else href
                    imgs = card.query_selector_all("img")
                    photo_cnt = len(imgs)
                    results.append({
                        "source": "youla",
                        "title": title,
                        "price": price,
                        "url": url_item,
                        "date": "",
                        "_date_parsed": None,
                        "_photo_cnt": photo_cnt,
                        "description": "",
                    })
                except Exception:
                    pass

            human_delay(2, 4)
    finally:
        page.close()

    print(f"  Юла: {len(results)} объявлений")
    return results


# ---------------------------------------------------------------------------
# Сбор, фильтрация, оценка
# ---------------------------------------------------------------------------

def fetch_all() -> list[dict]:
    all_items: list[dict] = []
    with sync_playwright() as pw:
        browser, context = make_context(pw)
        try:
            scrapers = [
                ("Авито",   scrape_avito),
                ("Авто.ру", scrape_autoru),
                ("Дром",    scrape_drom),
                ("Юла",     scrape_youla),
            ]
            for name, fn in scrapers:
                print(f"\n[{name}]")
                try:
                    all_items.extend(fn(context))
                except Exception as e:
                    print(f"  [!] Ошибка {name}: {e}")
        finally:
            context.close()
            browser.close()
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
        f"пропущено (мало дней): {skipped_days}, "
        f"без даты: {no_date}, "
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
