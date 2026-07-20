"""
local_avito_scraper.py — запускать на своём компьютере (обычный домашний IP,
не датацентр — Авито блокирует серверные IP, поэтому прямой парсинг с сервера
бота не работает).

Открывает браузер Chrome, парсит Авито по выбранному региону и отправляет
результат боту документом — бот сам подхватывает файл и подмешивает эти
объявления в обычный поиск (см. on_document/load_external_avito в control_bot.py).

Установка (один раз):
    pip install playwright requests beautifulsoup4 lxml python-dotenv
    playwright install chromium

Запуск:
    python local_avito_scraper.py ekaterinburg
    (или просто `python local_avito_scraper.py` — спросит регион)
"""

import json
import os
import re
import sys
import time
import random
import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Тот же токен, что и у control_bot.py (берётся из .env / переменной окружения,
# чтобы файл с локальным скрапером не хранил отдельный секрет).
BOT_TOKEN = os.getenv("BOT_TOKEN", "8923014188:AAHvNW2B5fin2XCmbVhlaLNjWhLwI3JhZ90")
# В .env у бота переменная называется ADMIN_IDS (список через запятую),
# поэтому берём первый ID оттуда, иначе дефолт.
_ADMINS_RAW = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or "749256529"
ADMIN_ID = int(_ADMINS_RAW.split(",")[0].strip())
PAGES = int(os.getenv("AVITO_PAGES", "10"))

REGIONS = {
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

DEALER_KEYWORDS = [
    "ооо", "ип ", "ао ", "зао ", "автосалон", "официальный дилер",
    "дилер", "автоцентр", "trade-in", "трейд-ин", "автохолдинг",
    "рольф", "major", "lada", "колёса даром", "автопланета",
    "автоград", "июль", "автоленд", "favorit", "фаворит",
    "авто плюс", "автоплюс", "fresh auto", "автобан", "автосфера",
    "арконт", "ключавто", "авилон", "петровский", "прагматика",
    "бизнес кар", "максимум", "мотус", "genser", "генсер",
    "кредит от", "автоподбор", "выкуп авто", "автовыкуп",
]

MONTHS = {
    "янв":1,"фев":2,"мар":3,"апр":4,"май":5,"мая":5,
    "июн":6,"июл":7,"авг":8,"сен":9,"окт":10,"ноя":11,"дек":12,
}

HOT = re.compile(r"(срочно|торг|уступлю|снижу|скидка|дёшево|дешево)", re.IGNORECASE)


def parse_price(s):
    digits = re.sub(r"[^\d]", "", str(s or ""))
    return int(digits) if digits else None


def is_dealer(title):
    text = title.lower()
    return any(k in text for k in DEALER_KEYWORDS)


def parse_date(text):
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
    if re.search(r"\d+\s+(час|мин)", low):
        return today
    m = re.search(r"(\d{1,2})\s+([а-яё]+)", text, re.IGNORECASE)
    if m:
        mon = MONTHS.get(m.group(2)[:3].lower())
        if mon:
            try:
                return datetime.date(today.year, mon, int(m.group(1)))
            except ValueError:
                pass
    return None


def send_to_bot(region, items):
    import requests
    fname = f"avito_{region}.json"
    out = Path(fname)
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    with open(out, "rb") as f:
        r = requests.post(url, data={"chat_id": ADMIN_ID}, files={"document": (fname, f)})
    if r.status_code == 200:
        print(f"✅ Отправлено боту: {len(items)} объявлений ({region})")
    else:
        print(f"❌ Ошибка отправки: {r.text[:200]}")


def scrape(region):
    from playwright.sync_api import sync_playwright
    from bs4 import BeautifulSoup

    slug = REGIONS[region]
    results = []
    today = datetime.date.today()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,  # видимый браузер — можно решить капчу вручную
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="ru-RU",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        for p in range(1, PAGES + 1):
            url = f"https://www.avito.ru/{slug}/avtomobili?p={p}&s=104"
            print(f"  Страница {p}...", end=" ", flush=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(2, 3))

                html = page.content()
                title = page.title()
                is_blocked = (
                    "Доступ ограничен" in html
                    or "captcha" in title.lower()
                    or page.query_selector("div[class*='captcha-wrapper']") is not None
                    or page.query_selector("iframe[src*='smartcaptcha']") is not None
                    or "Подтвердите, что вы не робот" in html
                )
                if is_blocked:
                    print(f"\n⚠️ Капча на стр.{p}! Реши в открытом браузере, затем нажми Enter...")
                    input()
                    html = page.content()
                    title = page.title()
                    if "Доступ ограничен" in html or "captcha" in title.lower() or "Подтвердите" in html:
                        print("Всё ещё заблокировано — пропускаю страницу, иду дальше.")
                        continue

                soup = BeautifulSoup(html, "lxml")
                # Несколько вариантов селектора карточки — Авито часто меняет вёрстку
                cards = soup.select("[data-marker='item']") or soup.select("[class*='iva-item-root']") or soup.select("div[itemtype='http://schema.org/Product']")
                if not cards:
                    print("нет карточек, конец")
                    break

                page_ok = 0
                title_candidates = [
                    "[itemprop='name']", "h3",
                    "[class*='iva-item-title']",
                    "[class*='title']",
                    "a[class*='title']",
                    "span[class*='title']",
                    "div[class*='description'] h3",
                    "meta[itemprop='name']",
                ]
                price_candidates = [
                    "[itemprop='price']",
                    "[class*='price']",
                    "[class*='iva-item-price']",
                    "meta[itemprop='price']",
                ]
                for card in cards:
                    try:
                        # Заголовок: ищем по нескольким селекторам, иначе по тексту ссылки
                        title_el = None
                        for sel in title_candidates:
                            title_el = card.select_one(sel)
                            if title_el:
                                break
                        if title_el and title_el.name == "meta":
                            title = title_el.get("content", "").strip()
                        else:
                            title = title_el.get_text(strip=True) if title_el else ""
                        if not title:
                            # запасной вариант — первая ссылка с длинным текстом
                            for a in card.select("a"):
                                t = a.get_text(strip=True)
                                if len(t) > 8:
                                    title = t
                                    break
                        if not title or is_dealer(title):
                            continue

                        link_el = card.select_one(f"a[href*='/{slug.split('_')[0]}']") or card.select_one("a[itemprop='url']") or card.select_one("a[href*='avito.ru']") or card.select_one("a")
                        href = link_el.get("href", "") if link_el else ""
                        if href and not href.startswith("http"):
                            item_url = "https://www.avito.ru" + href
                        else:
                            item_url = href
                        if not item_url or "avito.ru" not in item_url:
                            continue

                        price_el = None
                        for sel in price_candidates:
                            price_el = card.select_one(sel)
                            if price_el:
                                break
                        price = ""
                        if price_el:
                            price = price_el.get("content") or price_el.get_text(strip=True)

                        img_el = card.select_one("img")
                        photo_url = ""
                        if img_el:
                            src = img_el.get("src") or img_el.get("data-src") or ""
                            if src.startswith("//"):
                                src = "https:" + src
                            if src.startswith("http"):
                                photo_url = src

                        desc_el = card.select_one("[itemprop='description']") or card.select_one("[class*='iva-item-text']") or card.select_one("[class*='description']")
                        description = desc_el.get_text(strip=True) if desc_el else ""

                        date_el = card.select_one("[data-marker='item-date']") or card.select_one("span[class*='date']")
                        date_text = date_el.get_text(strip=True) if date_el else ""
                        date = parse_date(date_text)
                        days = max(0, (today - date).days) if date else 0
                        score = days * 0.3 + (10 if HOT.search(title) else 0)

                        _price_int = parse_price(price)
                        _year = 0
                        ym = re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", title)
                        if ym:
                            _year = int(ym.group(1))

                        results.append({
                            "source": "avito",
                            "title": title,
                            "price": price,
                            "url": item_url,
                            "date": str(date) if date else date_text,
                            "_photos": 1 if photo_url else 0,
                            "_photo_url": photo_url,
                            "_days_on_site": days,
                            "_hot_score": round(score, 2),
                            "_price_int": _price_int or 0,
                            "_year": _year,
                            "description": description,
                            "seller": "",
                        })
                        page_ok += 1
                    except Exception:
                        pass

                print(f"{page_ok} объявлений (всего накоплено: {len(results)})")
                time.sleep(random.uniform(2, 4))

            except Exception as e:
                print(f"❌ {e}")
                break

        browser.close()

    return results


if __name__ == "__main__":
    try:
        from bs4 import BeautifulSoup  # noqa: F401
    except ImportError:
        print("❌ Установи зависимости:")
        print("   pip install playwright requests beautifulsoup4 lxml python-dotenv")
        print("   playwright install chromium")
        sys.exit(1)

    # Авто-цикл для VPS/сервера 24/7: python local_avito_scraper.py ekaterinburg --loop
    loop_mode = "--loop" in sys.argv
    interval_h = int(os.getenv("SCRAPER_LOOP_HOURS", "8"))

    region = sys.argv[1] if len(sys.argv) > 1 else ""
    if region not in REGIONS:
        print("Доступные регионы: " + ", ".join(REGIONS))
        region = input("Введите регион: ").strip()
    if region not in REGIONS:
        print(f"❌ Неизвестный регион: {region}")
        sys.exit(1)

    if loop_mode:
        print(f"🔁 Авто-цикл: каждые {interval_h}ч, регион {region}")
        while True:
            try:
                print(f"\n=== {datetime.datetime.now()} ===")
                print(f"🔍 Парсю Авито: {region}...")
                items = scrape(region)
                print(f"Найдено: {len(items)} подходящих объявлений")
                if items:
                    send_to_bot(region, items)
                else:
                    print("Ничего не найдено, жду следующий цикл.")
            except Exception as e:
                print(f"❌ Ошибка цикла: {e}")
            print(f"⏳ Жду {interval_h}ч до следующего запуска...")
            time.sleep(interval_h * 3600)
    else:
        print(f"🔍 Парсю Авито: {region}...")
        print("Откроется окно браузера — не закрывай его!\n")

        items = scrape(region)
        print(f"\nНайдено: {len(items)} подходящих объявлений")
        if items:
            send_to_bot(region, items)
        else:
            print("Ничего не найдено.")
