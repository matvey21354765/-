"""
tg_web.py — Telegram-бот брокера через Telegram Web
Не требует API ключей. Работает через браузер.

Установка:
    pip install playwright
    playwright install chromium

Запуск:
    python tg_web.py

Первый раз — отсканируй QR код, потом бот работает сам.
"""

import json
import time
import random
import datetime
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

# ============================================================
#  НАСТРОЙКИ
# ============================================================

LISTINGS_FILE  = "listings.json"
DEALS_FILE     = "deals.json"
SESSION_DIR    = Path("tg_profile")

MAX_NEW_PER_RUN   = 15       # сколько новых диалогов открывать за запуск
DELAY_NEW_MIN     = 90       # мин. пауза между новыми сообщениями (сек)
DELAY_NEW_MAX     = 200
REPLY_DELAY_MIN   = 15       # мин. пауза перед ответом на входящее
REPLY_DELAY_MAX   = 45
POLL_INTERVAL     = 30       # как часто проверять новые ответы (сек)

# ============================================================
#  СЦЕНАРИЙ ДИАЛОГА
# ============================================================

OPENER = "Здравствуйте\nещё продаёте ?"

# После каждого ответа продавца бот смотрит на стадию и решает что писать
STAGES = {
    "opener":       "offer",
    "offer":        "price_ask",
    "price_ask":    "deal",
    "deal":         "details",
    "details":      "photos",
    "photos":       "active",
    "active":       None,
}

REPLIES = {
    "offer": (
        "Могу дополнительно продвигать её через свои соцсети и искать покупателей. "
        "Если клиент приходит через меня — беру комиссию после сделки. Можем попробовать поработать."
    ),
    "price_ask": "Скажите свою последнюю цену",
    "deal": (
        "Можем тогда если найду покупателя выше вашей крайней цены, "
        "все что сверху себе возьму. Без наглости 🙂"
    ),
    "details": (
        "по машине есть какие то повреждение по кузову и есть ли запрет ?\n"
        "Проверьте машину в Автотеке. Отчёт может показать:\n"
        "→ ДТП и повреждения\n"
        "→ скрутки пробега\n"
        "→ работу в такси\n"
        "→ ограничения на регистрацию\n"
        "→ залог и многое другое\n\n"
        "Проверить от 115 ₽"
    ),
    "photos": "Можете тогда какие нибудь другие, но хорошие фотографии с машиной ещё скинуть",
    "active": "Хорошо, если что напишу вам 👍",
}

# Ключевые слова отказа
REFUSE_WORDS = ["нет", "не надо", "не интересно", "не нужно", "продали",
                "продала", "продал", "снял", "сняла", "уже", "сами справимся"]

# ============================================================
#  ХРАНИЛИЩЕ
# ============================================================

def load_deals() -> dict:
    if Path(DEALS_FILE).exists():
        return json.loads(Path(DEALS_FILE).read_text(encoding="utf-8"))
    return {}

def save_deals(deals: dict) -> None:
    Path(DEALS_FILE).write_text(
        json.dumps(deals, ensure_ascii=False, indent=2), encoding="utf-8"
    )

# ============================================================
#  TELEGRAM WEB
# ============================================================

def human_delay(a=1.0, b=3.0):
    time.sleep(random.uniform(a, b))

