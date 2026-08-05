"""
Реферальная система PerekupDrive.

Архитектура:
- PostgreSQL — основное production-хранилище (DATABASE_URL).
- SQLite — fallback для локальных тестов и разработки (DATABASE_URL=sqlite://... или отсутствие DATABASE_URL).
- Источником истины для платежей и начислений служит таблица referral_events.

Интеграция:
- control_bot.py импортирует referrals и вызывает init_referrals_db() на старте.
- /start вызывает attach_referrer() для новых пользователей по ref_USER_ID.
- Создание счёта (cmd_yoomoney) вызывает create_invoice() для фиксации цены.
- Webhook (_yoomoney_webhook) вызывает get_invoice(), _activate_subscription() и record_payment().
- record_payment() возвращает награду рефереру; control_bot продлевает подписку и шлёт уведомления.
"""
import contextlib
import datetime
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Optional

# ──────────────────────────────────────────────────────────────────────
# Выбор бэкенда
# ──────────────────────────────────────────────────────────────────────
_DATA_DIR = Path("data")
_DATA_DIR.mkdir(parents=True, exist_ok=True)

def select_database_backend(database_url: str) -> tuple[str, str, Optional[str]]:
    """Возвращает (backend, normalized_dsn, sqlite_path), не смешивая драйверы."""
    raw = (database_url or "").strip()
    low = raw.lower()
    if not raw:
        path = str(_DATA_DIR / "referrals.db")
        return "sqlite", f"sqlite:///{path}", path
    if low.startswith(("sqlite+aiosqlite://", "sqlite://")):
        normalized = re.sub(
            r"^sqlite\+aiosqlite://", "sqlite://", raw, count=1, flags=re.I
        )
        if normalized == "sqlite:///:memory:":
            path = ":memory:"
        elif normalized.startswith("sqlite:////"):
            path = normalized[len("sqlite:///"):]  # сохраняем ведущий / abs path
        elif normalized.startswith("sqlite:///"):
            path = normalized[len("sqlite:///"):]  # относительный файл
        else:
            path = normalized[len("sqlite://"):] or str(_DATA_DIR / "referrals.db")
        return "sqlite", normalized, path
    if low.startswith(("postgresql://", "postgres://", "postgresql+asyncpg://")):
        normalized = re.sub(
            r"^postgresql\+asyncpg://", "postgresql://", raw, count=1, flags=re.I
        )
        if normalized.lower().startswith("postgres://"):
            normalized = "postgresql://" + normalized[len("postgres://"):]
        return "postgresql", normalized, None
    raise ValueError(
        "DATABASE_URL должен начинаться с sqlite:// или postgresql://"
    )


_DB_BACKEND, _DB_URL, _DB_SQLITE_PATH = select_database_backend(
    os.getenv("DATABASE_URL", "")
)
_DB_IS_SQLITE = _DB_BACKEND == "sqlite"

# Импортируем драйверы по необходимости
_pg = None
if not _DB_IS_SQLITE:
    try:
        import psycopg2 as _pg
    except Exception as exc:
        print(
            f"[referrals] PostgreSQL driver unavailable: {type(exc).__name__}; "
            "включён SQLite fallback"
        )
        _pg = None
        _DB_IS_SQLITE = True
        _DB_SQLITE_PATH = str(_DATA_DIR / "referrals.db")
        _DB_BACKEND = "sqlite"

import sqlite3 as _sqlite


# ──────────────────────────────────────────────────────────────────────
# Константы бизнес-логики
# ──────────────────────────────────────────────────────────────────────
REFERRAL_DISCOUNT_RATE = 0.10

# Итоговые цены фиксированы, чтобы избежать ошибок округления.
REFERRAL_PRICES = {
    "week": {"original": 349, "discount": 35, "final": 314},
    "month": {"original": 999, "discount": 100, "final": 899},
}

BASE_REWARD_DAYS = {
    "week": 2,
    "month": 4,
}

MILESTONE_EVERY = 5
MILESTONE_REWARD_DAYS = 5

EVENT_TYPES = {
    "referral_registered",
    "first_payment",
    "base_reward",
    "milestone_reward",
    "reward_reversed",
    "suspicious_activity",
}


