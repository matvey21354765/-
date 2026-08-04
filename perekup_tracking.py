"""Отслеживание объявлений для PerekupDrive: история цен, наблюдения,
персональная статистика и защита уведомлений от дублей.

Модуль изолирован от парсеров и главного бота: он только хранит события и
отвечает на вопросы «подешевело ли», «кому слать», «что показать за сегодня».

Хранилище — SQLite (как seller_analyzer.py), путь настраивается переменной
PEREKUP_DB_PATH. Все миграции идемпотентны и не удаляют данные.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_DB_PATH = Path(os.environ.get("PEREKUP_DB_PATH", "data/perekup_tracking.db"))
_LOCK = threading.RLock()
_INIT_DONE = False

# Пороги «значимого» снижения цены (по умолчанию, пользователь может менять).
DEFAULT_MIN_DROP_PCT = 3.0
DEFAULT_MIN_DROP_RUB_CHEAP = 10_000    # для авто дешевле 300 000 ₽
DEFAULT_MIN_DROP_RUB = 20_000          # для остальных
CHEAP_CAR_THRESHOLD = 300_000

DEFAULT_WATCH_DAYS = 14


# ──────────────────────────────────────────────────────────────────────
# Соединение и схема
# ──────────────────────────────────────────────────────────────────────
def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _add_column(conn: sqlite3.Connection, table: str, column: str, type_sql: str) -> None:
    """Безопасно добавляет колонку (миграция без потери данных)."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_sql}")
    except sqlite3.OperationalError:
        pass  # колонка уже существует


def init_db() -> None:
    """Идемпотентно создаёт/обновляет схему. Безопасно вызывать многократно."""
    global _INIT_DONE
    with _LOCK:
        conn = _connect()
        try:
            cur = conn.cursor()
            # История цен: по одной строке на СОБЫТИЕ (не на каждую встречу).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS listing_price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_key TEXT NOT NULL,
                    source TEXT,
                    price INTEGER NOT NULL,
                    prev_price INTEGER,
                    event_type TEXT NOT NULL,
                    detected_at REAL NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_lph_key ON listing_price_history(listing_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_lph_time ON listing_price_history(detected_at)")

            # Текущее состояние объявления (быстрый доступ без агрегации истории).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS listing_state (
                    listing_key TEXT PRIMARY KEY,
                    source TEXT,
                    title TEXT,
                    url TEXT,
                    first_price INTEGER,
                    prev_price INTEGER,
                    current_price INTEGER,
                    min_price INTEGER,
                    first_seen_at REAL,
                    last_seen_at REAL,
                    last_price_change_at REAL,
                    drops_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'active'
                )
            """)

            # Наблюдение пользователя за объявлением.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS listing_watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    listing_key TEXT NOT NULL,
                    source TEXT,
                    title TEXT,
                    url TEXT,
                    created_at REAL NOT NULL,
                    watch_until REAL,
                    last_known_price INTEGER,
                    last_notified_price INTEGER,
                    status TEXT DEFAULT 'active',
                    UNIQUE(user_id, listing_key)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_watch_key ON listing_watches(listing_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_watch_user ON listing_watches(user_id, status)")

            # Факт открытия объявления пользователем.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS listing_views (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    listing_key TEXT NOT NULL,
                    source TEXT,
                    viewed_at REAL NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_views_user ON listing_views(user_id, viewed_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_views_key ON listing_views(user_id, listing_key)")

            # Антидубли уведомлений.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notification_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    listing_key TEXT,
                    event_type TEXT NOT NULL,
                    event_signature TEXT NOT NULL,
                    sent_at REAL NOT NULL,
                    UNIQUE(user_id, event_signature)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_notif_user ON notification_history(user_id, sent_at)")

            # Персональная дневная статистика (накопительные счётчики).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_user_stats (
                    user_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    checked INTEGER DEFAULT 0,
                    matched INTEGER DEFAULT 0,
                    below_market INTEGER DEFAULT 0,
                    new_2h INTEGER DEFAULT 0,
                    new_24h INTEGER DEFAULT 0,
                    price_drops INTEGER DEFAULT 0,
                    relisted INTEGER DEFAULT 0,
                    opened INTEGER DEFAULT 0,
                    saved INTEGER DEFAULT 0,
                    calls INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, day)
                )
            """)
            conn.commit()
            _INIT_DONE = True
        finally:
            conn.close()


@contextmanager
def _db():
    if not _INIT_DONE:
        init_db()
    with _LOCK:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


