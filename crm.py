"""
CRM-система удержания пользователей PerekupDrive.

Отвечает за персональные retention-сообщения на основе реальных событий:
- новое объявление по поиску/мониторингу
- окончание тестового/платного периода
- возврат неактивных пользователей
- благодарность за оплату

Архитектура:
- PostgreSQL — основное хранилище (bot_users + crm_events).
- JSONL-файлы — fallback, если PG недоступна.
- Шедулер запускается в control_bot.py и каждые 5 минут проверяет триггеры
  и отправляет одно сообщение пользователю с соблюдением приоритетов и антиспама.
"""
import asyncio
import datetime
import json
import os
import time
from itertools import groupby
from pathlib import Path
from typing import Any

# ── Настройки ─────────────────────────────────────────────────────────
_DATA_DIR = Path("data")
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_DB_URL = os.getenv("DATABASE_URL", "")

CRM_USER_STATE_FILE = _DATA_DIR / "crm_user_state.json"
CRM_EVENTS_FILE = _DATA_DIR / "crm_events.jsonl"

CRM_ENABLED_DEFAULT = True
CRM_COOLDOWN_HOURS = 24  # базовый антиспам: 1 сообщение в сутки
CRM_OVERRIDE_COOLDOWN_SEC = 1800  # более важный триггер может перебить менее важный через 30 мин

# Приоритеты: чем меньше число, тем выше приоритет.
EVENT_PRIORITY = {
    "new_listing": 1,
    "trial_ending": 2,
    "subscription_ending": 2,
    "trial_ended": 3,
    "subscription_ended": 3,
    "continue_search": 4,
    "payment_thanks": 5,
}

# Иконки площадок для сообщений
_SOURCE_ICON = {
    "avito": "🟠 Авито",
    "drom": "🔵 Дром",
    "autoru": "🔴 Auto.ru",
    "vk": "💙 ВКонтакте",
    "tg": "✈️ Telegram",
    "youla": "🟡 Юла",
}


# ── Подключение к БД ──────────────────────────────────────────────────
def _get_db():
    """Возвращает соединение с PostgreSQL или fallback на control_bot._get_db()."""
    if _DB_URL:
        try:
            import psycopg2
            conn = psycopg2.connect(_DB_URL, connect_timeout=3)
            conn.autocommit = True
            return conn
        except Exception:
            pass
    # Динамический импорт, чтобы избежать циклического импорта на старте.
    try:
        import control_bot

        return control_bot._get_db()
    except Exception:
        return None