# ──────────────────────────────────────────────────────────────────────
# Подключение и транзакции
# ──────────────────────────────────────────────────────────────────────
def _get_conn():
    """Возвращает новое соединение с выбранной БД."""
    global _DB_IS_SQLITE, _DB_BACKEND, _DB_SQLITE_PATH
    if _DB_IS_SQLITE:
        conn = _sqlite.connect(_DB_SQLITE_PATH, timeout=20, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=20000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    if _pg is None:
        raise RuntimeError("PostgreSQL недоступен: psycopg2 не установлен")
    try:
        conn = _pg.connect(_DB_URL, connect_timeout=5)
        conn.autocommit = True
        return conn
    except Exception as exc:
        # Только после реальной и явно залогированной ошибки соединения.
        print(
            f"[referrals] PostgreSQL connection failed: {type(exc).__name__}: "
            f"{str(exc)[:160]}; включён SQLite fallback"
        )
        _DB_IS_SQLITE = True
        _DB_BACKEND = "sqlite"
        _DB_SQLITE_PATH = str(_DATA_DIR / "referrals.db")
        return _get_conn()


@contextlib.contextmanager
def _transaction():
    """Контекст транзакции с автоматическим commit/rollback и закрытием."""
    conn = _get_conn()
    try:
        if _DB_IS_SQLITE:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        else:
            old_autocommit = conn.autocommit
            conn.autocommit = False
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = old_autocommit
    finally:
        conn.close()


@contextlib.contextmanager
def _read_conn():
    """Соединение только для чтения (закрывается после)."""
    conn = _get_conn()
    try:
        yield conn
    finally:
        conn.close()


def _ph(n: int) -> str:
    """Placeholder-строка для текущего бэкенда."""
    if _DB_IS_SQLITE:
        return ", ".join(["?"] * n)
    return ", ".join(["%s"] * n)


def _lock_user(cur, uid: int) -> None:
    """Блокировка строки пользователя (PG FOR UPDATE / SQLite просто SELECT)."""
    if _DB_IS_SQLITE:
        cur.execute(f"SELECT uid FROM bot_users WHERE uid = {_ph(1)}", (uid,))
    else:
        cur.execute(f"SELECT uid FROM bot_users WHERE uid = {_ph(1)} FOR UPDATE", (uid,))


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _metadata_value(meta: dict) -> Any:
    """Адаптирует metadata для текущего бэкенда."""
    s = json.dumps(meta or {}, ensure_ascii=False)
    if _DB_IS_SQLITE:
        return s
    # Для PostgreSQL JSONB можно передать строку — произойдёт неявный cast.
    return s


# ──────────────────────────────────────────────────────────────────────
# Миграции
# ──────────────────────────────────────────────────────────────────────
def _add_column(conn, table: str, column: str, type_sql: str) -> None:
    """Безопасно добавляет колонку (если её ещё нет)."""
    cur = conn.cursor()
    if _DB_IS_SQLITE:
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_sql}")
        except Exception:
            pass
    else:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {type_sql}")


