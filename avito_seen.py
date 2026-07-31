"""Хранилище увиденных объявлений Avito (dedup + sent tracking)."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_DB_PATH = Path(os.environ.get("AVITO_SEEN_DB_PATH", "data/avito_seen.db"))
_LOCK = threading.Lock()
_INIT_DONE = False
_MEMORY_CACHE: set[str] = set()


def _ensure_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS avito_seen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            avito_id TEXT UNIQUE,
            url TEXT UNIQUE,
            title TEXT,
            price TEXT,
            city TEXT,
            images TEXT,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            sent INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_avito_seen_id ON avito_seen(avito_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_avito_seen_url ON avito_seen(url)"
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


def _load_recent_cache(days: int = 7) -> set[str]:
    """Загружает avito_id и url за последние N дней в память."""
    global _MEMORY_CACHE
    _init_once()
    cutoff = _now() - days * 24 * 3600
    with _LOCK:
        conn = _ensure_db()
        try:
            cur = conn.execute(
                "SELECT avito_id, url FROM avito_seen WHERE last_seen > ?",
                (cutoff,),
            )
            _MEMORY_CACHE = {
                v
                for row in cur.fetchall()
                for v in row
                if v
            }
            return set(_MEMORY_CACHE)
        finally:
            conn.close()


def is_avito_seen(item: dict[str, Any], use_cache: bool = True) -> bool:
    """Проверяет, отправлялось ли объявление ранее."""
    avito_id = str(item.get("avito_id", "") or item.get("source_id", "") or item.get("_source_id", "")).strip()
    url = str(item.get("url", "")).strip()
    if not avito_id and not url:
        return False

    if use_cache and _MEMORY_CACHE:
        if avito_id and avito_id in _MEMORY_CACHE:
            return True
        if url and url in _MEMORY_CACHE:
            return True

    _init_once()
    with _LOCK:
        conn = _ensure_db()
        try:
            if avito_id:
                cur = conn.execute(
                    "SELECT 1 FROM avito_seen WHERE avito_id = ? LIMIT 1",
                    (avito_id,),
                )
                if cur.fetchone():
                    return True
            if url:
                cur = conn.execute(
                    "SELECT 1 FROM avito_seen WHERE url = ? LIMIT 1",
                    (url,),
                )
                if cur.fetchone():
                    return True
        finally:
            conn.close()
    return False


def mark_avito_seen(item: dict[str, Any], sent: bool = False) -> None:
    """Отмечает объявление как увиденное. При sent=True — как отправленное."""
    avito_id = str(item.get("avito_id", "") or item.get("source_id", "") or item.get("_source_id", "")).strip()
    url = str(item.get("url", "")).strip()
    if not avito_id and not url:
        return

    title = str(item.get("title", ""))[:200]
    price = str(item.get("price", ""))[:32]
    city = str(item.get("city", "") or item.get("location", ""))[:64]
    images = ",".join(str(u) for u in (item.get("images") or [])[:5])[:500]
    now = _now()
    sent_flag = 1 if sent else 0

    _init_once()
    with _LOCK:
        conn = _ensure_db()
        try:
            conn.execute(
                """
                INSERT INTO avito_seen (avito_id, url, title, price, city, images, first_seen, last_seen, sent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(avito_id) DO UPDATE SET
                    url=excluded.url,
                    title=excluded.title,
                    price=excluded.price,
                    city=excluded.city,
                    images=excluded.images,
                    last_seen=excluded.last_seen,
                    sent=MAX(avito_seen.sent, excluded.sent)
                """,
                (avito_id, url, title, price, city, images, now, now, sent_flag),
            )
            conn.commit()
        finally:
            conn.close()

    if avito_id:
        _MEMORY_CACHE.add(avito_id)
    if url:
        _MEMORY_CACHE.add(url)


def get_avito_seen_stats() -> dict[str, Any]:
    """Возвращает статистику по seen-базе."""
    _init_once()
    with _LOCK:
        conn = _ensure_db()
        try:
            total = conn.execute("SELECT COUNT(*) FROM avito_seen").fetchone()[0]
            sent = conn.execute(
                "SELECT COUNT(*) FROM avito_seen WHERE sent = 1"
            ).fetchone()[0]
            recent = conn.execute(
                "SELECT COUNT(*) FROM avito_seen WHERE last_seen > ?",
                (_now() - 7 * 24 * 3600,),
            ).fetchone()[0]
            return {"total": total, "sent": sent, "recent_7d": recent}
        finally:
            conn.close()


def cleanup_old_avito_seen(days: int = 30) -> int:
    """Удаляет записи старше N дней."""
    cutoff = _now() - days * 24 * 3600
    _init_once()
    with _LOCK:
        conn = _ensure_db()
        try:
            cur = conn.execute(
                "DELETE FROM avito_seen WHERE last_seen < ?",
                (cutoff,),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
