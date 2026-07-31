"""История объявлений Avito: отслеживание появления, изменения цены, повторов."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_DB_PATH = Path(os.environ.get("AVITO_HISTORY_DB_PATH", "data/avito_history.db"))
_LOCK = threading.Lock()
_INIT_DONE = False


def _ensure_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS avito_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            avito_id TEXT UNIQUE,
            title TEXT,
            brand TEXT,
            model TEXT,
            year INTEGER,
            price INTEGER,
            city TEXT,
            seller_phone TEXT,
            seller_name TEXT,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            price_history TEXT,
            status TEXT DEFAULT 'active',
            url TEXT,
            images TEXT,
            description TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_avito_history_id ON avito_history(avito_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_avito_history_status ON avito_history(status)"
    )
    conn.commit()
    return conn


def _init_once() -> None:
    global _INIT_DONE
    if _INIT_DONE:
        return
    with _LOCK:
        if _INIT_DONE:
            return
        conn = _ensure_db()
        conn.close()
        _INIT_DONE = True


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _price_history(old: str | None, new_price: int) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    if old:
        try:
            history = json.loads(old)
        except Exception:
            history = []
    if not isinstance(history, list):
        history = []
    if history and history[-1].get("price") == new_price:
        return history
    history.append({"price": new_price, "ts": _now()})
    return history[-20:]  # храним последние 20 записей


def save_avito_history(item: dict[str, Any]) -> dict[str, Any]:
    """Сохраняет или обновляет объявление в истории. Возвращает статус записи."""
    avito_id = str(item.get("avito_id") or item.get("id") or "").strip()
    if not avito_id:
        return {"status": "no_id", "price_changed": False}

    title = str(item.get("title", ""))[:200]
    brand = str(item.get("brand", ""))[:64]
    model = str(item.get("model", ""))[:64]
    year = int(item.get("year") or 0) or None
    price = int(item.get("price") or 0) or 0
    city = str(item.get("city", "") or item.get("location", ""))[:64]
    phone = str(item.get("phone") or item.get("seller_phone") or "")[:32]
    seller = str(item.get("seller") or item.get("seller_name") or "")[:100]
    url = str(item.get("url", ""))[:500]
    images = json.dumps(item.get("images") or [], ensure_ascii=False)[:1000]
    description = str(item.get("description", ""))[:1000]
    now = _now()

    _init_once()
    with _LOCK:
        conn = _ensure_db()
        try:
            cur = conn.execute(
                "SELECT price, price_history, status FROM avito_history WHERE avito_id = ?",
                (avito_id,),
            )
            row = cur.fetchone()

            if row is None:
                # Новое объявление
                history = [{"price": price, "ts": now}]
                conn.execute(
                    """
                    INSERT INTO avito_history
                    (avito_id, title, brand, model, year, price, city, seller_phone,
                     seller_name, first_seen, last_seen, price_history, status, url, images, description)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        avito_id, title, brand, model, year, price, city, phone, seller,
                        now, now, json.dumps(history, ensure_ascii=False),
                        "active", url, images, description,
                    ),
                )
                conn.commit()
                return {
                    "status": "new",
                    "price_changed": False,
                    "old_price": None,
                    "new_price": price,
                }

            old_price, old_history, old_status = row
            price_changed = old_price != price
            new_history = _price_history(old_history, price)
            status = "active" if old_status != "removed" else "relisted"
            conn.execute(
                """
                UPDATE avito_history
                SET title=?, brand=?, model=?, year=?, price=?, city=?,
                    seller_phone=?, seller_name=?, last_seen=?, price_history=?,
                    status=?, url=?, images=?, description=?
                WHERE avito_id=?
                """,
                (
                    title, brand, model, year, price, city, phone, seller,
                    now, json.dumps(new_history, ensure_ascii=False),
                    status, url, images, description, avito_id,
                ),
            )
            conn.commit()
            return {
                "status": "updated" if not price_changed else "price_changed",
                "price_changed": price_changed,
                "old_price": old_price,
                "new_price": price,
            }
        finally:
            conn.close()


def get_avito_history(avito_id: str) -> dict[str, Any] | None:
    """Возвращает историю одного объявления."""
    if not avito_id:
        return None
    _init_once()
    with _LOCK:
        conn = _ensure_db()
        try:
            cur = conn.execute(
                "SELECT * FROM avito_history WHERE avito_id = ?",
                (avito_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            columns = [d[0] for d in cur.description]
            record = dict(zip(columns, row))
            try:
                record["price_history"] = json.loads(record.get("price_history") or "[]")
            except Exception:
                record["price_history"] = []
            return record
        finally:
            conn.close()


def cleanup_avito_history(days: int = 90) -> int:
    """Удаляет записи старше N дней, кроме имеющих историю цен."""
    cutoff = _now() - days * 24 * 3600
    _init_once()
    with _LOCK:
        conn = _ensure_db()
        try:
            cur = conn.execute(
                """
                DELETE FROM avito_history
                WHERE last_seen < ? AND (price_history IS NULL OR price_history = '[]')
                """,
                (cutoff,),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def get_avito_history_stats() -> dict[str, Any]:
    """Статистика истории."""
    _init_once()
    with _LOCK:
        conn = _ensure_db()
        try:
            total = conn.execute("SELECT COUNT(*) FROM avito_history").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM avito_history WHERE status = 'active'"
            ).fetchone()[0]
            today = conn.execute(
                "SELECT COUNT(*) FROM avito_history WHERE first_seen > ?",
                (_now() - 24 * 3600,),
            ).fetchone()[0]
            price_changed = conn.execute(
                "SELECT COUNT(*) FROM avito_history WHERE price_history LIKE '%price%'"
            ).fetchone()[0]
            return {
                "total": total,
                "active": active,
                "today": today,
                "price_changed": price_changed,
            }
        finally:
            conn.close()
