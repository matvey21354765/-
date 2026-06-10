import json
import time
import datetime
import http.server
import threading
import re
from pathlib import Path
from apify_client import ApifyClient

APIFY_TOKEN = "apify_api_regUALs4h8tE8QjOfxOOBvFaP6dBJd0VT24z"
OUTPUT_FILE = "listings.json"
PORT = 8000

MIN_DAYS_POSTED = 14
MAX_PHOTOS = 3

HOT_WORDS = re.compile(r"\b(срочно|срочная|торг|торгуюсь|уступлю|снижу|скидка)\b", re.IGNORECASE)


def fetch_listings() -> list[dict]:
    print("Запуск Apify актора tugkan/avito-scraper ...")
    client = ApifyClient(APIFY_TOKEN)

    run = client.actor("tugkan/avito-scraper").call(
        run_input={
            "startUrls": [
                {
                    "url": (
                        "https://www.avito.ru/ekaterinburg/avtomobili"
                        "?cd=1&s=104"  # сортировка по дате (новые сначала)
                    )
                }
            ],
            "maxItems": 500,
            "proxyConfiguration": {"useApifyProxy": True},
        }
    )

    items = []
    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        items.append(item)

    print(f"Получено объявлений: {len(items)}")
    return items


def parse_date(raw: str | None) -> datetime.date | None:
    """Пробует распарсить дату из разных форматов Авито."""
    if not raw:
        return None
    # ISO формат
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(raw[:19], fmt[:len(fmt)]).date()
        except ValueError:
            pass
    # Текстовый формат: «10 июня», «10 июн.»
    months = {
        "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5,
        "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    }
    m = re.search(r"(\d{1,2})\s+([а-яё]+)", raw, re.IGNORECASE)
    if m:
        day = int(m.group(1))
        mon_str = m.group(2)[:3].lower()
        month = months.get(mon_str)
        if month:
            year = datetime.date.today().year
            try:
                return datetime.date(year, month, day)
            except ValueError:
                pass
    return None


def photo_count(item: dict) -> int:
    photos = item.get("images") or item.get("photos") or item.get("imageUrls") or []
    if isinstance(photos, list):
        return len(photos)
    return 0


def days_on_site(item: dict) -> int:
    raw = item.get("date") or item.get("publishedAt") or item.get("createdAt") or ""
    d = parse_date(str(raw))
    if d is None:
        return 0
    return (datetime.date.today() - d).days


def hotness_score(item: dict, photos: int, days: int) -> float:
    """Чем выше — тем горячее (больше шансов на торг)."""
    score = 0.0
    # Мало фото → продавец не заинтересован в красивой подаче
    score += (MAX_PHOTOS - photos) * 2
    # Долго висит → давление продать
    score += days * 0.3
    # Ключевые слова в заголовке/описании
    title = item.get("title", "") or ""
    desc = item.get("description", "") or ""
    if HOT_WORDS.search(title + " " + desc):
        score += 10
    return round(score, 2)


def filter_and_score(items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        photos = photo_count(item)
        days = days_on_site(item)
        if photos > MAX_PHOTOS:
            continue
        if days < MIN_DAYS_POSTED:
            continue
        item["_photos"] = photos
        item["_days_on_site"] = days
        item["_hot_score"] = hotness_score(item, photos, days)
        result.append(item)

    result.sort(key=lambda x: x["_hot_score"], reverse=True)
    print(f"После фильтрации: {len(result)} объявлений")
    return result


def save(listings: list[dict]) -> None:
    Path(OUTPUT_FILE).write_text(
        json.dumps(listings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Сохранено в {OUTPUT_FILE}")


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
    print(f"Веб-сервер запущен: http://localhost:{PORT}/listings.json")
    server.serve_forever()


def main():
    print("=== Broker: парсинг Авито Екатеринбург ===")
    items = fetch_listings()
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
