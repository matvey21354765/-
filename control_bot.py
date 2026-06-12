"""
control_bot.py — Telegram бот-пульт для авто-брокера
Управляй диалогами с продавцами прямо из Telegram.

Установка:
    pip install aiogram playwright playwright-stealth
    playwright install chromium

Настройка:
    1. Создай бота через @BotFather в Telegram → получи токен
    2. Узнай свой Telegram ID: напиши @userinfobot
    3. Заполни BOT_TOKEN и MY_CHAT_ID ниже

Запуск:
    python control_bot.py
"""

import asyncio
import json
import logging
import random
import re
import time
import threading
import datetime
import subprocess
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command

# ============================================================
#  НАСТРОЙКИ
# ============================================================

import os
BOT_TOKEN  = os.getenv("BOT_TOKEN",  "8657191103:AAFBXaObKV2jcLbBsBzpYTuBfBj2bBkymrk")
MY_CHAT_ID = int(os.getenv("MY_CHAT_ID", "749256529"))
TWOCAPTCHA_KEY = os.getenv("TWOCAPTCHA_KEY", "d83693d29ec0a9b78bd85d0e7f869dfe")

LISTINGS_FILE = "listings.json"
DEALS_FILE    = "control_deals.json"
SESSION_DIR   = Path("browser_profile")

PRICE_MIN = 500_000
PRICE_MAX = 1_000_000

DEALER_KEYWORDS = [
    # Юр. лица
    "ооо", "ип ", "ао ", "зао ", "пао ",
    # Общие дилерские слова
    "автосалон", "официальный дилер", "дилер", "автоцентр",
    "trade-in", "трейд-ин", "автохолдинг", "автодом", "автомир",
    # Конкретные сети
    "рольф", "major", "lada", "колёса даром", "автопланета",
    "автоград", "июль", "автоленд", "favorit", "фаворит",
    "авто плюс", "автоплюс", "fresh auto", "fresh авто",
    "автобан", "автосфера", "арконт", "ключавто", "авилон",
    "петровский", "прагматика", "бизнес кар", "b-cars",
    "автоимпорт", "автоальянс", "автопассаж", "авторай",
    "максимум", "мотус", "genser", "генсер",
    # Признаки салона
    "кредит от", "гарантия", "автоподбор", "выкуп авто",
    "автовыкуп", "срочный выкуп",
]

# ============================================================
#  СЦЕНАРИЙ
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
    "offer":     "Могу дополнительно продвигать её через свои соцсети и искать покупателей. Если клиент приходит через меня — беру комиссию после сделки. Можем попробовать поработать.",
    "price_ask": "Скажите свою последнюю цену",
    "deal":      "Можем тогда если найду покупателя выше вашей крайней цены, все что сверху себе возьму. Без наглости 🙂",
    "details":   "по машине есть какие то повреждение по кузову и есть ли запрет ?\nПроверьте машину в Автотеке. Отчёт может показать:\n→ ДТП и повреждения\n→ скрутки пробега\n→ работу в такси\n→ ограничения на регистрацию\n→ залог и многое другое\n\nПроверить от 115 ₽",
    "photos":    "Можете тогда какие нибудь другие, но хорошие фотографии с машиной ещё скинуть",
    "active":    "Хорошо, если что напишу вам 👍",
}

STAGE_NAMES = {
    "opener":    "Первое сообщение",
    "offer":     "Предложение",
    "price_ask": "Спрос цены",
    "deal":      "Схема работы",
    "details":   "Состояние авто",
    "photos":    "Фотографии",
    "active":    "Активный",
    "closed":    "Закрыт",
}

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

def load_listings() -> list:
    if not Path(LISTINGS_FILE).exists():
        return []
    return json.loads(Path(LISTINGS_FILE).read_text(encoding="utf-8"))

# Короткие ID для кнопок (Telegram лимит: 64 байта на callback_data)
_id_to_url: dict[str, str] = {}
_url_to_id: dict[str, str] = {}
_id_counter = 0

def url_to_id(url: str) -> str:
    global _id_counter
    if url not in _url_to_id:
        _id_counter += 1
        sid = str(_id_counter)
        _url_to_id[url] = sid
        _id_to_url[sid] = url
    return _url_to_id[url]

def id_to_url(sid: str) -> str:
    return _id_to_url.get(sid, sid)

# ============================================================
#  ФИЛЬТРЫ
# ============================================================

def parse_price(s: str) -> int | None:
    digits = re.sub(r"[^\d]", "", str(s or ""))
    return int(digits) if digits else None

def is_dealer(item: dict) -> bool:
    text = (item.get("title","") + " " + item.get("description","")).lower()
    return any(k in text for k in DEALER_KEYWORDS)

def in_price_range(item: dict) -> bool:
    p = parse_price(item.get("price",""))
    if p is None:
        return True
    return PRICE_MIN <= p <= PRICE_MAX

# ============================================================
#  PLAYWRIGHT — отправка сообщений
# ============================================================

_browser_context = None
_playwright_obj  = None

def get_browser():
    global _browser_context, _playwright_obj
    if _browser_context:
        return _browser_context
    from playwright.sync_api import sync_playwright
    SESSION_DIR.mkdir(exist_ok=True)
    _playwright_obj = sync_playwright().start()
    IS_SERVER = os.getenv("RAILWAY_ENVIRONMENT") is not None
    use_proxy = _xray_proc and _xray_proc.poll() is None
    kwargs = dict(
        user_data_dir=str(SESSION_DIR),
        headless=IS_SERVER,
        args=["--no-sandbox","--disable-blink-features=AutomationControlled"],
        viewport={"width":1280,"height":900},
        locale="ru-RU",
        timezone_id="Asia/Yekaterinburg",
    )
    if use_proxy:
        kwargs["proxy"] = {"server": "socks5://127.0.0.1:10808"}
    _browser_context = _playwright_obj.chromium.launch_persistent_context(**kwargs)
    return _browser_context

def type_text(box, text: str):
    for line in text.split("\n"):
        for ch in line:
            box.type(ch, delay=random.randint(40,110))
        if line != text.split("\n")[-1]:
            box.press("Shift+Enter")
            time.sleep(random.uniform(0.2,0.5))

