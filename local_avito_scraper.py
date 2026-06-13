"""
local_avito_scraper.py — запускать на своём компьютере.
Открывает браузер Chrome, парсит Авито и отправляет результаты боту.

Установка (один раз):
    pip install playwright requests
    playwright install chromium

Запуск:
    python local_avito_scraper.py
"""

import json
import re
import time
import random
import datetime
from pathlib import Path

BOT_TOKEN = "8657191103:AAFBXaObKV2jcLbBsBzpYTuBfBj2bBkymrk"
MY_CHAT_ID = 749256529
PRICE_MIN = 500_000
PRICE_MAX = 1_000_000
PAGES = 10

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


def send_to_bot(items):
    import requests
    out = Path("avito_results.json")
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    with open(out, "rb") as f:
        r = requests.post(url, data={"chat_id": MY_CHAT_ID}, files={"document": ("avito_results.json", f)})
    if r.status_code == 200:
        print(f"✅ Отправлено боту: {len(items)} объявлений")
    else:
        print(f"❌ Ошибка отправки: {r.text[:200]}")


def scrape():
    from playwright.sync_api import sync_playwright
    from bs4 import BeautifulSoup

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
            url = f"https://www.avito.ru/ekaterinburg/avtomobili?p={p}&s=104"
            print(f"  Страница {p}...", end=" ", flush=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(2, 3))

                # Капча?
                html = page.content()
                if "captcha" in html.lower() or "Доступ ограничен" in html:
                    print(f"\n⚠️ Капча на стр.{p}! Реши в открытом браузере, затем нажми Enter...")
                    input()
                    html = page.content()
                    if "captcha" in html.lower() or "Доступ ограничен" in html:
                        print("Всё ещё заблокировано, останавливаюсь.")
                        break

                soup = BeautifulSoup(html, "lxml")
                cards = soup.select("[data-marker='item']")
                if not cards:
                    print("нет карточек, конец")
                    break

                page_ok = 0
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
                            "source": "avito",
                            "title": title,
                            "price": price,
                            "url": item_url,
                            "date": str(date) if date else date_text,
                            "_photos": photos,
                            "_days_on_site": days,
                            "_hot_score": round(score, 2),
                            "description": "",
                        })
                        page_ok += 1
                    except Exception:
                        pass

                print(f"{page_ok} объявлений")
                time.sleep(random.uniform(2, 4))

            except Exception as e:
                print(f"❌ {e}")
                break

        browser.close()

    return results


if __name__ == "__main__":
    print(f"🔍 Парсю Авито Екатеринбург, цена {PRICE_MIN//1000}–{PRICE_MAX//1000} тыс. руб...")
    print("Откроется окно браузера — не закрывай его!\n")

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("❌ Установи зависимости:")
        print("   pip install playwright requests beautifulsoup4 lxml")
        print("   playwright install chromium")
        exit(1)

    items = scrape()
    print(f"\nНайдено: {len(items)} подходящих объявлений")
    if items:
        send_to_bot(items)
    else:
        print("Ничего не найдено.")
