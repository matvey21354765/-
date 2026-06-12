"""
tg_bot.py — Telegram UserBot для авто-брокера
Работает от ТВОЕГО личного аккаунта (не бот-токен).

Установка:
    pip install telethon

Настройка:
    1. Получи API_ID и API_HASH на https://my.telegram.org/apps
    2. Заполни PHONE, API_ID, API_HASH ниже
    3. При первом запуске введёт код из Telegram

Запуск:
    python tg_bot.py

Лимиты (чтобы не забанили):
    - Не более 30-40 новых чатов в день
    - Скрипт делает паузы между сообщениями автоматически
"""

import asyncio
import json
import re
import random
import datetime
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact

# ============================================================
#  НАСТРОЙКИ — заполни свои значения
# ============================================================

# Получи на https://my.telegram.org/apps
API_ID = 0           # например: 12345678
API_HASH = ""        # например: "abcdef1234567890abcdef1234567890"
PHONE = ""           # твой номер: "+79001234567"

# Сколько новых диалогов открывать за один запуск (не больше 30-40 в день)
MAX_NEW_PER_RUN = 20

# Пауза между отправкой сообщений разным людям (секунды)
DELAY_BETWEEN_CONTACTS = (60, 180)  # случайно от 1 до 3 минут

# Пауза перед ответом в диалоге (имитация чтения)
REPLY_DELAY = (10, 40)

# Файлы
LISTINGS_FILE = "listings.json"
DEALS_FILE = "deals.json"
SESSION_FILE = "broker_session"

# ============================================================
#  СЦЕНАРИЙ ДИАЛОГА
# ============================================================

# Стадии диалога
STAGE_NEW = "new"                  # ещё не написали
STAGE_OPENER = "opener"            # отправили первое сообщение
STAGE_OFFER_SENT = "offer_sent"    # отправили предложение
STAGE_PRICE_ASKED = "price_asked"  # спросили крайнюю цену
STAGE_DEAL_PROPOSED = "deal"       # предложили схему работы
STAGE_DETAILS = "details"          # собираем инфо о машине
STAGE_PHOTOS = "photos"            # просим фото
STAGE_ACTIVE = "active"            # активно работаем
STAGE_CLOSED = "closed"            # отказ / закрыт
STAGE_DONE = "done"                # сделка завершена


# Фразы по стадиям
SCRIPTS = {
    STAGE_OPENER: [
        "Здравствуйте\nещё продаёте ?",
    ],

    STAGE_OFFER_SENT: [
        "Могу дополнительно продвигать её через свои соцсети и искать покупателей. Если клиент приходит через меня — беру комиссию после сделки. Можем попробовать поработать.",
    ],

    STAGE_PRICE_ASKED: [
        "Скажите свою последнюю цену",
    ],

    STAGE_DEAL_PROPOSED: [
        "Можем тогда если найду покупателя выше вашей крайней цены, все что сверху себе возьму. Без наглости 🙂",
    ],

    STAGE_DETAILS: [
        "по машине есть какие то повреждение по кузову и есть ли запрет ?\nПроверьте машину в Автотеке. Отчёт может показать:\n→ ДТП и повреждения, о которых не сообщали в ГИБДД\n→ скрутки пробега\n→ работу в такси\n→ ограничения на регистрацию\n→ залог и многое другое\n\nПроверить от 115 ₽",
    ],

    STAGE_PHOTOS: [
        "Можете тогда какие нибудь другие, но хорошие фотографии с машиной ещё скинуть",
        "Хорошо, тогда давайте я с этими буду выкладывать\nсможете ещё раз мне их скинуть",
    ],

    STAGE_ACTIVE: [
        "Хорошо, если что напишу вам 👍",
        "Отлично, берусь за работу. Как только найду покупателя — сразу свяжусь.",
    ],
}