def send_on_avito(url: str, message: str) -> tuple[bool, str]:
    """Открывает объявление Авито и отправляет сообщение. Возвращает (успех, url_чата)."""
    try:
        ctx = get_browser()
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(2,4))

            # Ищем кнопку "Написать"
            write_btn = None
            for sel in [
                "[data-marker='item-view/write-sms']",
                "[data-marker='seller-info/write']",
                "button[class*='write']",
            ]:
                el = page.query_selector(sel)
                if el:
                    write_btn = el
                    break
            if not write_btn:
                for btn in page.query_selector_all("button, a"):
                    try:
                        if "написать" in btn.inner_text().lower():
                            write_btn = btn
                            break
                    except Exception:
                        pass
            if not write_btn:
                return False, ""

            write_btn.click()
            time.sleep(random.uniform(1.5,3))

            # Поле ввода
            input_box = None
            for sel in [
                "textarea[data-marker='messenger/input']",
                "textarea[placeholder*='ообщени']",
                "textarea",
            ]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=4000):
                        input_box = el
                        break
                except Exception:
                    pass
            if not input_box:
                return False, ""

            input_box.click()
            time.sleep(0.5)
            type_text(input_box, message)
            time.sleep(random.uniform(0.5,1))

            send_btn = page.query_selector("button[data-marker='messenger/send-button']")
            if send_btn:
                send_btn.click()
            else:
                input_box.press("Enter")

            time.sleep(2)
            chat_url = page.url
            # Проверяем авторизацию
            if "login" in chat_url or "auth" in chat_url:
                return False, "нужно войти в аккаунт Авито в браузере бота"
            return True, chat_url
        finally:
            page.close()
    except Exception as e:
        return False, str(e)


def send_on_drom(url: str, message: str) -> tuple[bool, str]:
    """Открывает объявление Дром и отправляет сообщение через форму."""
    try:
        ctx = get_browser()
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(3, 5))

            # Дром: ищем кнопку написать
            write_btn = None
            for sel in [
                "[data-ga-stats-name='send_message']",
                "[data-ftid='component_bulletin-contacts_send-message']",
                "button[class*='SendMessage']",
                "a[class*='SendMessage']",
            ]:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    write_btn = el
                    break

            if not write_btn:
                for btn in page.query_selector_all("button, a"):
                    try:
                        t = btn.inner_text().strip().lower()
                        if any(w in t for w in ["написать", "сообщение продавцу", "связаться", "написать продавцу"]):
                            write_btn = btn
                            break
                    except Exception:
                        pass

            if not write_btn:
                # Пробуем показать номер телефона вместо сообщения
                phone_btn = page.query_selector("[data-ftid='component_bulletin-contacts_show-phone']")
                if phone_btn:
                    phone_btn.click()
                    time.sleep(2)
                    phone = page.query_selector("[data-ftid='component_bulletin-contacts_phone']")
                    if phone:
                        return False, f"тел: {phone.inner_text(strip=True)} (написать нельзя — только звонок)"
                return False, "кнопка написать не найдена"

            write_btn.click()
            time.sleep(random.uniform(2, 4))

            # Поле ввода после нажатия кнопки
            input_box = None
            for sel in [
                "textarea[name='message']",
                "textarea[placeholder*='ообщени']",
                "div[class*='Modal'] textarea",
                "div[class*='Popup'] textarea",
                "textarea",
                "div[contenteditable='true']",
            ]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=5000):
                        input_box = el
                        break
                except Exception:
                    pass

            if not input_box:
                return False, "поле ввода не найдено (возможно, нужно войти в аккаунт Дром)"

            input_box.click()
            time.sleep(0.5)
            type_text(input_box, message)
            time.sleep(random.uniform(0.5, 1))

            send_btn = None
            for sel in [
                "button[type='submit']",
                "button[class*='submit']",
                "button[class*='Send']",
                "button[class*='send']",
                "input[type='submit']",
            ]:
                try:
                    el = page.locator(sel).last
                    if el.is_visible(timeout=2000):
                        send_btn = el
                        break
                except Exception:
                    pass

            if send_btn:
                send_btn.click()
            else:
                input_box.press("Enter")

            time.sleep(2)
            # Проверяем — если появилось "войдите" значит не авторизованы
            content = page.content().lower()
            if "войдите" in content or "авторизуйтесь" in content or "войти" in content:
                return False, "нужно войти в аккаунт Дром в браузере бота"
            return True, page.url
        finally:
            page.close()
    except Exception as e:
        return False, str(e)


def send_on_autoru(url: str, message: str) -> tuple[bool, str]:
    """Открывает объявление Авто.ру и отправляет сообщение."""
    try:
        ctx = get_browser()
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(2,4))

            write_btn = None
            for btn in page.query_selector_all("button, a"):
                try:
                    t = btn.inner_text().lower()
                    if "написать" in t or "сообщение" in t:
                        write_btn = btn
                        break
                except Exception:
                    pass
            if not write_btn:
                return False, "кнопка не найдена"

            write_btn.click()
            time.sleep(random.uniform(1.5,3))

            input_box = None
            for sel in ["textarea","div[contenteditable='true']"]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=4000):
                        input_box = el
                        break
                except Exception:
                    pass
            if not input_box:
                return False, "поле ввода не найдено"

            input_box.click()
            time.sleep(0.5)
            type_text(input_box, message)
            time.sleep(random.uniform(0.5,1))

            for sel in ["button[type='submit']","button[class*='send']"]:
                el = page.query_selector(sel)
                if el:
                    el.click()
                    break
            else:
                input_box.press("Enter")

            time.sleep(2)
            return True, page.url
        finally:
            page.close()
    except Exception as e:
        return False, str(e)


def _load_avito_cookies_for_requests() -> dict:
    """Загружает куки Авито из файла для использования в requests."""
    cookies = {}
    f = Path("avito_cookies_raw.json")
    if f.exists():
        try:
            for c in json.loads(f.read_text(encoding="utf-8")):
                if c.get("name") and c.get("value"):
                    cookies[c["name"]] = c["value"]
        except Exception:
            pass
    return cookies


def send_on_avito_http(item_url: str, message: str) -> tuple[bool, str]:
    """
    Отправляет сообщение продавцу через Авито HTTP API (без браузера).
    Использует сохранённые куки из avito_cookies_raw.json.
    """
    try:
        import requests as _req
        import re as _re

        cookies = _load_avito_cookies_for_requests()
        if not cookies:
            return False, "нет куки Авито — загрузи через /login"

        proxies = {}
        if _xray_proc and _xray_proc.poll() is None:
            proxies = {"https": "socks5h://127.0.0.1:10808", "http": "socks5h://127.0.0.1:10808"}

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": item_url,
            "Origin": "https://www.avito.ru",
            "X-Requested-With": "XMLHttpRequest",
        }

        session = _req.Session()
        session.cookies.update(cookies)
        session.headers.update(headers)

        # Получаем ID объявления из URL
        m = _re.search(r'_(\d+)$', item_url.rstrip('/'))
        if not m:
            m = _re.search(r'/(\d+)(?:\?|$)', item_url)
        if not m:
            return False, f"не могу извлечь ID из URL: {item_url}"
        item_id = m.group(1)

        # Создаём чат / получаем существующий
        chat_resp = session.post(
            "https://www.avito.ru/web/1/messenger/getChat",
            json={"itemId": int(item_id)},
            proxies=proxies,
            timeout=20,
        )
        if chat_resp.status_code != 200:
            # Пробуем альтернативный endpoint
            chat_resp = session.post(
                "https://www.avito.ru/api/1/messenger/createChat",
                json={"itemId": item_id},
                proxies=proxies,
                timeout=20,
            )

        try:
            chat_data = chat_resp.json()
        except Exception:
            chat_data = {}

        chat_id = (
            chat_data.get("result", {}).get("id")
            or chat_data.get("chat", {}).get("id")
            or chat_data.get("id")
        )

        if not chat_id:
            # Последний шанс: открыть страницу объявления и найти chatId
            page_resp = session.get(item_url, proxies=proxies, timeout=20)
            m2 = _re.search(r'"chatId"\s*:\s*"([^"]+)"', page_resp.text)
            if m2:
                chat_id = m2.group(1)
            else:
                m2 = _re.search(r'chat_id["\s:=]+([a-z0-9_\-]+)', page_resp.text, _re.IGNORECASE)
                if m2:
                    chat_id = m2.group(1)

        if not chat_id:
            return False, f"не удалось получить chat_id (статус {chat_resp.status_code})"

        # Отправляем сообщение
        send_resp = session.post(
            f"https://www.avito.ru/web/1/messenger/sendMessage",
            json={"chatId": chat_id, "message": {"text": message}},
            proxies=proxies,
            timeout=20,
        )

        if send_resp.status_code == 200:
            return True, f"https://www.avito.ru/profile/messenger/{chat_id}"
        elif send_resp.status_code == 401:
            return False, "сессия истекла — загрузи свежие куки через /login"
        else:
            return False, f"ошибка API {send_resp.status_code}: {send_resp.text[:200]}"

    except Exception as e:
        return False, str(e)


