"""Единый нормализатор объявлений Avito из всех источников."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from listing_quality import parse_price


def _digits_only(s: Any) -> int | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        val = int(s)
        return val if val > 0 else None
    text = str(s).strip().lower()
    text = text.replace("\xa0", " ")
    digits = re.sub(r"\D", "", text)
    if digits:
        return int(digits)
    return None


def _extract_year(text: str) -> int | None:
    m = re.search(r"\b(19[5-9]\d|20[0-3]\d)\b", text)
    return int(m.group(1)) if m else None


def _extract_mileage(text: str) -> int | None:
    m = re.search(r"(\d{1,3}(?:\s?\d{3})*)\s*(?:км|тыс\.?\s*км?)", text, re.IGNORECASE)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        if digits:
            val = int(digits)
            if "тыс" in text.lower() and val < 1000:
                val *= 1000
            return val
    return None


def _normalize_images(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [u.strip() for u in value.split(",") if u.strip().startswith("http")]
    if isinstance(value, list):
        return [
            u.strip()
            for u in value
            if isinstance(u, str) and u.strip().startswith("http")
        ]
    return []


def _to_iso_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Пытаемся распарсить
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except Exception:
        return text


def normalize_avito_item(raw: dict[str, Any], source: str = "unknown") -> dict[str, Any]:
    """Приводит объявление из любого источника к единому формату."""
    title = str(raw.get("title") or raw.get("name") or "").strip()

    price = _digits_only(raw.get("price")) or _digits_only(raw.get("_price_int"))
    if price is None and raw.get("price_str"):
        price = parse_price(str(raw.get("price_str")))

    city = str(
        raw.get("city") or raw.get("location") or raw.get("address") or ""
    ).strip()
    region = str(raw.get("region") or "").strip()

    description = str(raw.get("description") or "").strip()
    seller = str(raw.get("seller") or raw.get("seller_name") or raw.get("name") or "").strip()
    seller_type = str(raw.get("seller_type") or "private").strip().lower()
    phone = str(raw.get("phone") or raw.get("seller_phone") or "").strip()
    images = _normalize_images(raw.get("images") or raw.get("image") or raw.get("photo_urls"))
    url = str(raw.get("url") or "").strip() or None
    date_iso = _to_iso_date(raw.get("date") or raw.get("published_at") or raw.get("time"))

    year = raw.get("year") or _extract_year(title) or _extract_year(description)
    mileage = _digits_only(raw.get("mileage")) or _extract_mileage(title) or _extract_mileage(description)
    brand = str(raw.get("brand") or raw.get("marka") or "").strip()
    model = str(raw.get("model") or "").strip()
    avito_id = str(raw.get("id") or raw.get("source_id") or raw.get("_source_id") or raw.get("avito_id") or "").strip()

    return {
        "id": avito_id,
        "avito_id": avito_id,
        "source_id": avito_id,
        "source": source,
        "title": title,
        "price": price or 0,
        "price_str": f"{price:,} ₽".replace(",", " ") if price else "цена не указана",
        "city": city,
        "region": region,
        "description": description,
        "phone": phone,
        "seller": seller,
        "seller_type": seller_type,
        "seller_url": str(raw.get("seller_url") or "").strip() or None,
        "images": images,
        "image_url": images[0] if images else "",
        "url": url,
        "date": date_iso,
        "date_str": str(date_iso or "сегодня")[:10] if date_iso else "сегодня",
        "year": int(year) if year else None,
        "mileage": int(mileage) if mileage else None,
        "brand": brand,
        "model": model,
        "specs": raw.get("specs") or {},
        "raw": raw,
    }


def normalize_rest_app_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Нормализует список объявлений из REST-App."""
    return [normalize_avito_item(it, source="rest_app") for it in items]


def normalize_playwright_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Нормализует список объявлений из Playwright."""
    return [normalize_avito_item(it, source="playwright") for it in items]