def _init_schema_sqlite(conn) -> None:
    cur = conn.cursor()
    # Базовая таблица пользователей (должна совпадать с control_bot._get_db)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            uid BIGINT PRIMARY KEY,
            username TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            search_count INTEGER DEFAULT 0,
            monitoring BOOLEAN DEFAULT FALSE
        )
    """)
    # Реферальные колонки
    _add_column(conn, "bot_users", "referred_by", "BIGINT")
    _add_column(conn, "bot_users", "referral_created_at", "TIMESTAMP")
    _add_column(conn, "bot_users", "referral_discount_used", "BOOLEAN DEFAULT FALSE")
    _add_column(conn, "bot_users", "first_paid_at", "TIMESTAMP")
    _add_column(conn, "bot_users", "paid_referrals_count", "INTEGER DEFAULT 0")
    _add_column(conn, "bot_users", "referral_reward_days_total", "INTEGER DEFAULT 0")
    _add_column(conn, "bot_users", "referral_milestones_count", "INTEGER DEFAULT 0")

    # События реферальной системы (источник истины)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referral_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id BIGINT NOT NULL,
            referred_user_id BIGINT NOT NULL,
            event_type TEXT NOT NULL,
            payment_id TEXT,
            tariff_code TEXT,
            original_amount INTEGER,
            paid_amount INTEGER,
            discount_amount INTEGER,
            base_reward_days INTEGER DEFAULT 0,
            milestone_reward_days INTEGER DEFAULT 0,
            total_reward_days INTEGER DEFAULT 0,
            milestone_number INTEGER,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP,
            reversed_at TIMESTAMP,
            metadata TEXT,
            CHECK (referrer_id != referred_user_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_events_referrer ON referral_events(referrer_id, event_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_events_referred ON referral_events(referred_user_id, event_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_events_payment ON referral_events(payment_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_events_status ON referral_events(status)")
    # Частичные unique-индексы для критических ограничений
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_events_one_referrer
        ON referral_events(referred_user_id) WHERE event_type = 'referral_registered'
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_events_one_first_payment
        ON referral_events(referred_user_id) WHERE event_type = 'first_payment'
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_events_unique_payment
        ON referral_events(payment_id) WHERE payment_id IS NOT NULL AND event_type = 'first_payment'
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_events_unique_base_reward
        ON referral_events(referred_user_id) WHERE event_type = 'base_reward'
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_events_unique_milestone
        ON referral_events(referrer_id, milestone_number) WHERE event_type = 'milestone_reward'
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_events_unique_reversed
        ON referral_events(payment_id) WHERE event_type = 'reward_reversed'
    """)

    # Счета для сверки сумм при оплате
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referral_invoices (
            label TEXT PRIMARY KEY,
            uid BIGINT NOT NULL,
            plan_key TEXT NOT NULL,
            original_amount INTEGER NOT NULL,
            discount_amount INTEGER NOT NULL,
            final_amount INTEGER NOT NULL,
            applied_discount_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            paid BOOLEAN DEFAULT FALSE
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_invoices_uid ON referral_invoices(uid)")
    conn.commit()


def _init_schema_pg(conn) -> None:
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            uid BIGINT PRIMARY KEY,
            username TEXT,
            first_seen TIMESTAMPTZ DEFAULT NOW(),
            last_seen TIMESTAMPTZ DEFAULT NOW(),
            search_count INTEGER DEFAULT 0,
            monitoring BOOLEAN DEFAULT FALSE
        )
    """)
    _add_column(conn, "bot_users", "referred_by", "BIGINT")
    _add_column(conn, "bot_users", "referral_created_at", "TIMESTAMPTZ")
    _add_column(conn, "bot_users", "referral_discount_used", "BOOLEAN DEFAULT FALSE")
    _add_column(conn, "bot_users", "first_paid_at", "TIMESTAMPTZ")
    _add_column(conn, "bot_users", "paid_referrals_count", "INTEGER DEFAULT 0")
    _add_column(conn, "bot_users", "referral_reward_days_total", "INTEGER DEFAULT 0")
    _add_column(conn, "bot_users", "referral_milestones_count", "INTEGER DEFAULT 0")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS referral_events (
            id SERIAL PRIMARY KEY,
            referrer_id BIGINT NOT NULL,
            referred_user_id BIGINT NOT NULL,
            event_type TEXT NOT NULL,
            payment_id TEXT,
            tariff_code TEXT,
            original_amount INTEGER,
            paid_amount INTEGER,
            discount_amount INTEGER,
            base_reward_days INTEGER DEFAULT 0,
            milestone_reward_days INTEGER DEFAULT 0,
            total_reward_days INTEGER DEFAULT 0,
            milestone_number INTEGER,
            status TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            processed_at TIMESTAMPTZ,
            reversed_at TIMESTAMPTZ,
            metadata JSONB,
            CHECK (referrer_id != referred_user_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_events_referrer ON referral_events(referrer_id, event_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_events_referred ON referral_events(referred_user_id, event_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_events_payment ON referral_events(payment_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_events_status ON referral_events(status)")
    # Частичные unique-индексы
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_events_one_referrer
        ON referral_events(referred_user_id) WHERE event_type = 'referral_registered'
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_events_one_first_payment
        ON referral_events(referred_user_id) WHERE event_type = 'first_payment'
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_events_unique_payment
        ON referral_events(payment_id) WHERE payment_id IS NOT NULL AND event_type = 'first_payment'
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_events_unique_base_reward
        ON referral_events(referred_user_id) WHERE event_type = 'base_reward'
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_events_unique_milestone
        ON referral_events(referrer_id, milestone_number) WHERE event_type = 'milestone_reward'
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_events_unique_reversed
        ON referral_events(payment_id) WHERE event_type = 'reward_reversed'
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS referral_invoices (
            label TEXT PRIMARY KEY,
            uid BIGINT NOT NULL,
            plan_key TEXT NOT NULL,
            original_amount INTEGER NOT NULL,
            discount_amount INTEGER NOT NULL,
            final_amount INTEGER NOT NULL,
            applied_discount_type TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            paid BOOLEAN DEFAULT FALSE
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_invoices_uid ON referral_invoices(uid)")
    conn.commit()


def _migrate_legacy_json() -> None:
    """Переносит старую реферальную JSON-структуру в PostgreSQL/SQLite (best-effort)."""
    legacy_file = Path("data/referrals.json")
    if not legacy_file.exists():
        return
    try:
        data = json.loads(legacy_file.read_text(encoding="utf-8"))
    except Exception:
        return

    with _transaction() as conn:
        cur = conn.cursor()
        # Проверяем, были ли уже мигрированы события
        cur.execute(f"SELECT COUNT(*) FROM referral_events WHERE event_type = 'referral_registered'")
        if cur.fetchone()[0] > 0:
            # Есть уже новые данные — не затираем
            return

        now = _now()
        for new_key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            try:
                new_uid = int(new_key)
            except Exception:
                continue
            inviter = entry.get("inviter")
            if inviter is None:
                continue
            try:
                inviter_uid = int(inviter)
            except Exception:
                continue
            if inviter_uid == new_uid:
                continue

            # Закрепляем реферера
            cur.execute(f"""
                INSERT INTO bot_users (uid, referred_by, referral_created_at)
                VALUES ({_ph(3)})
                ON CONFLICT(uid) DO UPDATE SET
                    referred_by = EXCLUDED.referred_by,
                    referral_created_at = EXCLUDED.referral_created_at
                WHERE bot_users.referred_by IS NULL
            """, (new_uid, now, inviter_uid))

            # Событие регистрации
            try:
                if _DB_IS_SQLITE:
                    cur.execute(f"""
                        INSERT OR IGNORE INTO referral_events
                        (referrer_id, referred_user_id, event_type, status, created_at, metadata)
                        VALUES ({_ph(6)})
                    """, (inviter_uid, new_uid, "referral_registered", "completed", now,
                          json.dumps({"source": "legacy_migration"}, ensure_ascii=False)))
                else:
                    cur.execute(f"""
                        INSERT INTO referral_events
                        (referrer_id, referred_user_id, event_type, status, created_at, metadata)
                        VALUES ({_ph(6)})
                        ON CONFLICT DO NOTHING
                    """, (inviter_uid, new_uid, "referral_registered", "completed", now,
                          json.dumps({"source": "legacy_migration"}, ensure_ascii=False)))
            except Exception:
                pass

            # Подсчёт оплаченных рефералов из старой структуры
            paid_history = entry.get("paid_history", {})
            if not isinstance(paid_history, dict):
                paid_history = {}
            paid_uids = set(paid_history.keys())
            bonus_days = int(entry.get("bonus_days", 0))

            # Обновляем агрегированные счётчики инвайтера
            cur.execute(f"""
                INSERT INTO bot_users (uid, paid_referrals_count, referral_reward_days_total)
                VALUES ({_ph(3)})
                ON CONFLICT(uid) DO UPDATE SET
                    paid_referrals_count = bot_users.paid_referrals_count + EXCLUDED.paid_referrals_count,
                    referral_reward_days_total = bot_users.referral_reward_days_total + EXCLUDED.referral_reward_days_total
            """, (inviter_uid, len(paid_uids), bonus_days))

    # Переименовываем legacy-файл, чтобы не мигрировать повторно
    try:
        legacy_file.rename(legacy_file.with_suffix(".json.migrated"))
    except Exception:
        pass


def init_referrals_db() -> None:
    """Создаёт/обновляет таблицы и индексы реферальной системы."""
    conn = _get_conn()
    try:
        if _DB_IS_SQLITE:
            _init_schema_sqlite(conn)
        else:
            _init_schema_pg(conn)
    finally:
        conn.close()
    _migrate_legacy_json()
    print(f"  [referrals] БД инициализирована ({'SQLite' if _DB_IS_SQLITE else 'PostgreSQL'})")


# ──────────────────────────────────────────────────────────────────────
# Базовые helpers
# ──────────────────────────────────────────────────────────────────────
def _ensure_user(conn, uid: int) -> None:
    """Создаёт минимальную запись пользователя, если она отсутствует."""
    cur = conn.cursor()
    if _DB_IS_SQLITE:
        cur.execute(f"""
            INSERT OR IGNORE INTO bot_users (uid, first_seen, last_seen)
            VALUES ({_ph(3)})
        """, (uid, _now(), _now()))
    else:
        cur.execute(f"""
            INSERT INTO bot_users (uid, first_seen, last_seen)
            VALUES ({_ph(3)})
            ON CONFLICT(uid) DO NOTHING
        """, (uid, _now(), _now()))


def _row_to_dict(row, columns) -> dict:
    return {columns[i]: row[i] for i in range(len(columns))}


def _get_bot_user(uid: int) -> Optional[dict]:
    with _read_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT uid, referred_by, referral_created_at, referral_discount_used,
                   first_paid_at, paid_referrals_count, referral_reward_days_total,
                   referral_milestones_count
            FROM bot_users WHERE uid = {_ph(1)}
        """, (uid,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [desc[0] for desc in cur.description]
        return _row_to_dict(row, cols)


# ──────────────────────────────────────────────────────────────────────
# Привязка реферера
# ──────────────────────────────────────────────────────────────────────
def attach_referrer(new_uid: int, inviter_uid: int, source: str = "ref") -> dict:
    """
    Закрепляет реферера за новым пользователем при /start ref_USER_ID.
    Условия: инвайтер существует, самореферал запрещён, referred_by ещё не установлен.
    """
    if not isinstance(new_uid, int) or not isinstance(inviter_uid, int):
        return {"ok": False, "reason": "invalid_params"}
    if new_uid == inviter_uid:
        _log_suspicious(new_uid, inviter_uid, "self_referral", {"source": source})
        return {"ok": False, "reason": "self_referral"}

    with _transaction() as conn:
        cur = conn.cursor()
        _ensure_user(conn, inviter_uid)
        _ensure_user(conn, new_uid)

        # Проверяем существование инвайтера
        _lock_user(cur, inviter_uid)
        cur.execute(f"SELECT uid FROM bot_users WHERE uid = {_ph(1)}", (inviter_uid,))
        if not cur.fetchone():
            return {"ok": False, "reason": "inviter_not_found"}

        # Проверяем, не привязан ли уже
        _lock_user(cur, new_uid)
        cur.execute(f"SELECT referred_by FROM bot_users WHERE uid = {_ph(1)}", (new_uid,))
        row = cur.fetchone()
        if row and row[0]:
            existing = row[0]
            if int(existing) != inviter_uid:
                _log_suspicious(new_uid, inviter_uid, "rebind_attempt", {
                    "existing_referrer": int(existing), "source": source,
                })
            return {"ok": False, "reason": "already_referred", "existing_referrer": int(existing)}

        now = _now()
        # Устанавливаем связь
        cur.execute(f"""
            UPDATE bot_users SET referred_by = {_ph(1)}, referral_created_at = {_ph(1)}
            WHERE uid = {_ph(1)}
        """, (inviter_uid, now, new_uid))

        # Событие регистрации (idempotent)
        if _DB_IS_SQLITE:
            cur.execute(f"""
                INSERT OR IGNORE INTO referral_events
                (referrer_id, referred_user_id, event_type, status, created_at, metadata)
                VALUES ({_ph(6)})
            """, (inviter_uid, new_uid, "referral_registered", "completed", now,
                  json.dumps({"source": source}, ensure_ascii=False)))
        else:
            cur.execute(f"""
                INSERT INTO referral_events
                (referrer_id, referred_user_id, event_type, status, created_at, metadata)
                VALUES ({_ph(6)})
                ON CONFLICT DO NOTHING
            """, (inviter_uid, new_uid, "referral_registered", "completed", now,
                  json.dumps({"source": source}, ensure_ascii=False)))

    return {"ok": True, "reason": "attached", "inviter_uid": inviter_uid}


# ──────────────────────────────────────────────────────────────────────
# Скидка приглашённому
# ──────────────────────────────────────────────────────────────────────
def get_discount_for_user(uid: int, plan_key: str) -> dict:
    """
    Возвращает ценовые параметры для первой платной подписки приглашённого.
    Скидка применяется только если есть referred_by, скидка ещё не использована и first_paid_at пуст.
    """
    if plan_key not in REFERRAL_PRICES:
        return {"original": 0, "discount": 0, "final": 0, "discount_type": None}

    info = _get_bot_user(uid)
    if info and info.get("referred_by") and not info.get("referral_discount_used") and not info.get("first_paid_at"):
        return {
            "original": REFERRAL_PRICES[plan_key]["original"],
            "discount": REFERRAL_PRICES[plan_key]["discount"],
            "final": REFERRAL_PRICES[plan_key]["final"],
            "discount_type": "referral",
        }
    return {
        "original": REFERRAL_PRICES[plan_key]["original"],
        "discount": 0,
        "final": REFERRAL_PRICES[plan_key]["original"],
        "discount_type": None,
    }


# ──────────────────────────────────────────────────────────────────────
# Счета для оплаты
# ──────────────────────────────────────────────────────────────────────
def create_invoice(uid: int, plan_key: str, label: str,
                   price: Optional[dict] = None) -> dict:
    """Создаёт счёт со скидкой (если применима). Возвращает суммы.

    price — готовые суммы от вызывающего кода. Нужен, чтобы в счёт попала
    ровно та цена, которую пользователь видел на кнопке: иначе оплата уходит
    на одну сумму, а сверка в webhook ждёт другую и платёж не засчитывается.
    """
    if price is None:
        price = get_discount_for_user(uid, plan_key)
    else:
        price = {
            "original": int(price.get("original") or 0),
            "discount": int(price.get("discount") or 0),
            "final": int(price.get("final") or 0),
            "discount_type": price.get("discount_type"),
        }
    with _transaction() as conn:
        cur = conn.cursor()
        if _DB_IS_SQLITE:
            cur.execute(f"""
                INSERT OR REPLACE INTO referral_invoices
                (label, uid, plan_key, original_amount, discount_amount, final_amount, applied_discount_type, created_at)
                VALUES ({_ph(8)})
            """, (label, uid, plan_key, price["original"], price["discount"], price["final"], price["discount_type"], _now()))
        else:
            cur.execute(f"""
                INSERT INTO referral_invoices
                (label, uid, plan_key, original_amount, discount_amount, final_amount, applied_discount_type, created_at)
                VALUES ({_ph(8)})
                ON CONFLICT(label) DO UPDATE SET
                    original_amount = EXCLUDED.original_amount,
                    discount_amount = EXCLUDED.discount_amount,
                    final_amount = EXCLUDED.final_amount,
                    applied_discount_type = EXCLUDED.applied_discount_type
            """, (label, uid, plan_key, price["original"], price["discount"], price["final"], price["discount_type"], _now()))
    return price


def get_invoice(label: str) -> Optional[dict]:
    """Возвращает счёт по label для сверки в webhook."""
    with _read_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM referral_invoices WHERE label = {_ph(1)}", (label,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [desc[0] for desc in cur.description]
        return _row_to_dict(row, cols)


def mark_invoice_paid(label: str) -> None:
    with _transaction() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            UPDATE referral_invoices SET paid = TRUE WHERE label = {_ph(1)}
        """, (label,))


# ──────────────────────────────────────────────────────────────────────
# Начисление награды рефереру
# ──────────────────────────────────────────────────────────────────────
def record_payment(
    uid: int,
    plan_key: str,
    payment_id: str,
    original_amount: int,
    paid_amount: int,
    discount_amount: int,
    applied_discount_type: Optional[str] = None,
) -> dict:
    """
    Регистрирует первую успешную оплату реферала и начисляет награду рефереру.
    Атомарная и идемпотентная операция.
    """
    if plan_key not in BASE_REWARD_DAYS:
        return {"ok": False, "reason": "unknown_tariff"}
    if not payment_id:
        return {"ok": False, "reason": "missing_payment_id"}

    base_reward = BASE_REWARD_DAYS[plan_key]

    with _transaction() as conn:
        cur = conn.cursor()
        _ensure_user(conn, uid)
        _lock_user(cur, uid)

        cur.execute(f"""
            SELECT referred_by, referral_discount_used, first_paid_at
            FROM bot_users WHERE uid = {_ph(1)}
        """, (uid,))
        row = cur.fetchone()
        if not row or not row[0]:
            return {"ok": False, "reason": "no_referrer"}
        referrer_id = int(row[0])

        # Бесплатная/промо/админская активация — не участвует
        if paid_amount == 0 or applied_discount_type == "promo" or payment_id.startswith("promo:") or payment_id.startswith("admin:"):
            return {"ok": False, "reason": "non_qualifying_payment"}

        # Первая оплата уже зафиксирована?
        cur.execute(f"""
            SELECT 1 FROM referral_events
            WHERE referred_user_id = {_ph(1)} AND event_type = 'first_payment'
            LIMIT 1
        """, (uid,))
        if cur.fetchone():
            return {"ok": False, "reason": "first_payment_already_recorded"}

        # payment_id уже использовался?
        cur.execute(f"""
            SELECT 1 FROM referral_events
            WHERE payment_id = {_ph(1)} AND event_type = 'first_payment'
            LIMIT 1
        """, (payment_id,))
        if cur.fetchone():
            return {"ok": False, "reason": "payment_id_already_used"}

        # Блокируем реферера для подсчёта
        _lock_user(cur, referrer_id)
        _ensure_user(conn, referrer_id)

        cur.execute(f"""
            SELECT COUNT(*) FROM referral_events
            WHERE referrer_id = {_ph(1)} AND event_type = 'first_payment' AND status = 'completed'
        """, (referrer_id,))
        paid_count_before = cur.fetchone()[0]
        new_paid_count = paid_count_before + 1

        milestone = (new_paid_count > 0) and (new_paid_count % MILESTONE_EVERY == 0)
        milestone_reward = MILESTONE_REWARD_DAYS if milestone else 0
        milestone_number = new_paid_count // MILESTONE_EVERY if milestone else None
        total_reward = base_reward + milestone_reward
        now = _now()

        # first_payment
        cur.execute(f"""
            INSERT INTO referral_events
            (referrer_id, referred_user_id, event_type, payment_id, tariff_code,
             original_amount, paid_amount, discount_amount, status, created_at, metadata)
            VALUES ({_ph(11)})
        """, (referrer_id, uid, "first_payment", payment_id, plan_key,
              original_amount, paid_amount, discount_amount, "completed", now,
              json.dumps({"applied_discount_type": applied_discount_type or "none"}, ensure_ascii=False)))

        # base_reward
        cur.execute(f"""
            INSERT INTO referral_events
            (referrer_id, referred_user_id, event_type, payment_id, tariff_code,
             base_reward_days, total_reward_days, status, created_at, processed_at)
            VALUES ({_ph(10)})
        """, (referrer_id, uid, "base_reward", payment_id, plan_key,
              base_reward, base_reward, "completed", now, now))

        # milestone_reward
        if milestone:
            cur.execute(f"""
                INSERT INTO referral_events
                (referrer_id, referred_user_id, event_type, payment_id, tariff_code,
                 milestone_reward_days, milestone_number, total_reward_days, status, created_at, processed_at)
                VALUES ({_ph(11)})
            """, (referrer_id, uid, "milestone_reward", payment_id, plan_key,
                  milestone_reward, milestone_number, milestone_reward, "completed", now, now))

        # Обновляем приглашённого: скидка использована, first_paid_at
        cur.execute(f"""
            UPDATE bot_users
            SET first_paid_at = {_ph(1)}, referral_discount_used = TRUE
            WHERE uid = {_ph(1)}
        """, (now, uid))

        # Обновляем реферера
        cur.execute(f"""
            UPDATE bot_users
            SET paid_referrals_count = paid_referrals_count + 1,
                referral_reward_days_total = referral_reward_days_total + {_ph(1)},
                referral_milestones_count = referral_milestones_count + {_ph(1)}
            WHERE uid = {_ph(1)}
        """, (total_reward, 1 if milestone else 0, referrer_id))

    return {
        "ok": True,
        "referrer_id": referrer_id,
        "referred_user_id": uid,
        "payment_id": payment_id,
        "tariff_code": plan_key,
        "base_reward_days": base_reward,
        "milestone_reward_days": milestone_reward,
        "total_reward_days": total_reward,
        "milestone": milestone,
        "milestone_number": milestone_number,
        "paid_count": new_paid_count,
    }


# ──────────────────────────────────────────────────────────────────────
# Отмена награды (возврат платежа)
# ──────────────────────────────────────────────────────────────────────
def reverse_payment(payment_id: str) -> dict:
    """Безопасно отменяет реферальную награду по платежу."""
    if not payment_id:
        return {"ok": False, "reason": "missing_payment_id"}

    with _transaction() as conn:
        cur = conn.cursor()

        # Проверяем, не отменена ли уже
        cur.execute(f"""
            SELECT 1 FROM referral_events
            WHERE payment_id = {_ph(1)} AND event_type = 'reward_reversed'
            LIMIT 1
        """, (payment_id,))
        if cur.fetchone():
            return {"ok": False, "reason": "already_reversed"}

        # Находим завершённые награды по этому платежу
        cur.execute(f"""
            SELECT id, referrer_id, referred_user_id, event_type,
                   base_reward_days, milestone_reward_days, total_reward_days, milestone_number
            FROM referral_events
            WHERE payment_id = {_ph(1)} AND event_type IN ('base_reward', 'milestone_reward')
                  AND status = 'completed'
        """, (payment_id,))
        rows = cur.fetchall()
        if not rows:
            return {"ok": False, "reason": "no_completed_rewards_for_payment"}

        total_days = 0
        milestone_count = 0
        referrer_id = None
        referred_user_id = None
        for row in rows:
            referrer_id = int(row[1])
            referred_user_id = int(row[2])
            total_days += int(row[6] or 0)
            if row[7]:
                milestone_count += 1

        # Помечаем оригинальные награды и first_payment как reversed
        now = _now()
        cur.execute(f"""
            UPDATE referral_events
            SET status = 'reversed', reversed_at = {_ph(1)}
            WHERE payment_id = {_ph(1)} AND event_type IN ('first_payment', 'base_reward', 'milestone_reward')
        """, (now, payment_id))

        # Создаём событие отмены
        if _DB_IS_SQLITE:
            cur.execute(f"""
                INSERT OR IGNORE INTO referral_events
                (referrer_id, referred_user_id, event_type, payment_id, total_reward_days, status, created_at, metadata)
                VALUES ({_ph(8)})
            """, (referrer_id, referred_user_id, "reward_reversed", payment_id, total_days, "completed", now,
                  json.dumps({"reversed_days": total_days}, ensure_ascii=False)))
        else:
            cur.execute(f"""
                INSERT INTO referral_events
                (referrer_id, referred_user_id, event_type, payment_id, total_reward_days, status, created_at, metadata)
                VALUES ({_ph(8)})
                ON CONFLICT DO NOTHING
            """, (referrer_id, referred_user_id, "reward_reversed", payment_id, total_days, "completed", now,
                  json.dumps({"reversed_days": total_days}, ensure_ascii=False)))

        # Корректируем агрегированные счётчики реферера
        if _DB_IS_SQLITE:
            cur.execute(f"""
                SELECT paid_referrals_count, referral_reward_days_total, referral_milestones_count
                FROM bot_users WHERE uid = {_ph(1)}
            """, (referrer_id,))
        else:
            cur.execute(f"""
                SELECT paid_referrals_count, referral_reward_days_total, referral_milestones_count
                FROM bot_users WHERE uid = {_ph(1)} FOR UPDATE
            """, (referrer_id,))
        row = cur.fetchone()
        if row:
            new_paid = max(0, int(row[0] or 0) - 1)
            new_days = max(0, int(row[1] or 0) - total_days)
            new_mile = max(0, int(row[2] or 0) - milestone_count)
            cur.execute(f"""
                UPDATE bot_users
                SET paid_referrals_count = {_ph(1)},
                    referral_reward_days_total = {_ph(1)},
                    referral_milestones_count = {_ph(1)}
                WHERE uid = {_ph(1)}
            """, (new_paid, new_days, new_mile, referrer_id))

    return {
        "ok": True,
        "referrer_id": referrer_id,
        "referred_user_id": referred_user_id,
        "payment_id": payment_id,
        "total_days_reversed": total_days,
    }


# ──────────────────────────────────────────────────────────────────────
# Статистика и UI
# ──────────────────────────────────────────────────────────────────────
def get_referral_status(referrer_id: int) -> dict:
    """Агрегированная статистика реферера."""
    with _read_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT COUNT(*) FROM referral_events
            WHERE referrer_id = {_ph(1)} AND event_type = 'referral_registered' AND status = 'completed'
        """, (referrer_id,))
        registered_count = cur.fetchone()[0]

        cur.execute(f"""
            SELECT COUNT(*) FROM referral_events
            WHERE referrer_id = {_ph(1)} AND event_type = 'first_payment' AND status = 'completed'
        """, (referrer_id,))
        paid_count = cur.fetchone()[0]

        cur.execute(f"""
            SELECT COALESCE(SUM(total_reward_days), 0) FROM referral_events
            WHERE referrer_id = {_ph(1)} AND event_type IN ('base_reward', 'milestone_reward')
                  AND status = 'completed'
        """, (referrer_id,))
        reward_days_total = int(cur.fetchone()[0] or 0)

    cycle = paid_count % MILESTONE_EVERY
    current_cycle_count = cycle
    remaining_to_milestone = MILESTONE_EVERY - cycle if cycle != 0 else MILESTONE_EVERY
    next_milestone = paid_count + remaining_to_milestone

    return {
        "registered_count": registered_count,
        "paid_count": paid_count,
        "reward_days_total": reward_days_total,
        "current_cycle_count": current_cycle_count,
        "remaining_to_milestone": remaining_to_milestone,
        "next_milestone": next_milestone,
    }


