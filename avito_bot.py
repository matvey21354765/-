"""
avito_bot.py — Авто-диалог с продавцами через Авито
Работает от твоего аккаунта Авито через браузер.

Установка:
    pip install playwright playwright-stealth
    playwright install chromium

Запуск:
    python avito_bot.py

Первый раз:
    - Откроется браузер
    - Войди в свой аккаунт Авито вручную
    - Сессия сохранится — повторно входить не нужно
"""

import json
import time
import random
import datetime
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

# ============================================================
#  НАСТРОЙКИ
# ============================================================

LISTINGS_FILE     = "listings.json"
DEALS_FILE        = "avito_deals.json"
SESSION_DIR       = Path("browser_profile")   # та же сессия что и у broker.py

MAX_NEW_PER_RUN   = 20       # новых диалогов за один запуск
DELAY_NEW_MIN     = 40       # пауза между новыми сообщениями (сек)
DELAY_NEW_MAX     = 90
REPLY_DELAY_MIN   = 20       # пауза перед ответом
REPLY_DELAY_MAX   = 60
POLL_INTERVAL     = 60       # как часто проверять новые ответы (сек)

# ============================================================
#  СЦЕНАРИЙ ДИАЛОГА
# ============================================================

OPENER = "Здравствуйте\nещё продаёте ?"

