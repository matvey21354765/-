"""
broker.py — парсинг объявлений о продаже авто в Екатеринбурге
Источники: Авито, Авто.ру, Дром

Настройка (заполни ниже):
  PROXY       — HTTP/SOCKS5 прокси для обхода блокировок (опционально)
  CAPTCHA_KEY — API-ключ 2captcha.com для решения капч Авито (опционально)
"""

import json
import time
import random
import datetime
import http.server
import threading
import re
import base64
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright, BrowserContext

# ============================================================
#  НАСТРОЙКИ — заполни свои значения
# ============================================================

# Прокси: "http://user:pass@host:port"  или  "socks5://user:pass@host:port"
# Оставь пустой строкой если прокси нет
PROXY = ""

# API-ключ 2captcha.com (регистрация бесплатна, капчи ~$1 за 1000)
# Оставь пустой строкой если решать капчи не нужно
CAPTCHA_KEY = ""

# ============================================================

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

# Набор user-agent'ов для ротации
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


# ---------------------------------------------------------------------------
# Утилиты — дата
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
    if d is None:
        return MIN_DAYS_POSTED  # неизвестная дата — пропускаем фильтр
    return max(0, (datetime.date.today() - d).days)


def hotness(title: str, desc: str, photos: int, days: int) -> float:
    score = (MAX_PHOTOS - min(photos, MAX_PHOTOS)) * 2.0
    score += days * 0.3
    if HOT_WORDS.search(title + " " + desc):
        score += 10.0
    return round(score, 2)


def human_delay(lo: float = 1.0, hi: float = 3.0) -> None:
    time.sleep(random.uniform(lo, hi))


# ---------------------------------------------------------------------------
# 2captcha — решение капч
# ---------------------------------------------------------------------------