# Ключевые слова для определения стадии по ответу продавца
def detect_intent(text: str) -> str:
    """Определяет намерение продавца по его ответу."""
    t = text.lower().strip()

    # Отказ
    refuse_words = ["нет", "не надо", "не интересно", "не нужно", "сами", "продали", "продала", "продал", "снял"]
    if any(w in t for w in refuse_words):
        return "refuse"

    # Вопрос о комиссии
    commission_words = ["комисс", "процент", "сколько берёт", "сколько берешь", "сколько возьм"]
    if any(w in t for w in commission_words):
        return "commission_question"

    # Цена (число)
    if re.search(r'\b\d{3,7}\b', t):
        return "price_given"

    # Согласие
    agree_words = ["ок", "ok", "хорошо", "договорились", "давайте", "попробуем", "согласн", "можно"]
    if any(w in t for w in agree_words):
        return "agree"

    # Вопрос
    if "?" in t or any(w in t for w in ["что", "как", "когда", "где", "зачем", "почему"]):
        return "question"

    return "other"


# ============================================================
#  ХРАНИЛИЩЕ СДЕЛОК
# ============================================================

def load_deals() -> dict:
    if Path(DEALS_FILE).exists():
        return json.loads(Path(DEALS_FILE).read_text(encoding="utf-8"))
    return {}


def save_deals(deals: dict) -> None:
    Path(DEALS_FILE).write_text(json.dumps(deals, ensure_ascii=False, indent=2), encoding="utf-8")


def get_deal(deals: dict, contact_id: str) -> dict:
    if contact_id not in deals:
        deals[contact_id] = {
            "stage": STAGE_NEW,
            "contact_id": contact_id,
            "listing": None,
            "price_bottom": None,
            "notes": [],
            "created": datetime.datetime.now().isoformat(),
            "updated": datetime.datetime.now().isoformat(),
        }
    return deals[contact_id]


def update_deal(deals: dict, contact_id: str, **kwargs) -> None:
    deal = get_deal(deals, contact_id)
    deal.update(kwargs)
    deal["updated"] = datetime.datetime.now().isoformat()
    save_deals(deals)


# ============================================================
#  ОСНОВНАЯ ЛОГИКА
# ============================================================

def next_message(deal: dict, intent: str) -> tuple[str | None, str | None]:
    """
    Возвращает (текст_сообщения, новая_стадия) исходя из текущей стадии и намерения продавца.
    Если отвечать не нужно — (None, None).
    """
    stage = deal["stage"]

    if intent == "refuse":
        return None, STAGE_CLOSED

    if stage == STAGE_OPENER:
        # Продавец ответил что-то → отправляем предложение
        return SCRIPTS[STAGE_OFFER_SENT][0], STAGE_OFFER_SENT

    if stage == STAGE_OFFER_SENT:
        if intent == "commission_question":
            # Вопрос о комиссии → спрашиваем крайнюю цену
            return SCRIPTS[STAGE_PRICE_ASKED][0], STAGE_PRICE_ASKED
        if intent == "agree":
            return SCRIPTS[STAGE_PRICE_ASKED][0], STAGE_PRICE_ASKED
        # Любой ответ → спрашиваем цену
        return SCRIPTS[STAGE_PRICE_ASKED][0], STAGE_PRICE_ASKED

    if stage == STAGE_PRICE_ASKED:
        if intent == "price_given":
            # Получили цену → предлагаем схему
            return SCRIPTS[STAGE_DEAL_PROPOSED][0], STAGE_DEAL_PROPOSED
        return SCRIPTS[STAGE_PRICE_ASKED][0], STAGE_PRICE_ASKED

    if stage == STAGE_DEAL_PROPOSED:
        if intent in ("agree", "other"):
            return SCRIPTS[STAGE_DETAILS][0], STAGE_DETAILS
        return None, None

    if stage == STAGE_DETAILS:
        if intent == "agree":
            return SCRIPTS[STAGE_PHOTOS][0], STAGE_PHOTOS
        # Рассказал о состоянии → просим фото
        return SCRIPTS[STAGE_PHOTOS][0], STAGE_PHOTOS

    if stage == STAGE_PHOTOS:
        # Любой ответ про фото → закрываем первичный диалог
        return SCRIPTS[STAGE_ACTIVE][0], STAGE_ACTIVE

    if stage == STAGE_ACTIVE:
        return None, None

    return None, None


# ============================================================
#  TELEGRAM КЛИЕНТ
# ============================================================