# ── Fallback-хранилища (JSON) ─────────────────────────────────────────
def _load_user_state() -> dict:
    if not CRM_USER_STATE_FILE.exists():
        return {}
    try:
        return json.loads(CRM_USER_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_user_state(state: dict) -> None:
    CRM_USER_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _state_for_uid(uid: int, state: dict | None = None) -> dict:
    """Возвращает (и создаёт при необходимости) запись состояния пользователя."""
    if state is None:
        state = _load_user_state()
    return state.setdefault(
        str(uid),
        {
            "last_search": None,
            "last_crm_message": None,
            "last_crm_type": None,
            "last_activity": None,
            "crm_enabled": True,
        },
    )


def _load_pending_events() -> list[dict]:
    """Считывает необработанные события из JSONL-файла."""
    rows = []
    if not CRM_EVENTS_FILE.exists():
        return rows
    try:
        for line in CRM_EVENTS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if not ev.get("processed"):
                rows.append(ev)
    except Exception:
        pass
    return rows


def _append_event(uid: int, event_type: str, priority: int, payload: dict) -> None:
    ev = {
        "id": f"{int(time.time() * 1000)}_{uid}_{event_type}",
        "uid": uid,
        "event_type": event_type,
        "priority": priority,
        "payload": payload or {},
        "created_at": datetime.datetime.utcnow().isoformat(),
        "sent_at": None,
        "processed": False,
    }
    with open(CRM_EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _mark_file_event_processed(event_id: str, sent_at: datetime.datetime | None = None) -> None:
    if not CRM_EVENTS_FILE.exists():
        return
    lines = []
    changed = False
    for line in CRM_EVENTS_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            lines.append(line)
            continue
        try:
            ev = json.loads(line)
        except Exception:
            lines.append(line)
            continue
        if ev.get("id") == event_id and not ev.get("processed"):
            ev["processed"] = True
            ev["sent_at"] = (
                sent_at.isoformat() if sent_at else datetime.datetime.utcnow().isoformat()
            )
            changed = True
        lines.append(json.dumps(ev, ensure_ascii=False))
    if changed:
        CRM_EVENTS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Миграции ───────────────────────────────────────────────────────────
def init_crm_db() -> None:
    """Создаёт/обновляет таблицы и индексы для CRM."""
    db = _get_db()
    if db:
        try:
            with db.cursor() as cur:
                cur.execute(
                    "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_search TIMESTAMP"
                )
                cur.execute(
                    "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_crm_message TIMESTAMP"
                )
                cur.execute(
                    "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_crm_type TEXT"
                )
                cur.execute(
                    "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS last_activity TIMESTAMP"
                )
                cur.execute(
                    "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS crm_enabled BOOLEAN DEFAULT TRUE"
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS crm_events (
                        id SERIAL PRIMARY KEY,
                        uid BIGINT NOT NULL,
                        event_type TEXT NOT NULL,
                        priority INTEGER NOT NULL,
                        payload JSONB,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        sent_at TIMESTAMPTZ,
                        processed BOOLEAN DEFAULT FALSE
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_crm_events_user_active "
                    "ON crm_events(uid, processed, priority, created_at)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_crm_events_unprocessed "
                    "ON crm_events(created_at) WHERE processed = FALSE"
                )
            print("  [CRM] миграции PostgreSQL применены")
        except Exception as e:
            print(f"  [CRM] ошибка миграций PG: {e}")
    else:
        # Fallback: файлы уже создаются при первой записи.
        CRM_EVENTS_FILE.touch(exist_ok=True)
        print("  [CRM] PostgreSQL недоступна — используем файловый fallback")


# ── Работа с состоянием пользователя ──────────────────────────────────
def _to_timestamp(value: Any) -> float | None:
    """Преобразует datetime/iso-строку/число в unix-timestamp."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.datetime):
        return value.timestamp()
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value).timestamp()
        except Exception:
            return None
    return None


def _get_user_db_state(uid: int) -> dict | None:
    db = _get_db()
    if not db:
        return None
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT last_search, last_crm_message, last_crm_type, last_activity, "
                "crm_enabled FROM bot_users WHERE uid = %s",
                (uid,),
            )
            row = cur.fetchone()
            if row:
                return {
                    "last_search": row[0],
                    "last_crm_message": row[1],
                    "last_crm_type": row[2],
                    "last_activity": row[3],
                    "crm_enabled": row[4],
                }
    except Exception:
        pass
    return None


def _get_last_crm_info(uid: int) -> dict:
    """Возвращает последнее CRM-состояние пользователя (из PG или файла)."""
    db_state = _get_user_db_state(uid)
    if db_state is not None:
        return {
            "last_search": _to_timestamp(db_state.get("last_search")),
            "last_crm_message": _to_timestamp(db_state.get("last_crm_message")),
            "last_crm_type": db_state.get("last_crm_type"),
            "last_activity": _to_timestamp(db_state.get("last_activity")),
            "crm_enabled": (
                bool(db_state["crm_enabled"])
                if db_state.get("crm_enabled") is not None
                else CRM_ENABLED_DEFAULT
            ),
        }
    state = _state_for_uid(uid)
    return {
        "last_search": _to_timestamp(state.get("last_search")),
        "last_crm_message": _to_timestamp(state.get("last_crm_message")),
        "last_crm_type": state.get("last_crm_type"),
        "last_activity": _to_timestamp(state.get("last_activity")),
        "crm_enabled": bool(state.get("crm_enabled", CRM_ENABLED_DEFAULT)),
    }


def _update_user_field(uid: int, field: str, value: datetime.datetime | None) -> None:
    """Обновляет одно поля в bot_users (PG) или в JSON fallback."""
    db = _get_db()
    if db:
        try:
            with db.cursor() as cur:
                if value is None:
                    cur.execute(
                        f"UPDATE bot_users SET {field} = NULL WHERE uid = %s", (uid,)
                    )
                else:
                    cur.execute(
                        f"UPDATE bot_users SET {field} = %s WHERE uid = %s",
                        (value, uid),
                    )
        except Exception:
            pass
    # Зеркалируем в fallback-файл
    state = _load_user_state()
    st = _state_for_uid(uid, state)
    st[field] = value.isoformat() if value else None
    _save_user_state(state)


def update_last_seen(uid: int) -> None:
    """Обновляет last_activity при каждом взаимодействии пользователя с ботом."""
    _update_user_field(uid, "last_activity", datetime.datetime.now())


def update_last_activity(uid: int) -> None:
    """Алиас для update_last_seen."""
    update_last_seen(uid)


def update_last_search(uid: int) -> None:
    """Фиксирует время последнего поиска (для триггера возврата)."""
    _update_user_field(uid, "last_search", datetime.datetime.now())


def set_crm_enabled(uid: int, enabled: bool) -> None:
    """Включает/отключает CRM-сообщения для пользователя."""
    _update_user_field(uid, "crm_enabled", enabled)
    state = _load_user_state()
    st = _state_for_uid(uid, state)
    st["crm_enabled"] = bool(enabled)
    _save_user_state(state)


# ── Очередь событий ───────────────────────────────────────────────────
def queue_event(uid: int, event_type: str, payload: dict | None = None, priority: int | None = None) -> None:
    """Добавляет событие в очередь CRM."""
    priority = priority or EVENT_PRIORITY.get(event_type, 99)
    db = _get_db()
    if db:
        try:
            import psycopg2.extras

            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO crm_events (uid, event_type, priority, payload) "
                    "VALUES (%s, %s, %s, %s::jsonb)",
                    (uid, event_type, priority, json.dumps(payload or {})),
                )
            return
        except Exception as e:
            print(f"  [CRM] ошибка записи события в PG: {e}")
    _append_event(uid, event_type, priority, payload or {})


def _has_pending_event(uid: int, event_type: str) -> bool:
    """Проверяет, есть ли уже необработанное событие такого типа у пользователя."""
    db = _get_db()
    if db:
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM crm_events WHERE uid = %s AND event_type = %s "
                    "AND processed = FALSE LIMIT 1",
                    (uid, event_type),
                )
                return cur.fetchone() is not None
        except Exception:
            pass
    for ev in _load_pending_events():
        if ev.get("uid") == uid and ev.get("event_type") == event_type:
            return True
    return False


def _last_crm_type_was(uid: int, event_type: str) -> bool:
    return _get_last_crm_info(uid).get("last_crm_type") == event_type


# ── Отправка сообщений ─────────────────────────────────────────────────
def _bot_username() -> str:
    """Возвращает username бота, если известен."""
    try:
        import control_bot

        return getattr(control_bot, "BOT_USERNAME", "") or "PerekupDriveBot"
    except Exception:
        return "PerekupDriveBot"


def _load_user_settings(uid: int) -> dict:
    try:
        import control_bot

        return control_bot.load_settings(uid)
    except Exception:
        return {}


def _build_message(event_type: str, payload: dict) -> tuple[str, Any]:
    """Формирует текст и клавиатуру для CRM-сообщения."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    if event_type == "new_listing":
        title = payload.get("title", "")
        price = payload.get("price", "")
        url = payload.get("url", "")
        source = payload.get("source", "")
        source_label = _SOURCE_ICON.get(source, "📌")
        text = (
            f"🔔 <b>По вашему поиску появилось новое объявление</b>\n\n"
            f"🚗 {title}\n"
            f"💰 {price}\n"
            f"📌 {source_label}\n"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="👀 Посмотреть", url=url)],
            ]
        )
        return text, kb

    if event_type in ("trial_ending", "subscription_ending"):
        days_left = payload.get("days_left", 1)
        text = (
            f"⏳ <b>Завтра закончится доступ</b>\n\n"
            f"Осталось <b>{days_left} дн.</b>\n\n"
            f"После этого бот перестанет автоматически искать новые объявления по вашим фильтрам."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Продлить подписку", callback_data="open_subscribe")],
            ]
        )
        return text, kb

    if event_type in ("trial_ended", "subscription_ended"):
        text = (
            "<b>Мониторинг остановлен</b>\n\n"
            "Ваши фильтры сохранены.\n\n"
            "Возобновите подписку, чтобы снова получать новые авто ниже рынка."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Возобновить мониторинг", callback_data="open_subscribe")],
            ]
        )
        return text, kb

    if event_type == "continue_search":
        uid = payload.get("uid")
        s = _load_user_settings(uid) if uid else {}
        region = s.get("region", "")
        brand = s.get("brand", "")
        pmax = s.get("price_max", 99_000_000)
        try:
            import control_bot

            regions = getattr(control_bot, "REGIONS", {})
            region_name = regions.get(region, region)
        except Exception:
            region_name = region
        brand_label = brand.capitalize() if brand and brand != "any" else "Любая марка"
        text = (
            f"<b>Продолжить последний поиск?</b>\n\n"
            f"🚘 {brand_label}\n"
            f"📍 {region_name}\n"
            f"💰 до {pmax:,.0f} ₽".replace(",", " ") + "\n\n"
            f"Проверь, появились ли новые объявления."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Проверить новые объявления", callback_data="crm_continue_search")],
            ]
        )
        return text, kb

    if event_type == "payment_thanks":
        until_date = payload.get("until_date", "")
        text = (
            f"<b>Спасибо за оплату!</b>\n\n"
            f"Подписка активна до <b>{until_date}</b>.\n\n"
            f"Включи мониторинг, чтобы первым получать выгодные авто."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔔 Включить мониторинг", callback_data="open_monitor")],
            ]
        )
        return text, kb

    # Неизвестный тип — не отправляем.
    return "", None