def get_referral_link(uid: int, bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{uid}"


def format_invite_screen(uid: int, bot_username: str) -> dict:
    """Возвращает текст и клавиатуру для экрана реферальной программы."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    status = get_referral_status(uid)
    link = get_referral_link(uid, bot_username)
    share_text = (
        "🚗 Ищешь автомобиль?\n\n"
        "PerekupDrive помогает находить свежие объявления по заданным параметрам.\n\n"
        f"По моей ссылке ты получишь скидку 10% на первую подписку:\n{link}"
    )
    cycle = status["current_cycle_count"]
    progress_bar = "🟢" * cycle + "⚪" * (MILESTONE_EVERY - cycle)

    text = (
        "🚀 <b>Приглашай друзей и получай бесплатные дни</b>\n\n"
        "За первую оплату каждого друга:\n"
        "🎁 до 4 дней подписки\n\n"
        "• друг выбрал 7 дней — тебе +2 дня\n"
        "• друг выбрал 30 дней — тебе +4 дня\n\n"
        "🏆 Каждые 5 оплативших друзей — ещё +5 дней\n\n"
        "Друг получит скидку 10% на первую подписку:\n"
        "• 7 дней — 314 ₽ вместо 349 ₽\n"
        "• 30 дней — 899 ₽ вместо 999 ₽\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "📊 <b>Твой прогресс</b>\n\n"
        f"👥 Зарегистрировалось: <b>{status['registered_count']}</b>\n"
        f"💳 Впервые оплатило: <b>{status['paid_count']}</b>\n\n"
        f"{progress_bar} {cycle}/5\n\n"
        f"До дополнительных +5 дней: <b>{status['remaining_to_milestone']}</b>\n\n"
        f"🎁 Всего начислено: <b>{status['reward_days_total']}</b> дней\n\n"
        "━━━━━━━━━━━━━━\n\n"
        f"🔗 <b>Персональная ссылка:</b>\n<code>{link}</code>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться", url=f"https://t.me/share/url?url={link}&text={urllib.parse.quote(share_text)}")],
        [InlineKeyboardButton(text="📊 Мои приглашения", callback_data="ref_stats")],
        [InlineKeyboardButton(text="ℹ️ Условия", callback_data="ref_terms")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="ref_back")],
    ])
    return {"text": text, "keyboard": kb}


def format_terms_text() -> str:
    return (
        "ℹ️ <b>Условия реферальной программы</b>\n\n"
        "1. Ссылка закрепляется за новым пользователем при первой регистрации.\n\n"
        "2. Скидка 10% действует только на первую платную подписку.\n\n"
        "3. За первую оплату недельного тарифа начисляется 2 дня.\n\n"
        "4. За первую оплату месячного тарифа начисляется 4 дня.\n\n"
        "5. Каждые 5 впервые оплативших друзей дают ещё 5 дней.\n\n"
        "6. Повторные покупки одного друга не дают новые бонусы.\n\n"
        "7. Промокоды, бесплатные и тестовые активации не участвуют.\n\n"
        "8. При возврате платежа бонус может быть отменён."
    )


# ──────────────────────────────────────────────────────────────────────
# Уведомления (текст)
# ──────────────────────────────────────────────────────────────────────
def format_registration_notification(inviter_uid: int, registered_count: int) -> str:
    return (
        "🎉 <b>Новый пользователь зарегистрировался по твоей ссылке!</b>\n\n"
        "Когда он впервые оплатит подписку, ты получишь до 4 бонусных дней.\n\n"
        f"👥 Всего зарегистрировалось: <b>{registered_count}</b>"
    )


def format_payment_notification(
    inviter_uid: int,
    plan_key: str,
    base_reward_days: int,
    milestone_reward_days: int,
    total_reward_days: int,
    paid_count: int,
    subscription_end: str,
) -> str:
    remaining = MILESTONE_EVERY - (paid_count % MILESTONE_EVERY) if paid_count % MILESTONE_EVERY != 0 else MILESTONE_EVERY

    if plan_key == "week":
        header = "🎁 Друг впервые оплатил подписку на 7 дней!"
    elif plan_key == "month":
        header = "🎁 Друг впервые оплатил подписку на 30 дней!"
    else:
        header = "🎁 Друг впервые оплатил подписку!"

    lines = [
        f"{header}\n",
        f"Тебе начислено: <b>+{base_reward_days} дня</b>",
        f"💳 Впервые оплативших друзей: <b>{paid_count}</b>",
        f"🏆 До дополнительных +5 дней: <b>{remaining}</b>",
        f"\nНовая дата окончания подписки:\n<b>{subscription_end}</b>",
    ]
    if milestone_reward_days:
        lines.insert(2, f"🏆 <b>Цель достигнута!</b> Дополнительно +{milestone_reward_days} дней")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Административные функции
# ──────────────────────────────────────────────────────────────────────
def get_admin_status(uid: int) -> dict:
    """Детальная реферальная информация для /ref_status."""
    with _read_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT uid, referred_by, referral_created_at, referral_discount_used,
                   first_paid_at, paid_referrals_count, referral_reward_days_total,
                   referral_milestones_count
            FROM bot_users WHERE uid = {_ph(1)}
        """, (uid,))
        row = cur.fetchone()
        user_info = _row_to_dict(row, [desc[0] for desc in cur.description]) if row else {}

        cur.execute(f"""
            SELECT * FROM referral_events
            WHERE referrer_id = {_ph(1)} OR referred_user_id = {_ph(1)}
            ORDER BY created_at DESC
            LIMIT 20
        """, (uid, uid))
        cols = [desc[0] for desc in cur.description]
        events = [_row_to_dict(r, cols) for r in cur.fetchall()]

    return {"user": user_info, "events": events}