# ──────────────────────────────────────────────────────────────────────
# Ключ объявления
# ──────────────────────────────────────────────────────────────────────
def listing_key(item: dict) -> str:
    """Стабильный ключ объявления: источник + нормализованный URL/ID."""
    src = str(item.get("source") or "").strip().lower()
    raw = str(item.get("url") or "").strip()
    raw = re.sub(r"[?#].*$", "", raw).rstrip("/").lower()
    sid = str(item.get("_source_id") or item.get("source_id") or "").strip()
    ident = sid or raw
    return f"{src}:{ident}" if ident else ""


# ──────────────────────────────────────────────────────────────────────
# История цен
# ──────────────────────────────────────────────────────────────────────
def record_listing(item: dict, now: float | None = None) -> dict:
    """Фиксирует встречу объявления и возвращает событие изменения цены.

    Возвращает dict:
      {"event": "first_seen"|"price_drop"|"price_increase"|"relisted"|"none",
       "old_price": int, "new_price": int, "drop": int, "drop_pct": float,
       "drops_count": int, "days_on_sale": float, "first_price": int}
    """
    key = listing_key(item)
    price = int(item.get("_price_int") or 0)
    if not key or price <= 0:
        return {"event": "none"}
    now = time.time() if now is None else float(now)
    src = str(item.get("source") or "")
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM listing_state WHERE listing_key = ?", (key,)
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO listing_state (listing_key, source, title, url,
                       first_price, prev_price, current_price, min_price,
                       first_seen_at, last_seen_at, last_price_change_at,
                       drops_count, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,0,'active')""",
                (key, src, str(item.get("title") or "")[:300], str(item.get("url") or ""),
                 price, None, price, price, now, now, now),
            )
            conn.execute(
                """INSERT INTO listing_price_history
                       (listing_key, source, price, prev_price, event_type, detected_at)
                   VALUES (?,?,?,?,?,?)""",
                (key, src, price, None, "first_seen", now),
            )
            return {"event": "first_seen", "old_price": 0, "new_price": price,
                    "drop": 0, "drop_pct": 0.0, "drops_count": 0,
                    "days_on_sale": 0.0, "first_price": price}

        old = int(row["current_price"] or 0)
        was_removed = str(row["status"] or "") == "removed"
        days_on_sale = max(0.0, (now - float(row["first_seen_at"] or now)) / 86400.0)
        drops = int(row["drops_count"] or 0)

        if price == old and not was_removed:
            # Цена не менялась — только отметка «видели», без записи в историю.
            conn.execute(
                "UPDATE listing_state SET last_seen_at = ? WHERE listing_key = ?",
                (now, key),
            )
            return {"event": "none", "old_price": old, "new_price": price,
                    "drop": 0, "drop_pct": 0.0, "drops_count": drops,
                    "days_on_sale": days_on_sale,
                    "first_price": int(row["first_price"] or price)}

        if was_removed:
            event = "relisted"
        elif price < old:
            event = "price_drop"
            drops += 1
        else:
            event = "price_increase"

        min_price = min(int(row["min_price"] or price), price)
        conn.execute(
            """UPDATE listing_state
                  SET prev_price = ?, current_price = ?, min_price = ?,
                      last_seen_at = ?, last_price_change_at = ?,
                      drops_count = ?, status = 'active'
                WHERE listing_key = ?""",
            (old, price, min_price, now, now, drops, key),
        )
        conn.execute(
            """INSERT INTO listing_price_history
                   (listing_key, source, price, prev_price, event_type, detected_at)
               VALUES (?,?,?,?,?,?)""",
            (key, src, price, old, event, now),
        )
        drop = max(0, old - price)
        drop_pct = round(drop / old * 100, 1) if old > 0 else 0.0
        return {"event": event, "old_price": old, "new_price": price,
                "drop": drop, "drop_pct": drop_pct, "drops_count": drops,
                "days_on_sale": days_on_sale,
                "first_price": int(row["first_price"] or price)}


def mark_removed(listing_keys: Iterable[str], now: float | None = None) -> int:
    """Помечает объявления как снятые (для определения повторного размещения)."""
    now = time.time() if now is None else float(now)
    keys = [k for k in listing_keys if k]
    if not keys:
        return 0
    with _db() as conn:
        conn.executemany(
            "UPDATE listing_state SET status='removed', last_seen_at=? WHERE listing_key=?",
            [(now, k) for k in keys],
        )
    return len(keys)


def get_listing_state(key: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM listing_state WHERE listing_key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None


def price_history(key: str, limit: int = 20) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            """SELECT price, prev_price, event_type, detected_at
                 FROM listing_price_history WHERE listing_key = ?
                ORDER BY detected_at DESC LIMIT ?""",
            (key, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


def is_significant_drop(old_price: int, new_price: int,
                        min_pct: float = DEFAULT_MIN_DROP_PCT) -> bool:
    """Значимое ли снижение (чтобы не спамить копеечными изменениями)."""
    if old_price <= 0 or new_price <= 0 or new_price >= old_price:
        return False
    drop = old_price - new_price
    pct = drop / old_price * 100
    min_rub = DEFAULT_MIN_DROP_RUB_CHEAP if old_price < CHEAP_CAR_THRESHOLD else DEFAULT_MIN_DROP_RUB
    return pct >= min_pct or drop >= min_rub


# ──────────────────────────────────────────────────────────────────────
# Наблюдение
# ──────────────────────────────────────────────────────────────────────
def add_watch(user_id: int, item: dict, days: int = DEFAULT_WATCH_DAYS,
              now: float | None = None) -> bool:
    """Подписывает пользователя на объявление. Повторный вызов НЕ создаёт дубль.
    Возвращает True, если подписка создана впервые."""
    key = listing_key(item)
    if not key:
        return False
    now = time.time() if now is None else float(now)
    until = None if days <= 0 else now + days * 86400
    with _db() as conn:
        existing = conn.execute(
            "SELECT id FROM listing_watches WHERE user_id=? AND listing_key=?",
            (int(user_id), key),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE listing_watches SET status='active', watch_until=?,
                          last_known_price=? WHERE id=?""",
                (until, int(item.get("_price_int") or 0), existing["id"]),
            )
            return False
        conn.execute(
            """INSERT INTO listing_watches (user_id, listing_key, source, title, url,
                   created_at, watch_until, last_known_price, status)
               VALUES (?,?,?,?,?,?,?,?, 'active')""",
            (int(user_id), key, str(item.get("source") or ""),
             str(item.get("title") or "")[:300], str(item.get("url") or ""),
             now, until, int(item.get("_price_int") or 0)),
        )
        return True


