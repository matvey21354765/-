"""
scraper_http.py — лёгкий HTTP скрапер без браузера
Работает на сервере, не требует Playwright для базового парсинга.
"""

import json
import re
import time
import random
import datetime
from pathlib import Path

try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    import urllib.request
    HAS_CLOUDSCRAPER = False

try:
    from bs4 import BeautifulSoup
    

MONTHS = {
    "янв":1,"фев":2,"мар":3,"апр":4,"май":5,"мая":5,
    "июн":6,"июл":7,"авг":8,"сен":9,"окт":10,"ноя":11,"дек":12,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.avito.ru/",
}

OUTPUT_FILE = "listings.json"

HOT_WORDS = re.compile(
    r"(срочно|торг|уступлю|снижу|скидка|дёшево|дешево)",
    re.IGNORECASE,
)


def make_session():
    if HAS_CLOUDSCRAPER:
        s = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        s.headers.update(HEADERS)
        return s
    return None


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
        day = int(m.group(1))
        mon = MONTHS.get(m.group(2)[:3].lower())
        if mon:
            try:
                return datetime.date(today.year, mon, day)
            except ValueError:
                pass
    return None


def hotness(title, photos, days, photos_known=True, days_known=True):
    # Больше фото = меньше срочности, больше дней = больше срочности
    photo_score = (5 - min(photos, 5)) * 2.0 if photos_known else 0
    days_score  = days * 0.3 if days_known else 0
    score = photo_score + days_score
    if HOT_WORDS.search(title):
        score += 10.0
    return round(score, 2)


def scrape_avito_http(pages=5) -> list[dict]:
    """Парсит Авито через HTTP без браузера."""
    if not HAS_BS4:
        print("[Авито HTTP] beautifulsoup4 не установлен")
        return []

    session = make_session()
    results = []

    for p in range(1, pages + 1):
        print(f"  Авито HTTP стр. {p}...")
        url = f"https://www.avito.ru/ekaterinburg/avtomobili?p={p}&s=104"
        try:
            if session:
                resp = session.get(url, timeout=20)
                html = resp.text
            else:
                req = urllib.request.Request(url, headers=HEADERS)
                html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="ignore")

            if "captcha" in html.lower() or "Доступ ограничен" in html:
                print("  [!] Авито заблокировал — капча")
                break

            soup = BeautifulSoup(html, "lxml")

            # Пробуем достать JSON из __initialData__
            script_data = None
            for script in soup.find_all("script"):
                text = script.string or ""
                if "initialData" in text or "catalog" in text:
                    m = re.search(r'"items"\s*:\s*(\[.*?\})\s*[,\}]', text, re.DOTALL)
                    if m:
                        try:
                            script_data = json.loads(m.group(1) + "]")
                            break
                        except Exception:
                            pass

            # Парсим HTML карточки
            cards = soup.select("[data-marker='item']")
            for card in cards:
                try:
                    title_el = card.select_one("[itemprop='name']") or card.select_one("h3")
                    title = title_el.get_text(strip=True) if title_el else ""

                    link_el = card.select_one("a[href*='/ekaterinburg/']")
                    href = link_el.get("href","") if link_el else ""
                    url_item = ("https://www.avito.ru" + href) if href else ""

                    price_el = card.select_one("[itemprop='price']") or card.select_one("[class*='price']")
                    price = ""
                    if price_el:
                        price = price_el.get("content") or price_el.get_text(strip=True)

                    date_el = card.select_one("[data-marker='item-date']") or card.select_one("span[class*='date']")
                    date_text = date_el.get_text(strip=True) if date_el else ""
                    date = parse_ru_date(date_text)
                    days = max(0, (datetime.date.today() - date).days) if date else 0

                    photos = len(card.select("img[src*='avito']"))

                    if title and url_item:
                        results.append({
                            "source": "avito",
                            "title": title,
                            "price": price,
                            "url": url_item,
                            "date": str(date) if date else date_text,
                            "_photos": photos,
                            "_days_on_site": days,
                            "_hot_score": hotness(title, photos, days),
                            "description": "",
                        })
                except Exception:
                    pass

            time.sleep(random.uniform(2, 5))

        except Exception as e:
            print(f"  [!] Ошибка стр.{p}: {e}")
            break

    print(f"  Авито HTTP: {len(results)} объявлений")
    return results