def recalculate_user_stats(referrer_id: int) -> dict:
    """Пересчитывает агрегированные счётчики из referral_events."""
    with _transaction() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT COUNT(*) FROM referral_events
            WHERE referrer_id = {_ph(1)} AND event_type = 'first_payment' AND status = 'completed'
        """, (referrer_id,))
        paid_count = cur.fetchone()[0]

        cur.execute(f"""
            SELECT COALESCE(SUM(base_reward_days), 0), COALESCE(SUM(milestone_reward_days), 0)
            FROM referral_events
            WHERE referrer_id = {_ph(1)} AND event_type IN ('base_reward', 'milestone_reward')
                  AND status = 'completed'
        """, (referrer_id,))
        base_sum, milestone_sum = cur.fetchone()
        total_days = int(base_sum or 0) + int(milestone_sum or 0)

        cur.execute(f"""
            SELECT COUNT(*) FROM referral_events
            WHERE referrer_id = {_ph(1)} AND event_type = 'milestone_reward' AND status = 'completed'
        """, (referrer_id,))
        milestone_count = cur.fetchone()[0]

        cur.execute(f"""
            UPDATE bot_users
            SET paid_referrals_count = {_ph(1)},
                referral_reward_days_total = {_ph(1)},
                referral_milestones_count = {_ph(1)}
            WHERE uid = {_ph(1)}
        """, (paid_count, total_days, milestone_count, referrer_id))

    return {
        "ok": True,
        "referrer_id": referrer_id,
        "paid_count": paid_count,
        "reward_days_total": total_days,
        "milestone_count": milestone_count,
    }


def dry_run_payment(referred_uid: int, plan_key: str) -> dict:
    """Dry-run: показывает, какая скидка и награда были бы применены."""
    discount = get_discount_for_user(referred_uid, plan_key)
    with _read_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT referred_by FROM bot_users WHERE uid = {_ph(1)}", (referred_uid,))
        row = cur.fetchone()
        referrer_id = int(row[0]) if row and row[0] else None

    if not referrer_id:
        return {"ok": False, "reason": "no_referrer", "discount": discount}

    with _read_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT COUNT(*) FROM referral_events
            WHERE referrer_id = {_ph(1)} AND event_type = 'first_payment' AND status = 'completed'
        """, (referrer_id,))
        paid_count = cur.fetchone()[0]

    base = BASE_REWARD_DAYS.get(plan_key, 0)
    new_paid = paid_count + 1
    milestone = (new_paid > 0) and (new_paid % MILESTONE_EVERY == 0)
    milestone_reward = MILESTONE_REWARD_DAYS if milestone else 0

    return {
        "ok": True,
        "referrer_id": referrer_id,
        "discount": discount,
        "base_reward_days": base,
        "milestone_reward_days": milestone_reward,
        "total_reward_days": base + milestone_reward,
        "paid_count_after": new_paid,
    }


# ──────────────────────────────────────────────────────────────────────
# Антифрод логирование
# ──────────────────────────────────────────────────────────────────────
def _log_suspicious(referred_uid: int, referrer_uid: int, reason: str, meta: dict) -> None:
    """Логирует подозрительную активность в referral_events."""
    try:
        with _transaction() as conn:
            cur = conn.cursor()
            cur.execute(f"""
                INSERT INTO referral_events
                (referrer_id, referred_user_id, event_type, status, created_at, metadata)
                VALUES ({_ph(6)})
            """, (referrer_uid or 0, referred_uid or 0, "suspicious_activity", "logged", _now(),
                  json.dumps({"reason": reason, **meta}, ensure_ascii=False)))
    except Exception as e:
        print(f"  [referrals] не удалось залогировать suspicious_activity: {e}")