def send_message_to_seller(item: dict, message: str) -> tuple[bool, str]:
    source = item.get("source","")
    url    = item.get("url","")
    if source == "avito":
        # Сначала пробуем HTTP API (быстрее, без браузера)
        ok, detail = send_on_avito_http(url, message)
        if ok:
            return ok, detail
        # Если HTTP не сработал — используем Playwright
        print(f"  [Авито HTTP API] {detail} — fallback to Playwright")
        return send_on_avito(url, message)
    elif source == "drom":
        return send_on_drom(url, message)
    elif source == "autoru":
        return send_on_autoru(url, message)
    return False, "неизвестный источник"


# ============================================================
#  БОТ
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# Временное хранилище: ожидаем ввод текста от пользователя
# chat_id -> {"action": "custom_text"/"captcha", "deal_key": ..., "event": asyncio.Event}
waiting_input: dict = {}

# Хранит asyncio.Event для ожидания ответа на капчу
# user_id -> {"answer": str, "event": asyncio.Event}
captcha_wait: dict = {}


def make_listing_keyboard(deal_key: str, stage: str) -> InlineKeyboardMarkup:
    sid = url_to_id(deal_key)
    next_stage = STAGES.get(stage)
    buttons = []
    if next_stage and next_stage in REPLIES:
        buttons.append([
            InlineKeyboardButton(
                text=f"✉️ Отправить: «{REPLIES[next_stage][:30]}...»",
                callback_data=f"send_auto|{sid}"
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="✏️ Написать своё", callback_data=f"send_custom|{sid}"),
        InlineKeyboardButton(text="❌ Пропустить",    callback_data=f"skip|{sid}"),
    ])
    buttons.append([
        InlineKeyboardButton(text="🔗 Открыть объявление",
                             url=deal_key if deal_key.startswith("http") else "https://avito.ru"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def listing_card_text(item: dict, deal: dict) -> str:
    stage_name = STAGE_NAMES.get(deal.get("stage",""), deal.get("stage",""))
    price = item.get("price","—")
    source_icons = {"avito":"🟢 Авито", "drom":"🔵 Дром", "autoru":"🔴 Авто.ру"}
    source = source_icons.get(item.get("source",""), item.get("source",""))
    title  = item.get("title","")
    days   = item.get("_days_on_site", "?")
    score  = item.get("_hot_score", "")

    text = (
        f"{source}  |  {stage_name}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🚗 {title}\n"
        f"💰 {price}\n"
        f"📅 Дней на сайте: {days}\n"
    )
    if score:
        text += f"🔥 Рейтинг: {score}\n"
    if deal.get("stage") not in ("new","opener"):
        next_stage = STAGES.get(deal.get("stage",""))
        if next_stage and next_stage in REPLIES:
            text += f"\n📝 Следующая фраза:\n_{REPLIES[next_stage][:120]}_"
    return text


async def notify_new_listing(item: dict):
    """Отправляет уведомление о новом подходящем объявлении."""
    deals = load_deals()
    url = item.get("url","")
    if url in deals:
        return

    deals[url] = {
        "stage": "new",
        "title": item.get("title",""),
        "source": item.get("source",""),
        "listing_url": url,
        "chat_url": None,
        "sent": None,
        "updated": datetime.datetime.now().isoformat(),
    }
    save_deals(deals)

    text = listing_card_text(item, deals[url])
    sid = url_to_id(url)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Написать первое сообщение",
                              callback_data=f"send_opener|{sid}")],
        [InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip|{sid}")],
        [InlineKeyboardButton(text="🔗 Объявление", url=url)],
    ])
    await bot.send_message(MY_CHAT_ID, text, reply_markup=kb, parse_mode="Markdown")


# ── Команды ──────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 Авито",   callback_data="source|avito"),
            InlineKeyboardButton(text="🔵 Дром",    callback_data="source|drom"),
            InlineKeyboardButton(text="🔴 Авто.ру", callback_data="source|autoru"),
        ],
        [InlineKeyboardButton(text="📋 Все источники", callback_data="source|all")],
    ])
    await msg.answer(
        "👋 Авто-брокер бот запущен!\n\n"
        "Выбери источник объявлений:",
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith("source|"))
async def cb_source(cb: CallbackQuery):
    source = cb.data.split("|", 1)[1]
    source_names = {"avito": "🟢 Авито", "drom": "🔵 Дром", "autoru": "🔴 Авто.ру", "all": "📋 Все"}
    await cb.answer(f"Выбрано: {source_names.get(source, source)}")

    listings = load_listings()
    deals = load_deals()
    new_items = [
        i for i in listings
        if not is_dealer(i) and in_price_range(i)
        and i.get("url") and i.get("url") not in deals
        and (source == "all" or i.get("source") == source)
    ][:10]

    if not new_items:
        await cb.message.answer(f"Нет новых объявлений для {source_names.get(source, source)}.\nНажми /scan чтобы обновить.")
        return

    await cb.message.answer(f"Нашёл {len(new_items)} объявлений ({source_names.get(source, source)}):")
    for item in new_items:
        url = item.get("url","")
        sid = url_to_id(url)
        src = item.get("source","")
        icon = "🟢" if src=="avito" else "🔵" if src=="drom" else "🔴"
        text = (
            f"{icon} {item.get('title','')}\n"
            f"💰 {item.get('price','—')}\n"
            f"📅 Дней: {item.get('_days_on_site','?')}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✉️ Написать", callback_data=f"send_opener|{sid}")],
            [InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip|{sid}")],
            [InlineKeyboardButton(text="🔗 Открыть", url=url)],
        ])
        await cb.message.answer(text, reply_markup=kb)