def _can_send_crm_event(uid: int, event_type: str, priority: int) -> bool:
    """
    Проверяет антиспам-правила.
    - Базовый cooldown: 24 часа.
    - Более важный триггер может перебить менее важный через 30 минут.
    - payment_thanks (транзакционное) отправляется всегда.
    - new_listing имеет свой собственный интервал (1 час) в notify_new_listing.
    """
    info = _get_last_crm_info(uid)
    if not info.get("crm_enabled", True):
        return False
    last_ts = info.get("last_crm_message")
    last_type = info.get("last_crm_type")
    if last_ts is None:
        return True
    if event_type == "payment_thanks":
        return True
    elapsed = time.time() - last_ts
    if elapsed >= CRM_COOLDOWN_HOURS * 3600:
        return True
    # Перебивание менее важного сообщения более важным
    if last_type and EVENT_PRIORITY.get(event_type, 99) < EVENT_PRIORITY.get(last_type, 99):
        return elapsed >= CRM_OVERRIDE_COOLDOWN_SEC
    return False


def _record_crm_send(uid: int, event_type: str) -> None:
    """Фиксирует факт отправки CRM-сообщения."""
    now = datetime.datetime.now()
    db = _get_db()
    if db:
        try:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE bot_users SET last_crm_message = %s, last_crm_type = %s "
                    "WHERE uid = %s",
                    (now, event_type, uid),
                )
        except Exception:
            pass
    state = _load_user_state()
    st = _state_for_uid(uid, state)
    st["last_crm_message"] = now.isoformat()
    st["last_crm_type"] = event_type
    st["last_activity"] = now.isoformat()
    _save_user_state(state)