def type_message(input_box, text: str):
    """Печатает текст как человек, Shift+Enter для переносов строк."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        for char in line:
            input_box.type(char, delay=random.randint(40, 120))
        if i < len(lines) - 1:
            input_box.press("Shift+Enter")
            human_delay(0.2, 0.5)

def open_chat(page, phone: str) -> bool:
    """Открывает чат по номеру телефона."""
    try:
        clean = re.sub(r"[^\d]", "", phone)
        page.goto(f"https://web.telegram.org/k/#?phone={clean}", timeout=15000)
        human_delay(3, 5)
        return True
    except Exception as e:
        print(f"  [!] Не открыть чат {phone}: {e}")
        return False

def get_last_incoming_message(page) -> str | None:
    """Возвращает текст последнего входящего сообщения в открытом чате."""
    try:
        # Ждём немного чтобы страница обновилась
        page.wait_for_load_state("domcontentloaded", timeout=5000)
        # Берём все входящие сообщения (не наши)
        messages = page.query_selector_all(".message.is-in .message")
        if not messages:
            messages = page.query_selector_all(".bubbles-inner .bubble.is-in .message")
        if not messages:
            # Альтернативный селектор
            messages = page.query_selector_all("[class*='bubble'][class*='is-in']")
        if messages:
            last = messages[-1]
            text = last.inner_text().strip()
            return text if text else None
    except Exception:
        pass
    return None

def get_message_count(page) -> int:
    """Считает количество сообщений в чате."""
    try:
        msgs = page.query_selector_all(".bubble")
        return len(msgs)
    except Exception:
        return 0

def send_text(page, text: str) -> bool:
    """Отправляет сообщение в открытый чат."""
    try:
        input_box = page.locator('[contenteditable="true"].input-message-input').first
        if not input_box.is_visible(timeout=8000):
            # Попробуем кликнуть по зоне ввода
            page.click(".input-message-container", timeout=5000)
            input_box = page.locator('[contenteditable="true"]').last
            if not input_box.is_visible(timeout=5000):
                return False

        input_box.click()
        human_delay(0.5, 1.2)
        type_message(input_box, text)
        human_delay(0.5, 1.0)
        input_box.press("Enter")
        human_delay(1, 2)
        print(f"  → Отправлено: {text[:70].replace(chr(10), ' ')}...")
        return True
    except Exception as e:
        print(f"  [!] Ошибка отправки: {e}")
        return False

def is_refuse(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in REFUSE_WORDS)

# ============================================================
#  ОСНОВНОЙ ЦИКЛ
# ============================================================

def run():
    SESSION_DIR.mkdir(exist_ok=True)

    # Проверяем listings.json
    if not Path(LISTINGS_FILE).exists():
        print(f"❌ Файл {LISTINGS_FILE} не найден!")
        print("   Сначала запусти: python broker.py")
        return

    listings = json.loads(Path(LISTINGS_FILE).read_text(encoding="utf-8"))
    deals = load_deals()

    # Собираем контакты с телефонами которым ещё не писали
    to_contact = []
    for item in listings:
        phone = item.get("phone") or item.get("seller_phone")
        if not phone:
            continue
        clean = "+" + re.sub(r"[^\d]", "", phone)
        if len(clean) < 7:
            continue
        if clean not in deals:
            item["_phone"] = clean
            to_contact.append(item)

    print(f"📋 Объявлений с телефоном: {len(to_contact)}")
    print(f"   Напишем сегодня: {min(len(to_contact), MAX_NEW_PER_RUN)}")
    print(f"   Уже в работе: {len([d for d in deals.values() if d.get('stage') not in ('closed','done')])}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
        )

        page = browser.pages[0] if browser.pages else browser.new_page()

        # Авторизация
        print("🌐 Открываю Telegram Web...")
        page.goto("https://web.telegram.org/k/", timeout=30000)
        print("⏳ Жди загрузки... (если первый раз — отсканируй QR код)\n")

        try:
            page.wait_for_selector(".chatlist", timeout=120000)
            print("✅ Авторизован!\n")
        except Exception:
            print("❌ Не авторизовался за 2 минуты.")
            browser.close()
            return

        human_delay(2, 3)

        # ── Шаг 1: отправляем первые сообщения новым контактам ──────────────
        sent_count = 0
        for item in to_contact[:MAX_NEW_PER_RUN]:
            phone = item["_phone"]
            title = item.get("title", "")[:50]
            print(f"[{sent_count+1}] {title}")
            print(f"    Телефон: {phone}")

            if not open_chat(page, phone):
                continue

            # Проверяем что чат открылся (есть поле ввода)
            try:
                page.wait_for_selector('[contenteditable="true"].input-message-input',
                                       timeout=8000)
            except Exception:
                print(f"    [!] Чат не открылся — пропускаем")
                deals[phone] = {"stage": "error", "title": title,
                                "listing": item.get("url"),
                                "sent": datetime.datetime.now().isoformat()}
                save_deals(deals)
                continue

            success = send_text(page, OPENER)

            deals[phone] = {
                "stage": "opener" if success else "error",
                "title": title,
                "listing": item.get("url"),
                "last_msg_count": get_message_count(page),
                "sent": datetime.datetime.now().isoformat(),
                "updated": datetime.datetime.now().isoformat(),
            }
            save_deals(deals)

            if success:
                sent_count += 1
                if sent_count < min(len(to_contact), MAX_NEW_PER_RUN):
                    delay = random.uniform(DELAY_NEW_MIN, DELAY_NEW_MAX)
                    print(f"    ⏱ Пауза {delay:.0f}с...\n")
                    time.sleep(delay)

        print(f"\n✅ Новых сообщений отправлено: {sent_count}")
        print(f"🔄 Запускаю мониторинг ответов (каждые {POLL_INTERVAL}с)...")
        print("   Нажми Ctrl+C чтобы остановить\n")

        # ── Шаг 2: бесконечный цикл — проверяем ответы ──────────────────────
        while True:
            try:
                active_deals = {
                    phone: deal for phone, deal in deals.items()
                    if deal.get("stage") not in ("closed", "done", "error", None)
                }

                for phone, deal in active_deals.items():
                    stage = deal.get("stage")
                    next_stage = STAGES.get(stage)

                    if next_stage is None:
                        continue  # диалог завершён

                    # Открываем чат
                    if not open_chat(page, phone):
                        continue

                    try:
                        page.wait_for_selector('[contenteditable="true"]',
                                               timeout=6000)
                    except Exception:
                        continue

                    # Проверяем новые сообщения
                    current_count = get_message_count(page)
                    last_count = deal.get("last_msg_count", 0)

                    if current_count <= last_count:
                        continue  # новых сообщений нет

                    # Есть новое сообщение
                    last_msg = get_last_incoming_message(page)
                    if not last_msg:
                        deal["last_msg_count"] = current_count
                        save_deals(deals)
                        continue

                    print(f"\n📨 [{deal.get('title','')[:30]}] {phone}")
                    print(f"   Продавец: {last_msg[:100]}")

                    # Отказ?
                    if is_refuse(last_msg):
                        print(f"   → Отказ. Закрываем.")
                        deal["stage"] = "closed"
                        deal["last_msg_count"] = current_count
                        deal["updated"] = datetime.datetime.now().isoformat()
                        save_deals(deals)
                        continue

                    # Определяем следующий ответ
                    reply_text = REPLIES.get(next_stage)
                    if not reply_text:
                        deal["stage"] = next_stage
                        deal["last_msg_count"] = current_count
                        save_deals(deals)
                        continue

                    # Пауза перед ответом
                    delay = random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
                    print(f"   ⏱ Отвечаю через {delay:.0f}с...")
                    time.sleep(delay)

                    if send_text(page, reply_text):
                        deal["stage"] = next_stage
                        print(f"   Стадия: {stage} → {next_stage}")

                    deal["last_msg_count"] = get_message_count(page)
                    deal["updated"] = datetime.datetime.now().isoformat()
                    save_deals(deals)
                    human_delay(2, 4)

                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[err] {e}")
                time.sleep(10)

        print("\n🛑 Остановлено.")
        browser.close()

if __name__ == "__main__":
    run()