@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    deals = load_deals()
    from collections import Counter
    stages = Counter(d.get("stage","?") for d in deals.values())
    lines = ["📊 Статистика диалогов:\n"]
    for stage, cnt in sorted(stages.items()):
        lines.append(f"  {STAGE_NAMES.get(stage, stage)}: {cnt}")
    lines.append(f"\nВсего: {len(deals)}")
    await msg.answer("\n".join(lines))

@dp.message(Command("new"))
async def cmd_new(msg: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 Авито",   callback_data="source|avito"),
            InlineKeyboardButton(text="🔵 Дром",    callback_data="source|drom"),
            InlineKeyboardButton(text="🔴 Авто.ру", callback_data="source|autoru"),
        ],
        [InlineKeyboardButton(text="📋 Все источники", callback_data="source|all")],
    ])
    await msg.answer("Выбери источник:", reply_markup=kb)
    listings = load_listings()
    deals = load_deals()
    new_items = [
        i for i in listings
        if not is_dealer(i) and in_price_range(i)
        and i.get("url") and i.get("url") not in deals
    ][:10]

    if not new_items:
        await msg.answer("Новых подходящих объявлений нет.\nЗапусти /scan чтобы обновить.")
        return

    await msg.answer(f"Нашёл {len(new_items)} новых объявлений:")
    for item in new_items:
        url = item.get("url","")
        sid = url_to_id(url)
        text = (
            f"{'🟢 Авито' if item.get('source')=='avito' else '🔵 Дром' if item.get('source')=='drom' else '🔴 Авто.ру'}\n"
            f"🚗 {item.get('title','')}\n"
            f"💰 {item.get('price','—')}\n"
            f"📅 Дней: {item.get('_days_on_site','?')}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✉️ Написать", callback_data=f"send_opener|{sid}")],
            [InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip|{sid}")],
            [InlineKeyboardButton(text="🔗 Открыть", url=url)],
        ])
        await msg.answer(text, reply_markup=kb)

@dp.message(Command("active"))
async def cmd_active(msg: Message):
    deals = load_deals()
    active = {k:v for k,v in deals.items()
              if v.get("stage") not in ("closed","done","error","new",None)}
    if not active:
        await msg.answer("Нет активных диалогов.")
        return
    await msg.answer(f"Активных диалогов: {len(active)}")
    for url, deal in list(active.items())[:10]:
        sid = url_to_id(url)
        stage = deal.get("stage","")
        next_stage = STAGES.get(stage)
        text = (
            f"{'🟢 Авито' if deal.get('source')=='avito' else '🔵 Дром' if deal.get('source')=='drom' else '🔴'}\n"
            f"🚗 {deal.get('title','')[:50]}\n"
            f"📍 Стадия: {STAGE_NAMES.get(stage, stage)}"
        )
        buttons = []
        if next_stage and next_stage in REPLIES:
            buttons.append([InlineKeyboardButton(
                text=f"✉️ «{REPLIES[next_stage][:35]}...»",
                callback_data=f"send_auto|{sid}"
            )])
        buttons.append([
            InlineKeyboardButton(text="✏️ Своё", callback_data=f"send_custom|{sid}"),
            InlineKeyboardButton(text="❌ Закрыть", callback_data=f"close|{sid}"),
        ])
        if url.startswith("http"):
            buttons.append([InlineKeyboardButton(text="🔗 Открыть", url=url)])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await msg.answer(text, reply_markup=kb)

@dp.message(Command("xray"))
async def cmd_xray(msg: Message):
    """Проверяет статус xray прокси."""
    lines = []

    # Статус процесса
    if _xray_proc is None:
        lines.append("❌ xray не запускался (бинарь не найден?)")
    elif _xray_proc.poll() is not None:
        lines.append(f"❌ xray упал (код {_xray_proc.poll()})")
    else:
        lines.append("✅ xray процесс работает (PID " + str(_xray_proc.pid) + ")")

    # Проверяем порт
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", 10808), timeout=2)
        s.close()
        lines.append("✅ SOCKS5 порт 10808 открыт")
    except Exception as e:
        lines.append(f"❌ Порт 10808 недоступен: {e}")

    await msg.answer("\n".join(lines) + "\n\n⏳ Тестирую IP через прокси...")

    # Тест SOCKS5 через низкоуровневый сокет
    try:
        import socket as _sock, struct as _struct

        def _socks5_test(target_host: str, target_port: int) -> tuple[bool, str]:
            s = _sock.socket()
            s.settimeout(10)
            s.connect(("127.0.0.1", 10808))
            s.send(b'\x05\x01\x00')
            r = s.recv(2)
            if r != b'\x05\x00':
                return False, f"SOCKS5 auth error: {r.hex()}"
            host_b = target_host.encode()
            s.send(b'\x05\x01\x00\x03' + bytes([len(host_b)]) + host_b + _struct.pack('>H', target_port))
            r = s.recv(10)
            s.close()
            if len(r) < 2 or r[1] != 0:
                code = r[1] if len(r) > 1 else -1
                errs = {1:"General failure",2:"Not allowed",3:"Network unreachable",4:"Host unreachable",5:"Connection refused"}
                return False, f"SOCKS5 error {code}: {errs.get(code,'unknown')}"
            return True, "OK"

        loop = asyncio.get_event_loop()
        ok, detail = await loop.run_in_executor(None, lambda: _socks5_test("api.ipify.org", 443))
        lines.append(f"{'✅' if ok else '❌'} SOCKS5→api.ipify.org:443: {detail}")
    except Exception as e:
        lines.append(f"❌ SOCKS5 тест: {e}")

    # IP через прокси (requests + socks)
    try:
        import requests as _req
        loop2 = asyncio.get_event_loop()
        def _get_proxy_ip():
            s = _req.Session()
            s.proxies = {"https": "socks5h://127.0.0.1:10808", "http": "socks5h://127.0.0.1:10808"}
            r = s.get("https://api.ipify.org", timeout=15)
            return r.text.strip()
        proxy_ip = await loop2.run_in_executor(None, _get_proxy_ip)
        lines.append(f"📍 Внешний IP через прокси: {proxy_ip}")
    except Exception as e:
        lines.append(f"❌ IP через прокси: {e}")

    # IP сервера без прокси
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.ipify.org", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                real_ip = await resp.text()
                lines.append(f"🖥 IP сервера (без прокси): {real_ip.strip()}")
    except Exception as e:
        lines.append(f"❌ Ошибка получения IP: {e}")

    # Показываем лог xray
    try:
        log_text = Path("/tmp/xray_error.log").read_text(encoding="utf-8", errors="ignore")
        last_lines = "\n".join(log_text.strip().splitlines()[-15:])
        if last_lines:
            await msg.answer(f"📋 Лог xray:\n```\n{last_lines[:3000]}\n```", parse_mode="Markdown")
    except Exception:
        pass

    await msg.answer("\n".join(lines))