async def _send_crm_message(bot, uid: int, event_type: str, payload: dict) -> bool:
    """Отправляет одно CRM-сообщение и фиксирует факт отправки."""
    from aiogram.types import InlineKeyboardMarkup

    text, kb = _build_message(event_type, payload)
    if not text:
        return False
    if kb is None:
        kb = InlineKeyboardMarkup(inline_keyboard=[])
    try:
        await bot.send_message(
            uid,
            text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        _record_crm_send(uid, event_type)
        print(f"  [CRM] отправлено {event_type} → uid {uid}")
        return True
    except Exception as e:
        print(f"  [CRM] ошибка отправки {event_type} uid {uid}: {e}")
        return False


async def notify_new_listing(bot, uid: int, item: dict) -> None:
    """
    Уведомляет о новом объявлении (вызывается из монитора).
    Отправляет CRM-напоминание не чаще раза в час, чтобы не спамить.
    """
    if not item or not item.get("url"):
        return
    info = _get_last_crm_info(uid)
    if not info.get("crm_enabled", True):
        return
    last_ts = info.get("last_crm_message")
    if last_ts and time.time() - last_ts < 3600:
        return
    payload = {
        "title": item.get("title", ""),
        "price": item.get("price", ""),
        "url": item.get("url", ""),
        "source": item.get("source", ""),
        "region": item.get("_monitor_region", ""),
    }
    await _send_crm_message(bot, uid, "new_listing", payload)


async def send_payment_thanks(bot, uid: int, until_date: str) -> None:
    """Благодарность за оплату — транзакционное сообщение, отправляется всегда."""
    await _send_crm_message(bot, uid, "payment_thanks", {"until_date": until_date})


# ── Триггеры ───────────────────────────────────────────────────────────
def _check_triggers_sync() -> None:
    """Синхронная проверка условий для постановки событий в очередь."""
    try:
        import control_bot
    except Exception:
        return

    try:
        all_uids = control_bot._all_user_ids()
    except Exception:
        return

    now = time.time()
    for uid in all_uids:
        try:
            info = _get_last_crm_info(uid)
            if not info.get("crm_enabled", True):
                continue

            # Подписка/триал
            sub = control_bot._subscription_info(uid)
            days_left = int(sub.get("days_left", 0))
            ended = bool(sub.get("ended", True))
            is_paid = bool(sub.get("is_paid", False))

            # За сутки до окончания
            if days_left == 1 and not ended:
                ev_type = "subscription_ending" if is_paid else "trial_ending"
                if not _has_pending_event(uid, ev_type) and not _last_crm_type_was(uid, ev_type):
                    queue_event(uid, ev_type, {"days_left": days_left})

            # После окончания
            if ended and days_left <= 0:
                ev_type = "subscription_ended" if is_paid else "trial_ended"
                if not _has_pending_event(uid, ev_type) and not _last_crm_type_was(uid, ev_type):
                    queue_event(uid, ev_type)

            # Возврат неактивного пользователя (7 дней без активности и есть поиск)
            last_activity = info.get("last_activity") or info.get("last_search")
            if (
                last_activity
                and (now - last_activity) > 7 * 24 * 3600
                and info.get("last_search")
            ):
                if not _has_pending_event(uid, "continue_search") and not _last_crm_type_was(uid, "continue_search"):
                    queue_event(uid, "continue_search", {"uid": uid})

        except Exception as e:
            print(f"  [CRM] ошибка проверки триггеров uid {uid}: {e}")


async def _check_triggers() -> None:
    """Асинхронная обёртка для проверки триггеров."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _check_triggers_sync)


# ── Обработка очереди ─────────────────────────────────────────────────
def _mark_processed(event_id) -> None:
    db = _get_db()
    if db:
        try:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE crm_events SET processed = TRUE, sent_at = NOW() WHERE id = %s",
                    (event_id,),
                )
        except Exception:
            pass
    else:
        _mark_file_event_processed(str(event_id))


async def _process_crm_queue(bot) -> None:
    """Выбирает по одному самому важному событию на пользователя и отправляет."""
    db = _get_db()
    rows = []
    if db:
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT id, uid, event_type, priority, payload FROM crm_events "
                    "WHERE processed = FALSE ORDER BY priority ASC, created_at ASC"
                )
                for r in cur.fetchall():
                    payload = r[4]
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except Exception:
                            payload = {}
                    rows.append(
                        {
                            "id": r[0],
                            "uid": r[1],
                            "event_type": r[2],
                            "priority": r[3],
                            "payload": payload or {},
                        }
                    )
        except Exception as e:
            print(f"  [CRM] ошибка чтения очереди: {e}")
    else:
        rows = _load_pending_events()

    # Группируем по uid, берём самый приоритетный и самый старый
    rows.sort(key=lambda x: (x["uid"], x["priority"], x.get("created_at", "")))
    for uid, events in groupby(rows, key=lambda x: x["uid"]):
        ev = next(events)  # первый — самый важный
        try:
            if not _can_send_crm_event(uid, ev["event_type"], ev["priority"]):
                continue
            ok = await _send_crm_message(bot, uid, ev["event_type"], ev.get("payload", {}))
            if ok:
                _mark_processed(ev["id"])
        except Exception as e:
            print(f"  [CRM] ошибка обработки события {ev.get('id')} для {uid}: {e}")


# ── Планировщик ──────────────────────────────────────────────────────
async def start_crm_scheduler(bot, interval: int = 300) -> None:
    """Фоновый цикл: проверяет триггеры и отправляет CRM-сообщения."""
    print(f"  [CRM] планировщик запущен (интервал {interval}с)")
    while True:
        try:
            await asyncio.sleep(interval)
            await _check_triggers()
            await _process_crm_queue(bot)
        except Exception as e:
            print(f"  [CRM] ошибка шедулера: {e}")
