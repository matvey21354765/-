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

LISTINGS_FILE = "listings.json"
DEALS_FILE    = "control_deals.json"
SESSION_DIR   = Path("browser_profile")

PRICE_MIN = 400_000
PRICE_MAX = 1_000_000

DEALER_KEYWORDS = [
    "ооо", "ип ", "автосалон", "официальный дилер", "дилер",
    "автоцентр", "trade-in", "трейд-ин", "автохолдинг",
    "автодом", "автомир", "рольф", "major", "lada",
    "колёса даром", "автопланета", "автоград",
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
    # На сервере headless=True, локально можно поставить False
    IS_SERVER = os.getenv("RAILWAY_ENVIRONMENT") is not None
    _browser_context = _playwright_obj.chromium.launch_persistent_context(
        user_data_dir=str(SESSION_DIR),
        headless=IS_SERVER,
        args=["--no-sandbox","--disable-blink-features=AutomationControlled"],
        viewport={"width":1280,"height":900},
        locale="ru-RU",
        timezone_id="Asia/Yekaterinburg",
    )
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
            time.sleep(random.uniform(2, 4))

            # Дром: кнопка "Написать продавцу" или "Отправить сообщение"
            write_btn = None
            for sel in [
                "button[data-ga-stats-name='send_message']",
                "a[data-ga-stats-name='send_message']",
                "button[class*='ContactForm']",
                "a[class*='ContactForm']",
                "button[class*='contact']",
                "a[class*='contact']",
            ]:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    write_btn = el
                    break

            if not write_btn:
                for btn in page.query_selector_all("button, a"):
                    try:
                        t = btn.inner_text().strip().lower()
                        if any(w in t for w in ["написать", "сообщение продавцу", "связаться", "отправить сообщение"]):
                            write_btn = btn
                            break
                    except Exception:
                        pass

            if not write_btn:
                return False, "кнопка не найдена"

            write_btn.click()
            time.sleep(random.uniform(2, 4))

            # Ищем поле ввода — Дром использует textarea или modal
            input_box = None
            for sel in [
                "textarea[name='message']",
                "textarea[placeholder*='сообщени']",
                "textarea[placeholder*='Сообщени']",
                "div[class*='Modal'] textarea",
                "div[class*='modal'] textarea",
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
                return False, "поле ввода не найдено"

            input_box.click()
            time.sleep(0.5)
            type_text(input_box, message)
            time.sleep(random.uniform(0.5, 1))

            send_btn = None
            for sel in [
                "button[type='submit']",
                "button[class*='submit']",
                "button[class*='Send']",
                "input[type='submit']",
            ]:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    send_btn = el
                    break

            if send_btn:
                send_btn.click()
            else:
                input_box.press("Enter")

            time.sleep(2)
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


def send_message_to_seller(item: dict, message: str) -> tuple[bool, str]:
    source = item.get("source","")
    url    = item.get("url","")
    if source == "avito":
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
# chat_id -> {"action": "custom_text", "deal_key": ...}
waiting_input: dict = {}


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
    await msg.answer(
        "👋 Авто-брокер бот запущен!\n\n"
        "Команды:\n"
        "/new — показать новые объявления\n"
        "/active — активные диалоги\n"
        "/stats — статистика\n"
        "/scan — запустить новое сканирование"
    )

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

@dp.message(Command("scan"))
async def cmd_scan(msg: Message):
    await msg.answer("🔄 Запускаю сканирование...\nЭто займёт 5-10 минут.")

    async def do_scan():
        try:
            import subprocess, sys
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "broker.py", "--no-server",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            listings = load_listings()
            deals = load_deals()
            new_items = [
                i for i in listings
                if not is_dealer(i) and in_price_range(i)
                and i.get("url") and i.get("url") not in deals
            ]
            await bot.send_message(MY_CHAT_ID,
                f"✅ Готово! Всего: {len(listings)} | Частников в диапазоне: {len(new_items)}\n"
                f"Нажми /new чтобы посмотреть."
            )
        except Exception as e:
            await bot.send_message(MY_CHAT_ID, f"❌ Ошибка: {e}")

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

@dp.message(F.chat.id == MY_CHAT_ID)
async def handle_text(msg: Message):
    if msg.from_user.id not in waiting_input:
        return

    state    = waiting_input.pop(msg.from_user.id)
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
    """Каждые 5 минут проверяет новые объявления и уведомляет."""
    await asyncio.sleep(10)
    while True:
        try:
            listings = load_listings()
            deals    = load_deals()
            new_items = [
                i for i in listings
                if not is_dealer(i) and in_price_range(i)
                and i.get("url") and i.get("url") not in deals
            ]
            if new_items:
                await bot.send_message(
                    MY_CHAT_ID,
                    f"🔔 Найдено {len(new_items)} новых подходящих объявлений!\nНажми /new чтобы посмотреть."
                )
        except Exception:
            pass
        await asyncio.sleep(300)


# ============================================================
#  ЗАПУСК
# ============================================================

async def main():
    if not BOT_TOKEN:
        print("❌ Заполни BOT_TOKEN в начале файла control_bot.py")
        print("   Создай бота через @BotFather в Telegram")
        return
    if not MY_CHAT_ID:
        print("❌ Заполни MY_CHAT_ID (свой Telegram ID)")
        print("   Узнай через @userinfobot в Telegram")
        return

    logging.basicConfig(level=logging.WARNING)
    print("✅ Бот запущен! Открой Telegram и напиши /start своему боту.")
    print("   Ctrl+C — остановить\n")

    asyncio.create_task(background_scanner())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