@dp.message(Command("login"))
async def cmd_login(msg: Message):
    """Отправляет cookies в браузер бота для авторизации."""
    await msg.answer(
        "🔐 *Как войти в аккаунты для отправки сообщений:*\n\n"
        "1️⃣ Войди в Авито/Дром в браузере на своём компьютере\n"
        "2️⃣ Установи расширение *EditThisCookie* (Chrome/Firefox)\n"
        "3️⃣ Экспортируй куки в JSON\n"
        "4️⃣ Отправь JSON-файл боту\n\n"
        "Или: отправь файл `avito_cookies.json` / `drom_cookies.json`\n\n"
        "⚠️ Бот работает на сервере — без авторизации писать продавцам не может.",
        parse_mode="Markdown"
    )


@dp.message(Command("scan"))
async def cmd_scan(msg: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 Авито",   callback_data="scan|avito"),
            InlineKeyboardButton(text="🔵 Дром",    callback_data="scan|drom"),
        ],
        [InlineKeyboardButton(text="📋 Авито + Дром", callback_data="scan|all")],
    ])
    await msg.answer("Что сканировать?", reply_markup=kb)


def _scrape_avito_http_with_cookies(pages: int = 5) -> list[dict]:
    """Скрапинг Авито через HTTP с куками и прокси — без браузера."""
    try:
        import requests as _req
        from bs4 import BeautifulSoup as _BS
        import re as _re, datetime as _dt, random as _rnd
    except ImportError:
        return []

    # Загружаем куки из browser_profile если есть
    cookies_file = Path("avito_cookies_raw.json")
    cookies = {}
    if cookies_file.exists():
        try:
            raw = json.loads(cookies_file.read_text(encoding="utf-8"))
            for c in raw:
                name = c.get("name","")
                value = c.get("value","")
                if name and value:
                    cookies[name] = value
        except Exception:
            pass

    proxies = {"https": "socks5h://127.0.0.1:10808", "http": "socks5h://127.0.0.1:10808"} if (_xray_proc and _xray_proc.poll() is None) else {}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.avito.ru/",
    }

    MONTHS = {"янв":1,"фев":2,"мар":3,"апр":4,"май":5,"мая":5,"июн":6,"июл":7,"авг":8,"сен":9,"окт":10,"ноя":11,"дек":12}
    HOT = _re.compile(r"(срочно|торг|уступлю|снижу|скидка|дёшево|дешево)", _re.IGNORECASE)

    def _parse_date(text):
        if not text: return None
        text = text.strip(); today = _dt.date.today(); low = text.lower()
        if "сегодня" in low: return today
        if "вчера" in low: return today - _dt.timedelta(days=1)
        m = _re.search(r"(\d+)\s+дн", low)
        if m: return today - _dt.timedelta(days=int(m.group(1)))
        if _re.search(r"\d+\s+(час|мин)", low): return today
        m = _re.search(r"(\d{1,2})\s+([а-яё]+)", text, _re.IGNORECASE)
        if m:
            mon = MONTHS.get(m.group(2)[:3].lower())
            if mon:
                try: return _dt.date(today.year, mon, int(m.group(1)))
                except ValueError: pass
        return None

    results = []
    session = _req.Session()
    session.headers.update(headers)
    session.cookies.update(cookies)

    for p in range(1, pages + 1):
        url = f"https://www.avito.ru/ekaterinburg/avtomobili?p={p}&s=104"
        try:
            r = session.get(url, proxies=proxies, timeout=20)
            html = r.text
            if "captcha" in html.lower() or "Доступ ограничен" in html:
                print(f"  [!] Авито HTTP стр.{p}: блокировка")
                break
            soup = _BS(html, "lxml")
            cards = soup.select("[data-marker='item']")
            for card in cards:
                try:
                    title_el = card.select_one("[itemprop='name']") or card.select_one("h3")
                    title = title_el.get_text(strip=True) if title_el else ""
                    link_el = card.select_one("a[href*='/ekaterinburg/']")
                    href = link_el.get("href","") if link_el else ""
                    item_url = ("https://www.avito.ru" + href) if href else ""
                    price_el = card.select_one("[itemprop='price']") or card.select_one("[class*='price']")
                    price = ""
                    if price_el:
                        price = price_el.get("content") or price_el.get_text(strip=True)
                    date_el = card.select_one("[data-marker='item-date']") or card.select_one("span[class*='date']")
                    date_text = date_el.get_text(strip=True) if date_el else ""
                    date = _parse_date(date_text)
                    days = max(0, (_dt.date.today() - date).days) if date else 0
                    photos = len(card.select("img[src*='avito']"))
                    if title and item_url:
                        results.append({
                            "source": "avito",
                            "title": title, "price": price, "url": item_url,
                            "date": str(date) if date else date_text,
                            "_photos": photos, "_days_on_site": days,
                            "_hot_score": round((5-min(photos,5))*2.0 + days*0.3 + (10 if HOT.search(title) else 0), 2),
                            "description": "",
                        })
                except Exception:
                    pass
            import time as _time; _time.sleep(_rnd.uniform(2, 4))
        except Exception as e:
            print(f"  [!] Авито HTTP стр.{p}: {e}")
            break
    return results


async def _solve_yandex_captcha(page, page_num: int) -> bool:
    """
    Отправляет пользователю ссылку на страницу капчи.
    Пользователь открывает в браузере, решает, отправляет куки боту.
    Бот загружает новые куки и продолжает парсинг.
    """
    page_url = page.url or f"https://www.avito.ru/ekaterinburg/avtomobili?p={page_num}&s=104"

    event = asyncio.Event()
    captcha_wait[MY_CHAT_ID] = {"answer": None, "event": event}

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть Авито", url="https://www.avito.ru/ekaterinburg/avtomobili")],
        [InlineKeyboardButton(text="✅ Готово, продолжай", callback_data="captcha_done")],
    ])

    await bot.send_message(
        MY_CHAT_ID,
        f"🔒 Авито показало капчу (стр. {page_num})\n\n"
        f"Открой Авито по кнопке, реши капчу если есть, затем нажми *Готово*.",
        reply_markup=kb,
        parse_mode="Markdown"
    )

    try:
        await asyncio.wait_for(event.wait(), timeout=300)
    except asyncio.TimeoutError:
        captcha_wait.pop(MY_CHAT_ID, None)
        await bot.send_message(MY_CHAT_ID, "⏱ Таймаут ожидания — продолжаю без решения капчи.")
        return False

    captcha_wait.pop(MY_CHAT_ID, None)
    # Небольшая пауза после обновления куков
    await asyncio.sleep(2)
    # Перезагружаем страницу с новыми куками
    await page.reload(wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)
    html = await page.content()
    if "captcha" not in html.lower() and "Доступ ограничен" not in html:
        await bot.send_message(MY_CHAT_ID, f"✅ Авито стр.{page_num}: продолжаю парсинг!")
        return True
    return False