def scrape_drom_http(pages=30, start_page=1) -> list[dict]:
    """Парсит Дром через HTTP."""
    if not HAS_BS4:
        return []

    session = make_session()
    results = []

    for p in range(start_page, start_page + pages):
        print(f"  Дром HTTP стр. {p}...")
        url = ("https://ekaterinburg.drom.ru/auto/all/"
               if p == 1
               else f"https://ekaterinburg.drom.ru/auto/all/page{p}/")
        try:
            if session:
                resp = session.get(url, timeout=20)
                html = resp.text
            else:
                req = urllib.request.Request(url, headers={**HEADERS, "Referer": "https://ekaterinburg.drom.ru/"})
                html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="ignore")

            soup = BeautifulSoup(html, "lxml")
            cards = soup.select("div[data-ftid='bulls-list_bull']")

            if not cards:
                break

            for card in cards:
                try:
                    link = card.select_one("a[data-ftid='bull_title']") or card.select_one("h3 a")
                    title = link.get_text(strip=True) if link else ""
                    href = link.get("href","") if link else ""
                    url_item = href if href.startswith("http") else ("https://ekaterinburg.drom.ru" + href)

                    price_el = card.select_one("span[data-ftid='bull_price']")
                    price = price_el.get_text(strip=True) if price_el else ""

                    date_el = (
                        card.select_one("[data-ftid='bull_date']")
                        or card.select_one("span[class*='date']")
                        or card.select_one("time")
                    )
                    date_text = date_el.get_text(strip=True) if date_el else ""
                    date = parse_ru_date(date_text)
                    days_known = date is not None
                    days = max(0, (datetime.date.today() - date).days) if date else 0

                    photo_el = (
                        card.select_one("span[data-ftid='bull_images-count']")
                        or card.select_one("[class*='images-count']")
                        or card.select_one("[class*='photo']")
                    )
                    photos_str = photo_el.get_text() if photo_el else ""
                    photos_match = re.search(r"\d+", photos_str)
                    photos = int(photos_match.group()) if photos_match else 0
                    photos_known = photo_el is not None

                    # Имя продавца/компании
                    seller_el = (
                        card.select_one("[data-ftid='bull_seller']")
                        or card.select_one("a[class*='seller']")
                        or card.select_one("span[class*='seller']")
                        or card.select_one("[class*='Seller']")
                    )
                    seller = seller_el.get_text(strip=True) if seller_el else ""

                    # Описание из карточки
                    desc_el = (
                        card.select_one("[data-ftid='bull_description']")
                        or card.select_one("p[class*='description']")
                        or card.select_one("div[class*='description']")
                    )
                    desc = desc_el.get_text(strip=True) if desc_el else ""

                    if title and url_item:
                        results.append({
                            "source": "drom",
                            "title": title,
                            "price": price,
                            "url": url_item,
                            "date": str(date) if date else date_text,
                            "_photos": photos,
                            "_days_on_site": days,
                            "_hot_score": hotness(title, photos, days, photos_known, days_known),
                            "description": f"{seller} {desc}".strip(),
                        })
                except Exception:
                    pass

            time.sleep(random.uniform(1, 3))

        except Exception as e:
            print(f"  [!] Ошибка стр.{p}: {e}")
            break

    print(f"  Дром HTTP: {len(results)} объявлений")
    return results


def merge_and_save(new_items: list[dict]) -> list[dict]:
    """Объединяет с существующим listings.json и сохраняет."""
    existing = {}
    if Path(OUTPUT_FILE).exists():
        try:
            for item in json.loads(Path(OUTPUT_FILE).read_text(encoding="utf-8")):
                if item.get("url"):
                    existing[item["url"]] = item
        except Exception:
            pass

    new_count = sum(1 for i in new_items if i.get("url") and i["url"] not in existing)
    for item in new_items:
        if item.get("url"):
            existing[item["url"]] = item

    merged = sorted(existing.values(), key=lambda x: x.get("_hot_score", 0), reverse=True)
    Path(OUTPUT_FILE).write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged, new_count


if __name__ == "__main__":
    print("Авито...")
    avito = scrape_avito_http(pages=5)
    print("Дром...")
    drom = scrape_drom_http(pages=10)
    merged, new_count = merge_and_save(avito + drom)
    print(f"Готово: {len(merged)} всего, {new_count} новых")