async def send_with_delay(client, peer, text: str, delay_range=REPLY_DELAY):
    """Отправляет сообщение с паузой (имитация живого человека)."""
    delay = random.uniform(*delay_range)
    print(f"  [ждём {delay:.0f}с перед отправкой]")
    await asyncio.sleep(delay)
    await client.send_message(peer, text)
    print(f"  → Отправлено: {text[:60]}...")


async def run():
    if not API_ID or not API_HASH or not PHONE:
        print("❌ Заполни API_ID, API_HASH и PHONE в начале файла tg_bot.py")
        print("   Получи на https://my.telegram.org/apps")
        return

    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await client.start(phone=PHONE)
    print("✅ Авторизован в Telegram")

    deals = load_deals()

    # ── Отправка первых сообщений новым контактам ──────────────────────────
    listings = json.loads(Path(LISTINGS_FILE).read_text(encoding="utf-8"))

    new_count = 0
    for item in listings:
        phone = item.get("phone") or item.get("seller_phone")
        username = item.get("username") or item.get("seller_username")

        if not phone and not username:
            continue

        contact_id = phone or username
        deal = get_deal(deals, contact_id)

        if deal["stage"] != STAGE_NEW:
            continue  # уже писали

        if new_count >= MAX_NEW_PER_RUN:
            print(f"[лимит] Достигнут лимит {MAX_NEW_PER_RUN} новых диалогов за запуск")
            break

        try:
            if phone:
                # Добавляем контакт по номеру
                await client(ImportContactsRequest([
                    InputPhoneContact(
                        client_id=random.randint(0, 9999),
                        phone=phone,
                        first_name=item.get("title", "")[:20],
                        last_name="",
                    )
                ]))
                peer = phone
            else:
                peer = username

            text = SCRIPTS[STAGE_OPENER][0]
            await send_with_delay(client, peer, text, delay_range=DELAY_BETWEEN_CONTACTS)

            update_deal(deals, contact_id,
                        stage=STAGE_OPENER,
                        listing=item.get("url"),
                        peer=peer)
            new_count += 1
            print(f"[{new_count}] Написали → {contact_id} | {item.get('title','')[:40]}")

        except Exception as e:
            print(f"[err] {contact_id}: {e}")
            update_deal(deals, contact_id, stage=STAGE_CLOSED, notes=[str(e)])

    print(f"\nНовых диалогов открыто: {new_count}")

    # ── Слушаем входящие ответы ────────────────────────────────────────────
    print("\n👂 Слушаем ответы... (Ctrl+C для остановки)\n")

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        sender = await event.get_sender()
        if not sender:
            return

        # Определяем contact_id
        phone = getattr(sender, "phone", None)
        username = getattr(sender, "username", None)
        contact_id = (f"+{phone}" if phone else None) or (f"@{username}" if username else str(sender.id))

        text = event.raw_text.strip()
        print(f"\n← [{contact_id}]: {text}")

        deal = get_deal(deals, contact_id)

        if deal["stage"] in (STAGE_CLOSED, STAGE_DONE):
            return

        # Сохраняем сообщение в заметки
        deal.setdefault("notes", []).append({"from": "seller", "text": text,
                                              "time": datetime.datetime.now().isoformat()})

        # Извлекаем цену если есть
        price_match = re.search(r'\b(\d{3,4})\b', text)
        if price_match and deal["stage"] == STAGE_PRICE_ASKED:
            deal["price_bottom"] = int(price_match.group(1)) * 1000
            print(f"  [цена] Крайняя: {deal['price_bottom']:,} ₽")

        intent = detect_intent(text)
        print(f"  [intent] {intent} | stage: {deal['stage']}")

        reply_text, new_stage = next_message(deal, intent)

        if new_stage:
            update_deal(deals, contact_id, stage=new_stage)
            print(f"  [stage] → {new_stage}")

        if reply_text:
            try:
                await send_with_delay(client, event.peer_id, reply_text)
                deal.setdefault("notes", []).append({"from": "me", "text": reply_text,
                                                      "time": datetime.datetime.now().isoformat()})
                save_deals(deals)
            except Exception as e:
                print(f"  [err] Не смог отправить: {e}")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(run())