async def scrape_avito_playwright_async(pages: int = 5) -> list[dict]:
    """Парсит Авито через Playwright; при капче шлёт скриншот и ждёт ответа от пользователя."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return []

    results = []
    IS_SERVER = os.getenv("RAILWAY_ENVIRONMENT") is not None

    async with async_playwright() as pw:
        SESSION_DIR.mkdir(exist_ok=True)
        use_proxy = _xray_proc and _xray_proc.poll() is None
        proxy_cfg = {"server": "socks5://127.0.0.1:10808"} if use_proxy else None
        launch_kwargs = dict(
            user_data_dir=str(SESSION_DIR),
            headless=IS_SERVER,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            locale="ru-RU",
            timezone_id="Asia/Yekaterinburg",
        )
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg
        context = await pw.chromium.launch_persistent_context(**launch_kwargs)
        try:
            import re as _re, datetime as _dt, random as _rnd, json as _json

            MONTHS = {
                "янв":1,"фев":2,"мар":3,"апр":4,"май":5,"мая":5,
                "июн":6,"июл":7,"авг":8,"сен":9,"окт":10,"ноя":11,"дек":12,
            }
            HOT = _re.compile(r"(срочно|торг|уступлю|снижу|скидка|дёшево|дешево)", _re.IGNORECASE)

            def _parse_date(text):
                if not text: return None
                text = text.strip(); today = _dt.date.today(); low = text.lower()
                if "сегодня" in low: return today
                if "вчера" in low: return today - _dt.timedelta(days=1)
                m = _re.search(r"(\d+)\s+дн", low)
                if m: return today - _dt.timedelta(days=int(m.group(1)))
                if _re.search(r"\d+\s+(час|мин)", low): return today
                m = _re.search(r"(\d{1,2})\s+([а-яё]+)", text, _re.IGNORECASE)
                if m:
                    mon = MONTHS.get(m.group(2)[:3].lower())
                    if mon:
                        try: return _dt.date(today.year, mon, int(m.group(1)))
                        except ValueError: pass
                return None

            def _hotness(title, photos, days):
                score = (5 - min(photos, 5)) * 2.0 + days * 0.3
                if HOT.search(title): score += 10.0
                return round(score, 2)

            for p in range(1, pages + 1):
                url = f"https://www.avito.ru/ekaterinburg/avtomobili?p={p}&s=104"
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=40000)
                    await asyncio.sleep(_rnd.uniform(2, 4))

                    # Проверяем капчу
                    html = await page.content()
                    is_captcha = (
                        "captcha" in html.lower()
                        or "Доступ ограничен" in html
                        or await page.query_selector("div[class*='captcha']") is not None
                        or await page.query_selector("iframe[src*='captcha']") is not None
                        or await page.query_selector("input[name*='captcha']") is not None
                    )

                    if is_captcha:
                        solved = await _solve_yandex_captcha(page, p)
                        if not solved:
                            await page.close()
                            break
                        # Обновляем html после решения капчи
                        html = await page.content()
                        if "captcha" in html.lower() or "Доступ ограничен" in html:
                            await page.close()
                            break

                    # Парсим карточки
                    from bs4 import BeautifulSoup as _BS
                    soup = _BS(html, "lxml")
                    cards = soup.select("[data-marker='item']")
                    for card in cards:
                        try:
                            title_el = card.select_one("[itemprop='name']") or card.select_one("h3")
                            title = title_el.get_text(strip=True) if title_el else ""
                            link_el = card.select_one("a[href*='/ekaterinburg/']")
                            href = link_el.get("href","") if link_el else ""
                            item_url = ("https://www.avito.ru" + href) if href else ""
                            price_el = card.select_one("[itemprop='price']") or card.select_one("[class*='price']")
                            price = ""
                            if price_el:
                                price = price_el.get("content") or price_el.get_text(strip=True)
                            date_el = card.select_one("[data-marker='item-date']") or card.select_one("span[class*='date']")
                            date_text = date_el.get_text(strip=True) if date_el else ""
                            date = _parse_date(date_text)
                            days = max(0, (_dt.date.today() - date).days) if date else 0
                            photos = len(card.select("img[src*='avito']"))
                            if title and item_url:
                                results.append({
                                    "source": "avito",
                                    "title": title,
                                    "price": price,
                                    "url": item_url,
                                    "date": str(date) if date else date_text,
                                    "_photos": photos,
                                    "_days_on_site": days,
                                    "_hot_score": _hotness(title, photos, days),
                                    "description": "",
                                })
                        except Exception:
                            pass
                    await asyncio.sleep(_rnd.uniform(2, 5))
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
        finally:
            await context.close()

    return results


@dp.callback_query(F.data.startswith("scan|"))
async def cb_scan(cb: CallbackQuery):
    source = cb.data.split("|", 1)[1]
    icons = {"avito": "🟢 Авито", "drom": "🔵 Дром", "all": "📋 Авито + Дром"}
    await cb.answer()
    await cb.message.edit_text(f"🔄 Сканирую {icons.get(source)}...\nЭто займёт 3-10 минут.")

    async def do_scan():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("scraper_http", "scraper_http.py")
            scraper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(scraper)

            loop = asyncio.get_event_loop()
            items = []

            if source in ("avito", "all"):
                # HTTP с куками (быстрее, без браузера)
                avito_items = await loop.run_in_executor(None, lambda: _scrape_avito_http_with_cookies(pages=5))
                if avito_items:
                    items.extend(avito_items)
                else:
                    await bot.send_message(
                        MY_CHAT_ID,
                        "⚠️ Авито HTTP заблокирован (капча/IP блок).\n"
                        "Загрузи свежие куки через /login или подожди — Дром работает без ограничений."
                    )

            if source in ("drom", "all"):
                # Читаем следующую страницу Дрома
                state_file = Path("scan_state.json")
                state = json.loads(state_file.read_text()) if state_file.exists() else {}
                start = state.get("drom_next_page", 1)
                drom_items = await loop.run_in_executor(None, lambda: scraper.scrape_drom_http(pages=20, start_page=start))
                items.extend(drom_items)
                # Сохраняем следующую страницу
                state["drom_next_page"] = start + 20
                state_file.write_text(json.dumps(state))

            merged, new_count = await loop.run_in_executor(None, lambda: scraper.merge_and_save(items))

            deals = load_deals()
            suitable = [
                i for i in merged
                if not is_dealer(i) and in_price_range(i)
                and i.get("url") and i.get("url") not in deals
            ]
            await bot.send_message(MY_CHAT_ID,
                f"✅ {icons.get(source)} готово!\n"
                f"Найдено: {len(items)} | Новых: {new_count}\n"
                f"Подходящих частников: {len(suitable)}\n\n"
                f"Нажми /new чтобы посмотреть."
            )
        except Exception as e:
            await bot.send_message(MY_CHAT_ID, f"❌ Ошибка сканирования: {e}")

    asyncio.create_task(do_scan())


# ── Кнопки ───────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("send_opener|"))
async def cb_send_opener(cb: CallbackQuery):
    url = id_to_url(cb.data.split("|", 1)[1])
    listings = load_listings()
    item = next((i for i in listings if i.get("url") == url), None)
    if not item:
        await cb.answer("Объявление не найдено")
        return

    await cb.answer("Отправляю...")
    await cb.message.edit_text(cb.message.text + "\n\n⏳ Отправляю сообщение...")

    def do_send():
        return send_message_to_seller(item, OPENER)

    loop = asyncio.get_event_loop()
    success, chat_url = await loop.run_in_executor(None, do_send)

    deals = load_deals()
    deals[url] = deals.get(url, {})
    deals[url].update({
        "stage": "opener" if success else "error",
        "title": item.get("title",""),
        "source": item.get("source",""),
        "listing_url": url,
        "chat_url": chat_url,
        "sent": datetime.datetime.now().isoformat(),
        "updated": datetime.datetime.now().isoformat(),
    })
    save_deals(deals)

    if success:
        await cb.message.edit_text(
            cb.message.text.replace("⏳ Отправляю сообщение...", "") +
            f"\n\n✅ Отправлено! Жду ответа продавца..."
        )
    else:
        await cb.message.edit_text(
            cb.message.text.replace("⏳ Отправляю сообщение...", "") +
            f"\n\n❌ Ошибка: {chat_url}"
        )


@dp.callback_query(F.data.startswith("send_auto|"))
async def cb_send_auto(cb: CallbackQuery):
    url = id_to_url(cb.data.split("|", 1)[1])
    deals = load_deals()
    deal  = deals.get(url)
    if not deal:
        await cb.answer("Сделка не найдена")
        return

    stage      = deal.get("stage","")
    next_stage = STAGES.get(stage)
    if not next_stage or next_stage not in REPLIES:
        await cb.answer("Нечего отправлять на этой стадии")
        return

    reply_text = REPLIES[next_stage]
    listings   = load_listings()
    item       = next((i for i in listings if i.get("url") == url), None)
    if not item:
        item = {"url": url, "source": deal.get("source","avito")}

    await cb.answer("Отправляю...")

    def do_send():
        chat_url = deal.get("chat_url") or url
        if deal.get("source") == "avito" and chat_url != url:
            return send_on_avito(url, reply_text)
        return send_message_to_seller(item, reply_text)

    loop = asyncio.get_event_loop()
    success, new_chat_url = await loop.run_in_executor(None, do_send)

    if success:
        deals[url]["stage"]    = next_stage
        deals[url]["chat_url"] = new_chat_url or deal.get("chat_url")
        deals[url]["updated"]  = datetime.datetime.now().isoformat()
        save_deals(deals)
        await cb.message.edit_text(
            cb.message.text + f"\n\n✅ Отправлено ({STAGE_NAMES.get(next_stage,next_stage)})"
        )
    else:
        await cb.message.edit_text(cb.message.text + f"\n\n❌ Ошибка: {new_chat_url}")


@dp.callback_query(F.data.startswith("send_custom|"))
async def cb_send_custom(cb: CallbackQuery):
    url = id_to_url(cb.data.split("|", 1)[1])
    waiting_input[cb.from_user.id] = {"action": "custom_text", "deal_key": url}
    await cb.answer()
    await cb.message.reply("✏️ Напиши своё сообщение для продавца:")


@dp.callback_query(F.data == "captcha_done")
async def cb_captcha_done(cb: CallbackQuery):
    info = captcha_wait.get(cb.from_user.id) or captcha_wait.get(MY_CHAT_ID)
    if info:
        info["answer"] = "done"
        info["event"].set()
    waiting_input.pop(cb.from_user.id, None)
    await cb.answer("✅ Принято, продолжаю скан!")
    await cb.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data.startswith("skip|"))
async def cb_skip(cb: CallbackQuery):
    url = id_to_url(cb.data.split("|", 1)[1])
    deals = load_deals()
    if url not in deals:
        deals[url] = {}
    deals[url]["stage"]   = "closed"
    deals[url]["updated"] = datetime.datetime.now().isoformat()
    save_deals(deals)
    await cb.answer("Пропущено")
    await cb.message.edit_text(cb.message.text + "\n\n❌ Пропущено")


@dp.callback_query(F.data.startswith("close|"))
async def cb_close(cb: CallbackQuery):
    url = id_to_url(cb.data.split("|", 1)[1])
    deals = load_deals()
    if url in deals:
        deals[url]["stage"]   = "closed"
        deals[url]["updated"] = datetime.datetime.now().isoformat()
        save_deals(deals)
    await cb.answer("Закрыто")
    await cb.message.edit_text(cb.message.text + "\n\n🔒 Закрыт")


# ── Обработка свободного текста ──────────────────────────────

async def _load_cookies_to_browser(cookies: list, domain: str):
    """Загружает cookies в Playwright браузер."""
    from playwright.async_api import async_playwright
    IS_SERVER = os.getenv("RAILWAY_ENVIRONMENT") is not None
    SESSION_DIR.mkdir(exist_ok=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=IS_SERVER,
            args=["--no-sandbox"],
        )
        await ctx.add_cookies(cookies)
        await ctx.close()


@dp.message(F.document, F.chat.id == MY_CHAT_ID)
async def handle_document(msg: Message):
    """Принимает listings.json или cookies файл и сохраняет на сервере."""
    doc = msg.document
    if not doc.file_name or not doc.file_name.endswith(".json"):
        await msg.answer("❌ Отправь .json файл")
        return

    await msg.answer("⏳ Загружаю файл...")
    try:
        file = await bot.get_file(doc.file_id)
        content = await bot.download_file(file.file_path)
        data = json.loads(content.read())

        fname = doc.file_name.lower()

        # Cookies файл — определяем по имени или по содержимому
        is_cookie_file = (
            "cookie" in fname
            or (isinstance(data, list) and data and "domain" in data[0] and "name" in data[0] and "value" in data[0])
        )
        if is_cookie_file:
            text_repr = json.dumps(data)
            await _process_cookie_text(msg, text_repr)
            return

        # Объединяем с существующими
        existing = {}
        if Path(LISTINGS_FILE).exists():
            try:
                for item in json.loads(Path(LISTINGS_FILE).read_text(encoding="utf-8")):
                    if item.get("url"):
                        existing[item["url"]] = item
            except Exception:
                pass

        new_count = 0
        for item in data:
            if item.get("url") and item["url"] not in existing:
                new_count += 1
            if item.get("url"):
                existing[item["url"]] = item

        merged = sorted(existing.values(), key=lambda x: x.get("_hot_score", 0), reverse=True)
        Path(LISTINGS_FILE).write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        deals = load_deals()
        new_items = [
            i for i in merged
            if not is_dealer(i) and in_price_range(i)
            and i.get("url") and i.get("url") not in deals
        ]

        await msg.answer(
            f"✅ Загружено!\n"
            f"Всего в файле: {len(data)}\n"
            f"Новых добавлено: {new_count}\n"
            f"Итого в базе: {len(merged)}\n"
            f"Подходящих частников: {len(new_items)}\n\n"
            f"Нажми /new чтобы посмотреть."
        )
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}")


async def _process_cookie_text(msg: Message, text: str):
    """Принимает JSON-массив куков из текстового сообщения и загружает в браузер."""
    import re as _re

    def clean_domain(d: str) -> str:
        # Убираем markdown: ".[www.avito.ru](https://...)" → ".www.avito.ru"
        d = _re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', d)
        return d

    SAMESITE_MAP = {
        "no_restriction": "None",
        "unspecified": "None",
        "lax": "Lax",
        "strict": "Strict",
        "none": "None",
    }

    try:
        raw = json.loads(text)
        if not isinstance(raw, list):
            await msg.answer("❌ Ожидается массив JSON")
            return

        # Сохраняем оригинальные куки для HTTP запросов
        Path("avito_cookies_raw.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )

        pw_cookies = []
        for c in raw:
            domain = clean_domain(c.get("domain", ""))
            if not domain:
                continue
            cookie = {
                "name": c["name"],
                "value": c["value"],
                "domain": domain,
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
                "sameSite": SAMESITE_MAP.get(c.get("sameSite", "").lower(), "None"),
            }
            exp = c.get("expirationDate")
            if exp:
                cookie["expires"] = int(exp)
            pw_cookies.append(cookie)

        await msg.answer(f"⏳ Загружаю {len(pw_cookies)} куков в браузер Авито...")

        from playwright.async_api import async_playwright
        IS_SERVER = os.getenv("RAILWAY_ENVIRONMENT") is not None
        SESSION_DIR.mkdir(exist_ok=True)

        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(SESSION_DIR),
                headless=IS_SERVER,
                args=["--no-sandbox"],
            )
            try:
                await ctx.add_cookies(pw_cookies)
                # Проверяем — открываем профиль
                page = await ctx.new_page()
                await page.goto("https://www.avito.ru/profile", wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2)
                html = await page.content()
                await page.close()
                logged_in = "Выйти" in html or "profile" in page.url or "logout" in html
            finally:
                await ctx.close()

        if logged_in:
            await msg.answer("✅ Куки Авито загружены! Авторизация подтверждена.\nТеперь бот может писать продавцам на Авито.")
        else:
            await msg.answer("⚠️ Куки загружены, но авторизация не подтверждена (возможно куки устарели).")

    except json.JSONDecodeError as e:
        await msg.answer(f"❌ Ошибка разбора JSON: {e}")
    except Exception as e:
        await msg.answer(f"❌ Ошибка загрузки куков: {e}")


@dp.message(F.chat.id == MY_CHAT_ID)
async def handle_text(msg: Message):
    if msg.from_user.id not in waiting_input:
        return

    # Если пользователь прислал JSON с куками прямо в текст
    if msg.text and msg.text.strip().startswith("[") and '"domain"' in msg.text and '"avito' in msg.text.lower():
        await _process_cookie_text(msg, msg.text)
        return
    if msg.text and msg.text.strip().startswith("[") and '"domain"' in msg.text and '"drom' in msg.text.lower():
        await _process_cookie_text(msg, msg.text)
        return

    if msg.from_user.id not in waiting_input:
        return

    state = waiting_input.pop(msg.from_user.id)

    # Обработка капчи
    if state.get("action") == "captcha":
        info = captcha_wait.get(msg.from_user.id) or captcha_wait.get(MY_CHAT_ID)
        if info:
            info["answer"] = msg.text.strip()
            info["event"].set()
        return

    url      = state["deal_key"]
    text     = msg.text.strip()
    deals    = load_deals()
    deal     = deals.get(url, {})
    listings = load_listings()
    item     = next((i for i in listings if i.get("url") == url),
                    {"url": url, "source": deal.get("source","avito")})

    await msg.answer("⏳ Отправляю...")

    def do_send():
        return send_message_to_seller(item, text)

    loop = asyncio.get_event_loop()
    success, chat_url = await loop.run_in_executor(None, do_send)

    if success:
        deals[url] = deals.get(url, {})
        deals[url]["chat_url"] = chat_url or deal.get("chat_url")
        deals[url]["updated"]  = datetime.datetime.now().isoformat()
        if deals[url].get("stage") == "new":
            deals[url]["stage"] = "opener"
        save_deals(deals)
        await msg.answer("✅ Сообщение отправлено!")
    else:
        await msg.answer(f"❌ Ошибка: {chat_url}")


# ============================================================
#  ФОНОВЫЙ СКАНЕР — уведомляет о новых объявлениях
# ============================================================

async def background_scanner():
    """Каждые 30 минут сканирует Дром и уведомляет о новых объявлениях."""
    await asyncio.sleep(30)
    while True:
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("scraper_http", "scraper_http.py")
            scraper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(scraper)

            loop = asyncio.get_event_loop()

            # Дром — всегда работает
            state_file = Path("scan_state.json")
            state = json.loads(state_file.read_text()) if state_file.exists() else {}
            start = state.get("drom_bg_page", 1)
            drom_items = await loop.run_in_executor(
                None, lambda: scraper.scrape_drom_http(pages=10, start_page=start)
            )
            state["drom_bg_page"] = start + 10
            state_file.write_text(json.dumps(state))

            # Авито — пробуем HTTP
            avito_items = await loop.run_in_executor(None, lambda: _scrape_avito_http_with_cookies(pages=3))

            all_items = drom_items + avito_items
            if all_items:
                merged, new_count = await loop.run_in_executor(
                    None, lambda: scraper.merge_and_save(all_items)
                )
                deals = load_deals()
                suitable = [
                    i for i in merged
                    if not is_dealer(i) and in_price_range(i)
                    and i.get("url") and i.get("url") not in deals
                ]
                if suitable:
                    await bot.send_message(
                        MY_CHAT_ID,
                        f"🔔 Авто-скан: {new_count} новых объявлений!\n"
                        f"Подходящих частников: {len(suitable)}\n"
                        f"Нажми /new чтобы посмотреть."
                    )
        except Exception as e:
            print(f"[background_scanner] {e}")
        await asyncio.sleep(1800)  # каждые 30 минут


# ============================================================
#  ЗАПУСК
# ============================================================

_xray_proc = None

def start_xray():
    global _xray_proc
    xray_bin = "/usr/local/bin/xray"
    config = Path("xray_config.json")
    if not Path(xray_bin).exists() or not config.exists():
        return
    try:
        _xray_proc = subprocess.Popen(
            [xray_bin, "run", "-c", str(config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        print("✅ xray запущен (socks5://127.0.0.1:10808)")
    except Exception as e:
        print(f"⚠️ xray не запустился: {e}")


async def main():
    if not BOT_TOKEN:
        print("❌ Заполни BOT_TOKEN в начале файла control_bot.py")
        print("   Создай бота через @BotFather в Telegram")
        return
    if not MY_CHAT_ID:
        print("❌ Заполни MY_CHAT_ID (свой Telegram ID)")
        print("   Узнай через @userinfobot в Telegram")
        return

    start_xray()
    logging.basicConfig(level=logging.WARNING)
    print("✅ Бот запущен! Открой Telegram и напиши /start своему боту.")
    print("   Ctrl+C — остановить\n")

    asyncio.create_task(background_scanner())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
