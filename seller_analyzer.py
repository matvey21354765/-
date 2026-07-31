"""Анализ продавца Avito: частник vs перекуп."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from avito_normalizer import normalize_avito_item


_DB_PATH = Path(os.environ.get("AVITO_SELLER_DB_PATH", "data/avito_sellers.db"))
_LOCK = threading.Lock()
_INIT_DONE = False


def _ensure_db() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS avito_sellers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_key TEXT UNIQUE,
            seller_name TEXT,
            seller_type TEXT,
            listing_count INTEGER DEFAULT 0,
            phone TEXT,
            cities TEXT,
            first_seen REAL,
            last_seen REAL,
            samples TEXT
        )
        """
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


def _seller_key(item: dict[str, Any]) -> str:
    phone = _normalize_phone(item.get("phone")).strip()
    if phone:
        return f"phone:{phone}"
    seller = str(item.get("seller") or item.get("seller_name") or "").strip()
    if seller:
        return f"name:{seller.lower()[:64]}"
    url = str(item.get("seller_url") or "").strip()
    if url:
        return f"url:{url[:120]}"
    return ""


def _normalize_phone(raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def analyze_seller(item: dict[str, Any], history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Анализирует продавца по текущему объявлению и истории.

    Возвращает:
        {
            "type": "private" | "dealer" | "unknown",
            "type_label": "ЧАСТНИК" | "ПЕРЕКУП/ДИЛЕР" | "НЕИЗВЕСТНО",
            "confidence": 0..100,
            "reasons": [str],
            "listing_count": int,
            "cities": list[str],
            "phones": list[str],
        }
    """
    normalized = normalize_avito_item(item)
    seller_name = normalized["seller"]
    seller_type = normalized["seller_type"]
    phone = _normalize_phone(normalized["phone"])
    city = normalized["city"]
    title = normalized["title"]
    description = normalized["description"]
    text = f"{title}\n{description}".lower()

    reasons: list[str] = []
    listing_count = 0
    cities: set[str] = set()
    phones: set[str] = set()

    if phone:
        phones.add(phone)
    if city:
        cities.add(city)

    # Собираем по истории
    if history:
        for hist in history:
            listing_count += 1
            hist_city = hist.get("city")
            if hist_city:
                cities.add(hist_city)
            hist_phone = _normalize_phone(hist.get("seller_phone") or hist.get("phone"))
            if hist_phone:
                phones.add(hist_phone)

    dealer_signals = 0
    private_signals = 0

    if seller_type in ("dealer", "company", "organization"):
        dealer_signals += 3
        reasons.append("тип продавца: компания/дилер")

    dealer_phrases = ("автосалон", "официальный дилер", "дилер", "trade-in", "трейд-ин", "перекуп", "компания")
    if any(p in seller_name.lower() or p in text for p in dealer_phrases):
        dealer_signals += 2
        reasons.append("найдены дилерские/перекупские признаки")

    if listing_count >= 5:
        dealer_signals += 3
        reasons.append(f"много объявлений: {listing_count}")
    elif listing_count >= 2:
        dealer_signals += 1
        reasons.append("несколько объявлений у одного продавца")

    if len(cities) >= 2:
        dealer_signals += 2
        reasons.append("объявления в разных городах")

    if len(phones) >= 2:
        dealer_signals += 2
        reasons.append("разные телефоны у одного продавца")

    private_phrases = ("собственник", "один хозяин", "владелец", "срочно продам", "лично")
    if any(p in text for p in private_phrases):
        private_signals += 2
        reasons.append("признаки частного владельца")

    if listing_count == 0 and seller_type in ("private", "person"):
        private_signals += 2
        reasons.append("тип продавца: частное лицо")

    if seller_name and len(re.findall(r"[a-zA-Zа-яА-Я0-9]", seller_name)) <= 3:
        private_signals += 1
        reasons.append("короткое имя — вероятно, частник")

    if dealer_signals >= 4:
        confidence = min(95, 70 + dealer_signals * 5)
        result_type = "dealer"
        label = "ПЕРЕКУП/ДИЛЕР"
    elif private_signals >= 2 and dealer_signals < 2:
        confidence = min(95, 60 + private_signals * 10)
        result_type = "private"
        label = "ЧАСТНИК"
    else:
        confidence = 50
        result_type = "unknown"
        label = "НЕИЗВЕСТНО"

    return {
        "type": result_type,
        "type_label": label,
        "confidence": confidence,
        "reasons": reasons,
        "listing_count": listing_count,
        "cities": sorted(cities),
        "phones": sorted(phones),
    }


def format_seller_analysis(analysis: dict[str, Any]) -> str:
    """Форматирует анализ продавца для Telegram."""
    label = analysis.get("type_label", "НЕИЗВЕСТНО")
    confidence = analysis.get("confidence", 0)
    reasons = analysis.get("reasons", [])
    lines = [f"👤 Продавец: {label} ({confidence}%)", "Причины:"]
    for reason in reasons[:4]:
        lines.append(f"• {reason}")
    return "\n".join(lines)


def save_seller_observation(item: dict[str, Any]) -> None:
    """Сохраняет наблюдение о продавце в локальную базу."""
    key = _seller_key(item)
    if not key:
        return

    normalized = normalize_avito_item(item)
    seller_name = normalized["seller"] or "unknown"
    seller_type = normalized["seller_type"]
    phone = _normalize_phone(normalized["phone"])
    city = normalized["city"]

    _init_once()
    with _LOCK:
        conn = _ensure_db()
        try:
            cur = conn.execute(
                "SELECT listing_count, cities, samples FROM avito_sellers WHERE seller_key = ?",
                (key,),
            )
            row = cur.fetchone()
            now = _now()
            sample = {
                "title": normalized["title"][:80],
                "city": city,
                "price": normalized["price"],
                "ts": now,
            }
            if row is None:
                conn.execute(
                    """
                    INSERT INTO avito_sellers
                    (seller_key, seller_name, seller_type, listing_count, phone, cities, first_seen, last_seen, samples)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key, seller_name, seller_type, 1, phone, city,
                        now, now, json.dumps([sample], ensure_ascii=False),
                    ),
                )
            else:
                count, old_cities, old_samples = row
                cities_set = set((old_cities or "").split(",")) if old_cities else set()
                if city:
                    cities_set.add(city)
                samples = []
                if old_samples:
                    try:
                        samples = json.loads(old_samples)
                    except Exception:
                        samples = []
                if not isinstance(samples, list):
                    samples = []
                samples.append(sample)
                samples = samples[-20:]
                conn.execute(
                    """
                    UPDATE avito_sellers
                    SET listing_count = ?, seller_name = ?, seller_type = ?, phone = ?,
                        cities = ?, last_seen = ?, samples = ?
                    WHERE seller_key = ?
                    """,
                    (
                        count + 1, seller_name, seller_type, phone,
                        ",".join(sorted(cities_set)), now,
                        json.dumps(samples, ensure_ascii=False), key,
                    ),
                )
            conn.commit()
        finally:
            conn.close()


def get_seller_history(key: str) -> list[dict[str, Any]]:
    """Возвращает последние наблюдения по продавцу."""
    if not key:
        return []
    _init_once()
    with _LOCK:
        conn = _ensure_db()
        try:
            cur = conn.execute(
                "SELECT samples FROM avito_sellers WHERE seller_key = ?",
                (key,),
            )
            row = cur.fetchone()
            if not row:
                return []
            try:
                return json.loads(row[0] or "[]")
            except Exception:
                return []
        finally:
            conn.close()
