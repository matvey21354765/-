"""
tg_web.py — Авто-рассылка через Telegram Web (web.telegram.org)
Не требует API ключей — работает через браузер.

Установка:
    pip install playwright
    playwright install chromium

Первый запуск:
    python tg_web.py
    → Откроется браузер, отсканируй QR код телефоном
    → Сессия сохранится, повторно сканировать не нужно

Требования к listings.json:
    Нужен номер телефона продавца в поле "phone"
    Авито иногда скрывает номера — бот пропустит такие объявления
"""

import json
import time
import random
import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

# ============================================================
#  НАСТРОЙКИ
# ============================================================

LISTINGS_FILE = "listings.json"
DEALS_FILE = "deals.json"
SESSION_DIR = Path("tg_profile")   # папка с сессией браузера

# Сколько новых сообщений отправить за один запуск
MAX_NEW_PER_RUN = 15

# Пауза между сообщениями (секунды) — не торопись, иначе забанят
DELAY_BETWEEN = (90, 200)

# Первое сообщение
OPENER = "Здравствуйте\nещё продаёте ?"

# ============================================================

def load_deals():
    if Path(DEALS_FILE).exists():
        return json.loads(Path(DEALS_FILE).read_text(encoding="utf-8"))
    return {}

def save_deals(deals):
    Path(DEALS_FILE).write_text(json.dumps(deals, ensure_ascii=False, indent=2), encoding="utf-8")

def human_delay(min_s=1.0, max_s=3.0):
    time.sleep(random.uniform(min_s, max_s))

def send_message(page, phone: str, text: str) -> bool:
    """Открывает чат по номеру телефона и отправляет сообщение."""
    try:
        # Открываем чат через ссылку
        clean_phone = phone.replace("+", "").replace(" ", "").replace("-", "")
        page.goto(f"https://web.telegram.org/k/#?phone={clean_phone}", timeout=15000)
        human_delay(3, 6)

        # Ждём загрузки
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        human_delay(2, 4)

        # Ищем поле ввода
        input_box = page.locator('[contenteditable="true"].input-message-input').first
        if not input_box.is_visible(timeout=8000):
            print(f"  [!] Поле ввода не найдено для {phone}")
            return False

        input_box.click()
        human_delay(0.5, 1.5)

        # Вводим текст по символам (имитация печати)
        for line in text.split("\n"):
            for char in line:
                input_box.type(char, delay=random.randint(50, 150))
            # Shift+Enter для переноса строки (Enter — отправка)
            if line != text.split("\n")[-1]:
                input_box.press("Shift+Enter")
                human_delay(0.3, 0.7)

        human_delay(0.5, 1.5)

        # Отправляем Enter
        input_box.press("Enter")
        human_delay(1, 2)

        print(f"  ✓ Отправлено → {phone}")
        return True

    except Exception as e:
        print(f"  ✗ Ошибка ({phone}): {e}")
        return False


def run():
    SESSION_DIR.mkdir(exist_ok=True)

    listings = json.loads(Path(LISTINGS_FILE).read_text(encoding="utf-8"))
    deals = load_deals()

    # Фильтруем объявления у которых есть телефон и ещё не писали
    to_contact = []
    for item in listings:
        phone = item.get("phone") or item.get("seller_phone")
        if not phone:
            continue
        # Нормализуем телефон
        clean = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not clean.startswith("+"):
            clean = "+" + clean
        if clean in deals:
            continue
        item["_phone_clean"] = clean
        to_contact.append(item)

    print(f"Объявлений с телефоном: {len(to_contact)}")
    print(f"Будем писать (лимит {MAX_NEW_PER_RUN}): {min(len(to_contact), MAX_NEW_PER_RUN)}\n")

    if not to_contact:
        print("Нет новых контактов для отправки.")
        print("Убедись что в listings.json есть поле 'phone' у объявлений.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=False,   # обязательно видимый браузер
            args=["--no-sandbox"],
            viewport={"width": 1280, "height": 800},
        )

        page = browser.pages[0] if browser.pages else browser.new_page()

        # Открываем Telegram Web
        print("Открываю Telegram Web...")
        page.goto("https://web.telegram.org/k/", timeout=30000)

        # Ждём авторизацию
        print("\n⏳ Если первый запуск — отсканируй QR код в браузере.")
        print("   Жди пока загрузится список чатов...\n")

        # Ждём появления списка чатов (признак авторизации)
        try:
            page.wait_for_selector(".chatlist", timeout=120000)
            print("✅ Авторизован!\n")
        except:
            print("❌ Не удалось авторизоваться за 2 минуты. Попробуй снова.")
            browser.close()
            return

        human_delay(2, 4)

        sent_count = 0
        for item in to_contact[:MAX_NEW_PER_RUN]:
            phone = item["_phone_clean"]
            title = item.get("title", "")[:50]
            print(f"[{sent_count+1}/{MAX_NEW_PER_RUN}] {title} | {phone}")

            success = send_message(page, phone, OPENER)

            deals[phone] = {
                "stage": "opener" if success else "error",
                "listing": item.get("url"),
                "title": title,
                "sent": datetime.datetime.now().isoformat(),
            }
            save_deals(deals)

            if success:
                sent_count += 1
                if sent_count < MAX_NEW_PER_RUN:
                    delay = random.uniform(*DELAY_BETWEEN)
                    print(f"  [пауза {delay:.0f}с перед следующим]\n")
                    time.sleep(delay)

        print(f"\n✅ Готово! Отправлено: {sent_count} сообщений")
        print(f"Статусы сохранены в {DEALS_FILE}")

        input("\nНажми Enter чтобы закрыть браузер...")
        browser.close()


if __name__ == "__main__":
    run()
