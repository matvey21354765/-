"""
avito_daemon.py — фоновый сканер Авито для Windows.
Запусти один раз, оставь работать в фоне.
Каждые 2 часа открывает браузер, парсит Авито, отправляет результаты боту.

Установка (один раз):
    pip install playwright requests beautifulsoup4 lxml
    playwright install chromium

Запуск:
    python avito_daemon.py

Чтобы запускался автоматически при старте Windows:
    Нажми Win+R → shell:startup → скопируй avito_daemon.bat туда
"""

import json
import re
import time
import random
import datetime
import sys
from pathlib import Path

BOT_TOKEN = "8657191103:AAFBXaObKV2jcLbBsBzpYTuBfBj2bBkymrk"
MY_CHAT_ID = 749256529
PRICE_MIN = 500_000
PRICE_MAX = 1_000_000
PAGES = 10
INTERVAL_HOURS = 2  # Интервал сканирования

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

SEEN_FILE = Path("avito_seen.json")


def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen), ensure_ascii=False), encoding="utf-8")


def parse_price(s):
    digits = re.sub(r"[^\d]", "", str(s or ""))
    return int(digits) if digits else None


def is_dealer(title):
    return any(k in title.lower() for k in DEALER_KEYWORDS)


def parse_date(text):
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
        mon = MONTHS.get(m.group(2)[:3].lower())
        if mon:
            try: return datetime.date(today.year, mon, int(m.group(1)))
            except ValueError: pass
    return None


def send_file_to_bot(items):
    import requests
    out = Path("avito_results.json")
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    with open(out, "rb") as f:
        r = requests.post(url, data={"chat_id": MY_CHAT_ID}, files={"document": ("avito_results.json", f)})
    return r.status_code == 200


def send_message(text):
    import requests
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": MY_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )


def scrape_once():
    from playwright.sync_api import sync_playwright
    from bs4 import BeautifulSoup

    results = []
    today = datetime.date.today()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,  # фоновый режим — окно не открывается
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="ru-RU",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        for p in range(1, PAGES + 1):
            url = f"https://www.avito.ru/ekaterinburg/avtomobili?p={p}&s=104"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(2, 4))

                html = page.content()
                title_tag = page.title()
                is_blocked = (
                    "Доступ ограничен" in html
                    or "captcha" in title_tag.lower()
                    or page.query_selector("iframe[src*='smartcaptcha']") is not None
                    or "Подтвердите, что вы не робот" in html
                )
                if is_blocked:
                    print(f"  [!] Авито блок на стр.{p}")
                    send_message("⚠️ Авито показало капчу на локальном парсере. Открой браузер и запусти `local_avito_scraper.py` вручную.")
                    break

                soup = BeautifulSoup(html, "lxml")
                cards = soup.select("[data-marker='item']")
                if not cards:
                    break

                for card in cards:
                    try:
                        title_el = card.select_one("[itemprop='name']") or card.select_one("h3")
                        title = title_el.get_text(strip=True) if title_el else ""
                        if not title or is_dealer(title):
                            continue

                        link_el = card.select_one("a[href*='/ekaterinburg/']")
                        href = link_el.get("href", "") if link_el else ""
                        item_url = ("https://www.avito.ru" + href) if href else ""
                        if not item_url:
                            continue

                        price_el = card.select_one("[itemprop='price']") or card.select_one("[class*='price']")
                        price = ""
                        if price_el:
                            price = price_el.get("content") or price_el.get_text(strip=True)
                        price_val = parse_price(price)
                        if price_val and (price_val < PRICE_MIN or price_val > PRICE_MAX):
                            continue

                        date_el = card.select_one("[data-marker='item-date']") or card.select_one("span[class*='date']")
                        date_text = date_el.get_text(strip=True) if date_el else ""
                        date = parse_date(date_text)
                        days = max(0, (today - date).days) if date else 0
                        photos = len(card.select("img[src*='avito']"))
                        score = (5 - min(photos, 5)) * 2.0 + days * 0.3 + (10 if HOT.search(title) else 0)

                        results.append({
                            "source": "avito", "title": title, "price": price,
                            "url": item_url, "date": str(date) if date else date_text,
                            "_photos": photos, "_days_on_site": days,
                            "_hot_score": round(score, 2), "description": "",
                        })
                    except Exception:
                        pass

                time.sleep(random.uniform(2, 3))
            except Exception as e:
                print(f"  [!] стр.{p}: {e}")
                break

        browser.close()

    return results


def run():
    print(f"🤖 Авито демон запущен. Сканирование каждые {INTERVAL_HOURS} часа.")
    print("   Оставь это окно открытым (можно свернуть).\n")
    send_message(f"🤖 Авито демон запущен на твоём ПК. Буду сканировать каждые {INTERVAL_HOURS} ч.")

    while True:
        now = datetime.datetime.now().strftime("%H:%M")
        print(f"[{now}] Сканирую Авито...")

        seen = load_seen()
        try:
            items = scrape_once()
        except Exception as e:
            print(f"  Ошибка: {e}")
            items = []

        if items:
            new_items = [i for i in items if i["url"] not in seen]
            for i in items:
                seen.add(i["url"])
            save_seen(seen)

            print(f"  Найдено: {len(items)}, новых: {len(new_items)}")

            if new_items:
                # Отправляем файл боту — он добавит в базу
                ok = send_file_to_bot(new_items)
                if ok:
                    send_message(f"✅ Авито: найдено {len(new_items)} новых объявлений!\nНажми /new чтобы посмотреть.")
                    print(f"  Отправлено боту: {len(new_items)}")
                else:
                    print("  Ошибка отправки боту")
            else:
                print("  Новых нет")
        else:
            print("  Ничего не найдено")

        next_time = (datetime.datetime.now() + datetime.timedelta(hours=INTERVAL_HOURS)).strftime("%H:%M")
        print(f"  Следующее сканирование в {next_time}\n")
        time.sleep(INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    try:
        from bs4 import BeautifulSoup
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ Установи зависимости:")
        print("   pip install playwright requests beautifulsoup4 lxml")
        print("   playwright install chromium")
        sys.exit(1)
    run()