def stop_watch(user_id: int, key: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE listing_watches SET status='stopped' WHERE user_id=? AND listing_key=?",
            (int(user_id), key),
        )


def watchers_for(key: str, now: float | None = None) -> list[dict]:
    """Активные наблюдатели объявления (истёкшие подписки исключаются)."""
    now = time.time() if now is None else float(now)
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM listing_watches
                WHERE listing_key=? AND status='active'
                  AND (watch_until IS NULL OR watch_until > ?)""",
            (key, now),
        ).fetchall()
        return [dict(r) for r in rows]


def expire_watches(now: float | None = None) -> int:
    now = time.time() if now is None else float(now)
    with _db() as conn:
        cur = conn.execute(
            """UPDATE listing_watches SET status='expired'
                WHERE status='active' AND watch_until IS NOT NULL AND watch_until <= ?""",
            (now,),
        )
        return cur.rowcount or 0


def all_active_watches(now: float | None = None, limit: int = 5000) -> list[dict]:
    """Все действующие наблюдения (для фоновой рассылки о снижении цены)."""
    now = time.time() if now is None else float(now)
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM listing_watches
                WHERE status='active' AND (watch_until IS NULL OR watch_until > ?)
                ORDER BY created_at DESC LIMIT ?""",
            (now, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_notified(user_id: int, key: str, price: int) -> None:
    """Запоминает цену, о которой уже сообщили — следующее сообщение только
    при НОВОМ снижении относительно неё."""
    with _db() as conn:
        conn.execute(
            """UPDATE listing_watches
                  SET last_notified_price = ?, last_known_price = ?
                WHERE user_id = ? AND listing_key = ?""",
            (int(price), int(price), int(user_id), key),
        )


def user_watches(user_id: int, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else float(now)
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM listing_watches
                WHERE user_id=? AND status='active'
                  AND (watch_until IS NULL OR watch_until > ?)
                ORDER BY created_at DESC""",
            (int(user_id), now),
        ).fetchall()
        return [dict(r) for r in rows]


# ──────────────────────────────────────────────────────────────────────
# Просмотры
# ──────────────────────────────────────────────────────────────────────
def record_view(user_id: int, item: dict, now: float | None = None) -> None:
    key = listing_key(item)
    if not key:
        return
    now = time.time() if now is None else float(now)
    with _db() as conn:
        conn.execute(
            "INSERT INTO listing_views (user_id, listing_key, source, viewed_at) VALUES (?,?,?,?)",
            (int(user_id), key, str(item.get("source") or ""), now),
        )
    bump_stat(user_id, "opened")


def has_viewed(user_id: int, key: str) -> float | None:
    """Когда пользователь открывал объявление (ts) или None."""
    with _db() as conn:
        row = conn.execute(
            """SELECT viewed_at FROM listing_views
                WHERE user_id=? AND listing_key=? ORDER BY viewed_at DESC LIMIT 1""",
            (int(user_id), key),
        ).fetchone()
        return float(row["viewed_at"]) if row else None


# ──────────────────────────────────────────────────────────────────────
# Антидубли уведомлений
# ──────────────────────────────────────────────────────────────────────
def should_notify(user_id: int, event_type: str, signature: str,
                  listing_key_: str = "", now: float | None = None) -> bool:
    """True — если такое событие пользователю ещё НЕ отправляли.
    Сразу фиксирует отправку, поэтому вызывать непосредственно перед отправкой."""
    if not signature:
        return False
    now = time.time() if now is None else float(now)
    with _db() as conn:
        try:
            conn.execute(
                """INSERT INTO notification_history
                       (user_id, listing_key, event_type, event_signature, sent_at)
                   VALUES (?,?,?,?,?)""",
                (int(user_id), listing_key_, event_type, signature, now),
            )
            return True
        except sqlite3.IntegrityError:
            return False  # уже отправляли это событие


def drop_signature(key: str, old_price: int, new_price: int) -> str:
    """Подпись события снижения: повторное снижение = новое событие."""
    return f"drop:{key}:{int(old_price)}:{int(new_price)}"


# ──────────────────────────────────────────────────────────────────────
# Персональная статистика
# ──────────────────────────────────────────────────────────────────────
def _today(now: float | None = None) -> str:
    ts = time.time() if now is None else float(now)
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=3))).strftime("%Y-%m-%d")


_STAT_FIELDS = {"checked", "matched", "below_market", "new_2h", "new_24h",
                "price_drops", "relisted", "opened", "saved", "calls"}


def bump_stat(user_id: int, field: str, amount: int = 1, now: float | None = None) -> None:
    """Накопительный счётчик персональной статистики за день."""
    if field not in _STAT_FIELDS or amount == 0:
        return
    day = _today(now)
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO daily_user_stats (user_id, day) VALUES (?,?)",
            (int(user_id), day),
        )
        conn.execute(
            f"UPDATE daily_user_stats SET {field} = {field} + ? WHERE user_id=? AND day=?",
            (int(amount), int(user_id), day),
        )


def get_stats(user_id: int, day: str | None = None) -> dict:
    day = day or _today()
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM daily_user_stats WHERE user_id=? AND day=?",
            (int(user_id), day),
        ).fetchone()
    base = {f: 0 for f in _STAT_FIELDS}
    if row:
        for f in _STAT_FIELDS:
            base[f] = int(row[f] or 0)
    base["day"] = day
    return base


# ──────────────────────────────────────────────────────────────────────
# Возрастные разделы выдачи
# ──────────────────────────────────────────────────────────────────────
def age_bucket(item: dict, now: float | None = None) -> str:
    """Раздел по возрасту: 'fresh' (<2ч), 'today' (2-24ч), 'days3' (1-3д), 'old' (>3д)."""
    now = time.time() if now is None else float(now)
    ts = item.get("_first_seen_ts")
    if ts:
        hours = (now - float(ts)) / 3600.0
    else:
        days = item.get("_days_on_site")
        if days is None:
            return "today"
        hours = float(days) * 24.0
        if hours <= 0:
            # «Сегодня» без точного времени — считаем дневным, не «горячим».
            return "today"
    if hours < 2:
        return "fresh"
    if hours < 24:
        return "today"
    if hours < 72:
        return "days3"
    return "old"


def bargain_reasons(item: dict, state: dict | None = None) -> list[str]:
    """Причины вернуться к старому объявлению (для «Простор для торга»)."""
    reasons: list[str] = []
    st = state or get_listing_state(listing_key(item)) or {}
    drops = int(st.get("drops_count") or 0)
    if drops >= 2:
        reasons.append(f"продавец снижал цену {drops} раза")
    elif drops == 1:
        reasons.append("продавец уже снижал цену")
    text = f"{item.get('title','')} {item.get('description','')}".lower()
    if "торг" in text:
        reasons.append("продавец указал торг")
    if any(w in text for w in ("срочно", "уступлю", "сегодня продам")):
        reasons.append("продавец пишет о срочности")
    first_seen = st.get("first_seen_at")
    if first_seen:
        days = (time.time() - float(first_seen)) / 86400.0
        if days >= 14:
            reasons.append(f"в продаже уже {int(days)} дней")
    if item.get("_savings_pct", 0) and float(item.get("_savings_pct") or 0) > 0:
        reasons.append("цена ниже похожих вариантов")
    return reasons