STAGES = {
    "opener":    "offer",
    "offer":     "price_ask",
    "price_ask": "deal",
    "deal":      "details",
    "details":   "photos",
    "photos":    "active",
    "active":    None,
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

REFUSE_WORDS = [
    "нет", "не надо", "не интересно", "не нужно",
    "продали", "продала", "продал", "снял", "сняла",
    "уже продал", "уже нашли", "сами справимся", "не актуально",
]

# ============================================================
#  ФИЛЬТРЫ
# ============================================================

PRICE_MIN = 400_000   # минимальная цена
PRICE_MAX = 1_000_000 # максимальная цена

DEALER_KEYWORDS = [
    "ооо", "ип ", "автосалон", "официальный дилер", "дилер",
    "автоцентр", "trade-in", "трейд-ин", "автохолдинг",
    "автодом", "автомир", "рольф", "major", "lada",
    "колёса даром", "автопланета", "автоград",
]

def parse_price(price_str: str) -> int | None:
    """Извлекает число из строки цены. '1 500 000 ₽' → 1500000"""
    if not price_str:
        return None
    digits = re.sub(r"[^\d]", "", str(price_str))
    return int(digits) if digits else None

def is_dealer(item: dict) -> bool:
    text = (item.get("title", "") + " " + item.get("description", "")).lower()
    return any(kw in text for kw in DEALER_KEYWORDS)

def in_price_range(item: dict) -> bool:
    price = parse_price(item.get("price", ""))
    if price is None:
        return True  # цена не указана — не исключаем
    return PRICE_MIN <= price <= PRICE_MAX

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
#  АВИТО
# ============================================================

def human_delay(a=1.0, b=3.0):
    time.sleep(random.uniform(a, b))

def type_message(input_box, text: str):
    lines = text.split("\n")
    for i, line in enumerate(lines):
        for char in line:
            input_box.type(char, delay=random.randint(40, 130))
        if i < len(lines) - 1:
            input_box.press("Shift+Enter")
            human_delay(0.2, 0.5)

def is_logged_in(page) -> bool:
    """Проверяет что мы авторизованы на Авито."""
    try:
        # Кнопка "Войти" — значит не авторизованы
        login_btn = page.query_selector("[data-marker='header/login-button']")
        if login_btn:
            return False
        # Аватар или имя пользователя — авторизованы
        user_el = (
            page.query_selector("[data-marker='header/user-name']") or
            page.query_selector("[data-marker='header/avatar']") or
            page.query_selector("[class*='user-name']")
        )
        return user_el is not None
    except Exception:
        return False

def wait_for_login(page):
    """Ждёт пока пользователь войдёт в аккаунт."""
    print("\n" + "="*55)
    print("  Войди в свой аккаунт Авито в открытом браузере.")
    print("  После входа нажми Enter в этом терминале.")
    print("="*55)
    input("  [Enter после входа в Авито] ")
    human_delay(2, 3)

def open_listing_and_write(page, url: str, message: str) -> bool:
    """
    Открывает объявление и отправляет сообщение продавцу.
    Возвращает True если сообщение отправлено.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        human_delay(2, 4)

        # Скроллим немного вниз
        page.evaluate("window.scrollBy(0, 300)")
        human_delay(1, 2)

        # Ищем кнопку "Написать"
        write_btn = (
            page.query_selector("[data-marker='item-view/write-sms']") or
            page.query_selector("[data-marker='seller-info/write']") or
            page.query_selector("button[class*='write']") or
            page.query_selector("a[class*='write']")
        )

        if not write_btn:
            # Ищем по тексту кнопки
            btns = page.query_selector_all("button, a")
            for btn in btns:
                try:
                    txt = btn.inner_text().strip().lower()
                    if "написать" in txt:
                        write_btn = btn
                        break
                except Exception:
                    pass

        if not write_btn:
            print("    [!] Кнопка 'Написать' не найдена")
            return False

        write_btn.click()
        human_delay(1.5, 3)

        # Ждём появления поля ввода в чате
        input_box = None
        for selector in [
            "textarea[data-marker='messenger/input']",
            "textarea[placeholder*='сообщение']",
            "textarea[placeholder*='Сообщение']",
            "div[contenteditable='true'][data-marker='messenger/input']",
            "textarea.form-control",
            "textarea",
        ]:
            try:
                el = page.locator(selector).first
                if el.is_visible(timeout=4000):
                    input_box = el
                    break
            except Exception:
                pass

        if not input_box:
            print("    [!] Поле ввода сообщения не найдено")
            return False

        input_box.click()
        human_delay(0.5, 1.2)
        type_message(input_box, message)
        human_delay(0.5, 1.0)

        # Отправляем
        send_btn = (
            page.query_selector("button[data-marker='messenger/send-button']") or
            page.query_selector("button[type='submit'][class*='send']")
        )
        if send_btn:
            send_btn.click()
        else:
            input_box.press("Enter")

        human_delay(1.5, 2.5)
        print(f"    ✓ Отправлено: {message[:60].replace(chr(10),' ')}...")
        return True

    except Exception as e:
        print(f"    [!] Ошибка: {e}")
        return False


def get_avito_chat_url(page, listing_url: str) -> str | None:
    """
    Возвращает URL чата Авито для данного объявления.
    Обычно это https://www.avito.ru/profile/messenger?context=...
    """
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=20000)
        human_delay(1, 2)
        write_btn = page.query_selector("[data-marker='item-view/write-sms']")
        if write_btn:
            href = write_btn.get_attribute("href")
            if href:
                return href
    except Exception:
        pass
    return None


def get_last_seller_message(page) -> str | None:
    """Читает последнее сообщение от продавца в открытом чате."""
    try:
        # Сообщения от собеседника (не наши)
        msgs = page.query_selector_all("[data-marker='messenger/message'][class*='incoming']")
        if not msgs:
            msgs = page.query_selector_all("[class*='message-incoming']")
        if not msgs:
            # Fallback: все сообщения, берём последнее не от нас
            all_msgs = page.query_selector_all("[data-marker='messenger/message']")
            incoming = []
            for m in all_msgs:
                cls = m.get_attribute("class") or ""
                if "outgoing" not in cls and "sent" not in cls:
                    incoming.append(m)
            msgs = incoming

        if msgs:
            text = msgs[-1].inner_text().strip()
            # Убираем системные сообщения и время
            text = re.sub(r'\d{1,2}:\d{2}', '', text).strip()
            return text if len(text) > 1 else None
    except Exception:
        pass
    return None


def get_message_count_in_chat(page) -> int:
    try:
        msgs = page.query_selector_all("[data-marker='messenger/message']")
        return len(msgs)
    except Exception:
        return 0


def open_avito_chat(page, chat_url: str) -> bool:
    """Открывает существующий чат по URL."""
    try:
        if not chat_url.startswith("http"):
            chat_url = "https://www.avito.ru" + chat_url
        page.goto(chat_url, wait_until="domcontentloaded", timeout=20000)
        human_delay(2, 3)
        return True
    except Exception:
        return False


def send_in_open_chat(page, message: str) -> bool:
    """Отправляет сообщение в уже открытом чате Авито."""
    try:
        input_box = None
        for selector in [
            "textarea[data-marker='messenger/input']",
            "textarea[placeholder*='сообщение']",
            "textarea[placeholder*='Сообщение']",
            "textarea",
        ]:
            try:
                el = page.locator(selector).first
                if el.is_visible(timeout=3000):
                    input_box = el
                    break
            except Exception:
                pass

        if not input_box:
            return False

        input_box.click()
        human_delay(0.5, 1.0)
        type_message(input_box, message)
        human_delay(0.5, 1.0)

        send_btn = page.query_selector("button[data-marker='messenger/send-button']")
        if send_btn:
            send_btn.click()
        else:
            input_box.press("Enter")

        human_delay(1, 2)
        print(f"    → {message[:70].replace(chr(10),' ')}...")
        return True
    except Exception as e:
        print(f"    [!] {e}")
        return False


def is_refuse(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in REFUSE_WORDS)

# ============================================================
#  ГЛАВНЫЙ ЦИКЛ
# ============================================================

def run():
    SESSION_DIR.mkdir(exist_ok=True)

    if not Path(LISTINGS_FILE).exists():
        print(f"❌ {LISTINGS_FILE} не найден. Сначала запусти: python broker.py")
        return

    listings = json.loads(Path(LISTINGS_FILE).read_text(encoding="utf-8"))
    deals = load_deals()

    # Только объявления с Авито, частники, в нужном диапазоне цен
    all_avito = [i for i in listings if i.get("source") == "avito" and i.get("url")]

    skipped_dealer = sum(1 for i in all_avito if is_dealer(i))
    skipped_price  = sum(1 for i in all_avito if not is_dealer(i) and not in_price_range(i))

    avito_listings = [
        item for item in all_avito
        if not is_dealer(item)
        and in_price_range(item)
        and item.get("url") not in deals
    ]

    print(f"📋 Авито всего: {len(all_avito)}")
    print(f"   Пропущено дилеров: {skipped_dealer}")
    print(f"   Пропущено не в цене ({PRICE_MIN//1000}к–{PRICE_MAX//1000}к): {skipped_price}")
    print(f"   Подходящих частников: {len(avito_listings)}")
    print(f"   Напишем сейчас: {min(len(avito_listings), MAX_NEW_PER_RUN)}")
    print(f"   Уже в работе: {len([d for d in deals.values() if d.get('stage') not in ('closed','done','error')])}\n")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1280, "height": 900},
            locale="ru-RU",
            timezone_id="Asia/Yekaterinburg",
        )

        if HAS_STEALTH:
            context.on("page", lambda pg: stealth_sync(pg))

        page = context.pages[0] if context.pages else context.new_page()

        # Проверяем авторизацию
        print("🌐 Открываю Авито...")
        page.goto("https://www.avito.ru/", wait_until="domcontentloaded", timeout=30000)
        human_delay(2, 3)

        if not is_logged_in(page):
            wait_for_login(page)
            if not is_logged_in(page):
                print("❌ Не удалось войти в аккаунт.")
                context.close()
                return

        print("✅ Авторизован на Авито!\n")
        human_delay(2, 3)

        # ── Фаза 1: отправляем первые сообщения ─────────────────────────────
        sent_count = 0
        for item in avito_listings[:MAX_NEW_PER_RUN]:
            url = item["url"]
            title = item.get("title", "")[:50]
            print(f"[{sent_count+1}/{min(len(avito_listings), MAX_NEW_PER_RUN)}] {title}")

            success = open_listing_and_write(page, url, OPENER)

            # Сохраняем URL чата если получилось открыть
            chat_url = page.url if success else None

            deals[url] = {
                "stage": "opener" if success else "error",
                "title": title,
                "listing_url": url,
                "chat_url": chat_url,
                "last_msg_count": 1 if success else 0,
                "sent": datetime.datetime.now().isoformat(),
                "updated": datetime.datetime.now().isoformat(),
            }
            save_deals(deals)

            if success:
                sent_count += 1
                if sent_count < min(len(avito_listings), MAX_NEW_PER_RUN):
                    delay = random.uniform(DELAY_NEW_MIN, DELAY_NEW_MAX)
                    print(f"    ⏱ Пауза {delay:.0f}с...\n")
                    time.sleep(delay)

        print(f"\n✅ Отправлено первых сообщений: {sent_count}")
        print(f"🔄 Мониторинг ответов каждые {POLL_INTERVAL}с...")
        print("   Ctrl+C — остановить\n")

        # ── Фаза 2: мониторинг ответов ───────────────────────────────────────
        while True:
            try:
                active = {
                    url: deal for url, deal in deals.items()
                    if deal.get("stage") not in ("closed", "done", "error", None)
                    and deal.get("chat_url")
                }

                for listing_url, deal in active.items():
                    stage = deal.get("stage")
                    next_stage = STAGES.get(stage)
                    if next_stage is None:
                        continue

                    chat_url = deal["chat_url"]
                    if not open_avito_chat(page, chat_url):
                        continue

                    current_count = get_message_count_in_chat(page)
                    last_count = deal.get("last_msg_count", 0)

                    if current_count <= last_count:
                        continue  # новых сообщений нет

                    last_msg = get_last_seller_message(page)
                    if not last_msg:
                        deal["last_msg_count"] = current_count
                        save_deals(deals)
                        continue

                    print(f"\n📨 [{deal.get('title','')[:35]}]")
                    print(f"   Продавец: {last_msg[:100]}")

                    if is_refuse(last_msg):
                        print("   → Отказ. Закрываем.")
                        deal["stage"] = "closed"
                        deal["last_msg_count"] = current_count
                        deal["updated"] = datetime.datetime.now().isoformat()
                        save_deals(deals)
                        continue

                    reply = REPLIES.get(next_stage)
                    if not reply:
                        deal["stage"] = next_stage
                        deal["last_msg_count"] = current_count
                        save_deals(deals)
                        continue

                    delay = random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
                    print(f"   ⏱ Отвечаю через {delay:.0f}с...")
                    time.sleep(delay)

                    if send_in_open_chat(page, reply):
                        print(f"   Стадия: {stage} → {next_stage}")
                        deal["stage"] = next_stage

                    deal["last_msg_count"] = get_message_count_in_chat(page)
                    deal["updated"] = datetime.datetime.now().isoformat()
                    save_deals(deals)
                    human_delay(3, 6)

                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[err] {e}")
                time.sleep(15)

        print("\n🛑 Остановлено.")
        context.close()

if __name__ == "__main__":
    run()