def solve_image_captcha(img_base64: str) -> str | None:
    """Отправляет картинку в 2captcha, возвращает текст ответа."""
    if not CAPTCHA_KEY:
        return None
    try:
        # Отправляем задачу
        data = f"key={CAPTCHA_KEY}&method=base64&body={urllib.parse.quote(img_base64)}&json=1"
        req = urllib.request.Request(
            "http://2captcha.com/in.php",
            data=data.encode(),
            method="POST",
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        if resp.get("status") != 1:
            print(f"  [2captcha] ошибка отправки: {resp}")
            return None
        task_id = resp["request"]

        # Ждём результата (poll каждые 5 сек, до 120 сек)
        for _ in range(24):
            time.sleep(5)
            res = json.loads(urllib.request.urlopen(
                f"http://2captcha.com/res.php?key={CAPTCHA_KEY}&action=get&id={task_id}&json=1",
                timeout=10,
            ).read())
            if res.get("status") == 1:
                return res["request"]
            if res.get("request") != "CAPCHA_NOT_READY":
                print(f"  [2captcha] ошибка: {res}")
                return None
        return None
    except Exception as e:
        print(f"  [2captcha] исключение: {e}")
        return None


def solve_recaptcha_v2(site_key: str, page_url: str) -> str | None:
    """Решает reCAPTCHA v2 через 2captcha."""
    if not CAPTCHA_KEY:
        return None
    try:
        import urllib.parse
        data = (
            f"key={CAPTCHA_KEY}&method=userrecaptcha"
            f"&googlekey={urllib.parse.quote(site_key)}"
            f"&pageurl={urllib.parse.quote(page_url)}&json=1"
        )
        req = urllib.request.Request(
            "http://2captcha.com/in.php",
            data=data.encode(),
            method="POST",
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        if resp.get("status") != 1:
            return None
        task_id = resp["request"]
        for _ in range(36):
            time.sleep(5)
            res = json.loads(urllib.request.urlopen(
                f"http://2captcha.com/res.php?key={CAPTCHA_KEY}&action=get&id={task_id}&json=1",
                timeout=10,
            ).read())
            if res.get("status") == 1:
                return res["request"]
            if res.get("request") != "CAPCHA_NOT_READY":
                return None
        return None
    except Exception as e:
        print(f"  [2captcha reCAPTCHA] исключение: {e}")
        return None


# ---------------------------------------------------------------------------
# Обработчик капчи Авито
# ---------------------------------------------------------------------------

def handle_avito_captcha(page) -> bool:
    """
    Проверяет наличие капчи на странице и пытается её решить.
    Возвращает True если страница чистая (капчи нет или решена).
    """
    url = page.url
    html = page.content()

    # Авито использует несколько видов защиты
    is_blocked = (
        "captcha" in url.lower()
        or page.query_selector("[class*='firewall']") is not None
        or page.query_selector("[class*='captcha']") is not None
        or "Доступ ограничен" in html
        or "robot" in html.lower()
    )

    if not is_blocked:
        return True  # всё чисто

    print("  [!] Авито: обнаружена защита/капча")

    if not CAPTCHA_KEY:
        print("  [!] CAPTCHA_KEY не задан — капча не решается. Укажи ключ 2captcha в начале файла.")
        return False

    # Попытка 1: reCAPTCHA v2 (iframe от Google)
    recaptcha_frame = page.query_selector("iframe[src*='recaptcha']")
    if recaptcha_frame:
        src = recaptcha_frame.get_attribute("src") or ""
        m = re.search(r"k=([A-Za-z0-9_-]+)", src)
        if m:
            site_key = m.group(1)
            print(f"  Решаем reCAPTCHA v2 (sitekey={site_key[:20]}…) через 2captcha…")
            token = solve_recaptcha_v2(site_key, url)
            if token:
                # Вставляем токен в скрытое поле и сабмитим
                page.evaluate(f"""
                    document.getElementById('g-recaptcha-response').value = '{token}';
                    if (typeof onCaptchaSuccess === 'function') onCaptchaSuccess('{token}');
                    if (typeof grecaptcha !== 'undefined') {{
                        const cb = ___grecaptcha_cfg.clients[0]['l']['l']['callback'];
                        if (typeof cb === 'function') cb('{token}');
                    }}
                """)
                human_delay(2, 3)
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                print("  reCAPTCHA решена!")
                return True

    # Попытка 2: картиночная капча — делаем скриншот и отправляем в 2captcha
    captcha_img = page.query_selector("img[class*='captcha'], img[src*='captcha']")
    if captcha_img:
        print("  Решаем картиночную капчу через 2captcha…")
        img_bytes = captcha_img.screenshot()
        img_b64 = base64.b64encode(img_bytes).decode()
        answer = solve_image_captcha(img_b64)
        if answer:
            inp = page.query_selector("input[class*='captcha'], input[name*='captcha'], input[type='text']")
            if inp:
                inp.fill(answer)
                btn = page.query_selector("button[type='submit'], input[type='submit']")
                if btn:
                    btn.click()
                    human_delay(2, 3)
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    print("  Картиночная капча решена!")
                    return True

    print("  [!] Не удалось решить капчу автоматически")
    return False


# ---------------------------------------------------------------------------
# Браузер
# ---------------------------------------------------------------------------

def make_context(playwright, proxy: str = "") -> tuple:
    """Создаёт браузер и контекст с русскими настройками и опциональным прокси."""

    launch_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--disable-dev-shm-usage",
    ]

    proxy_cfg = None
    if proxy:
        # Playwright принимает proxy как {"server": "...", "username": ..., "password": ...}
        m = re.match(r"(\w+)://(?:([^:@]+):([^@]+)@)?(.+)", proxy)
        if m:
            scheme, user, pwd, host = m.groups()
            proxy_cfg = {"server": f"{scheme}://{host}"}
            if user:
                proxy_cfg["username"] = user
            if pwd:
                proxy_cfg["password"] = pwd
            print(f"  Прокси: {scheme}://{host}")

    browser = playwright.chromium.launch(
        headless=True,
        args=launch_args,
        proxy=proxy_cfg,
    )

    context = browser.new_context(
        viewport={"width": random.randint(1280, 1920), "height": random.randint(700, 900)},
        locale="ru-RU",
        timezone_id="Asia/Yekaterinburg",
        user_agent=random.choice(USER_AGENTS),
        extra_http_headers={
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        },
        # Отключаем WebRTC чтобы не утекал реальный IP через прокси
        ignore_https_errors=True,
    )

    # Максимально скрываем признаки автоматизации
    context.add_init_script("""
        // Убираем webdriver
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        // Эмулируем реальные плагины
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const arr = [
                    {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
                    {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
                    {name: 'Native Client', filename: 'internal-nacl-plugin'},
                ];
                arr.__proto__ = PluginArray.prototype;
                return arr;
            }
        });
        // Chrome runtime
        window.chrome = {
            runtime: {
                connect: () => {},
                sendMessage: () => {},
            },
            loadTimes: () => ({}),
            csi: () => ({}),
        };
        // Языки
        Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
        // Разрешения
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({state: Notification.permission})
                : origQuery(parameters);
    """)

    return browser, context


# ---------------------------------------------------------------------------
# Авито
# ---------------------------------------------------------------------------

def scrape_avito(context: BrowserContext, pages: int = 5) -> list[dict]:
    results = []
    page = context.new_page()
    try:
        # Прогреваем сессию
        page.goto("https://www.avito.ru/", wait_until="domcontentloaded", timeout=30000)
        handle_avito_captcha(page)
        human_delay(2, 4)

        for p in range(1, pages + 1):
            print(f"  Авито стр. {p}…")
            url = f"https://www.avito.ru/ekaterinburg/avtomobili?p={p}&s=104"
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            human_delay(2, 4)

            # Обрабатываем капчу если появилась
            if not handle_avito_captcha(page):
                print("  Авито: остановка из-за нерешённой капчи")
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

            human_delay(3, 6)
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

            containers = (
                page.query_selector_all("div[data-ftid='bulls-list_bull']")
                or page.query_selector_all("article.css-1nuvnlx")
                or page.query_selector_all("[class*='bull_']")
            )

            if not containers:
                links = page.query_selector_all("a[href*='drom.ru/auto']")
                for link in links:
                    href = link.get_attribute("href") or ""
                    title = link.inner_text().strip()
                    if title and href:
                        results.append({
                            "source": "drom", "title": title, "price": "",
                            "url": href, "date": "", "_date_parsed": None, "_photo_cnt": 0, "description": "",
                        })
                if not links:
                    break
                human_delay(1.5, 3)
                continue

            for card in containers:
                try:
                    link = (
                        card.query_selector("a[data-ftid='bull_title']")
                        or card.query_selector("h3 a")
                        or card.query_selector("a")
                    )
                    title = link.inner_text().strip() if link else ""
                    href = link.get_attribute("href") if link else ""
                    url_item = href if href and href.startswith("http") else ("https://ekaterinburg.drom.ru" + (href or ""))

                    price_el = card.query_selector("span[data-ftid='bull_price']") or card.query_selector("[class*='price']")
                    price = price_el.inner_text().strip() if price_el else ""

                    date_el = card.query_selector("[data-ftid='bull_date']")
                    date_text = date_el.inner_text().strip() if date_el else ""
                    date = parse_ru_date(date_text)

                    photo_cnt = 0
                    for sel in ["span[data-ftid='bull_images-count']", "[class*='images-count']", "[class*='photo-count']"]:
                        photo_el = card.query_selector(sel)
                        if photo_el:
                            m = re.search(r"(\d+)", photo_el.inner_text())
                            if m:
                                photo_cnt = int(m.group(1))
                                break
                    if photo_cnt == 0:
                        photo_cnt = len(card.query_selector_all("img"))

                    results.append({
                        "source": "drom", "title": title, "price": price,
                        "url": url_item, "date": str(date) if date else date_text,
                        "_date_parsed": date, "_photo_cnt": photo_cnt, "description": "",
                    })
                except Exception:
                    pass

            human_delay(1.5, 3)
    finally:
        page.close()

    print(f"  Дром: {len(results)} объявлений")
    return results


# ---------------------------------------------------------------------------
# Сбор, фильтрация, оценка
# ---------------------------------------------------------------------------

def fetch_all() -> list[dict]:
    all_items: list[dict] = []
    with sync_playwright() as pw:
        browser, context = make_context(pw, proxy=PROXY)
        try:
            scrapers = [
                ("Авито",   scrape_avito),
                ("Авто.ру", scrape_autoru),
                ("Дром",    scrape_drom),
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
        json.dumps(listings, ensure_ascii=False, indent=2), encoding="utf-8"
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
    print("=== Broker: авто Екатеринбург (Авито / Авто.ру / Дром) ===")
    if PROXY:
        print(f"Прокси: {PROXY}")
    if CAPTCHA_KEY:
        print("2captcha: включён")
    else:
        print("2captcha: отключён (CAPTCHA_KEY не задан)")

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
    import urllib.parse
    main()
