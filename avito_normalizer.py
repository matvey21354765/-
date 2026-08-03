"""Strict, observable normalization for Avito provider payloads."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any


def parse_price(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 0 else None
    digits = re.sub(r"\D", "", str(value).replace("\xa0", " "))
    return int(digits) if digits and int(digits) > 0 else None


def _extract_year(text: str) -> int | None:
    match = re.search(r"\b(19[5-9]\d|20[0-3]\d)\b", text)
    return int(match.group(1)) if match else None


def parse_year(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        year = int(value)
        return year if 1950 <= year <= 2039 else None
    return _extract_year(str(value or ""))


def parse_mileage(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        mileage = int(value)
        return mileage if mileage >= 0 else None
    digits = re.sub(r"\D", "", str(value or ""))
    return int(digits) if digits else None


def _extract_mileage(text: str) -> int | None:
    match = re.search(r"(\d{1,3}(?:\s?\d{3})*)\s*(?:км|тыс\.?\s*км?)", text, re.I)
    if not match:
        return None
    value = int(re.sub(r"\D", "", match.group(1)))
    return value * 1000 if "тыс" in text.casefold() and value < 1000 else value


def normalize_images(value: Any) -> list[str]:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return [str(url).strip() for url in values if str(url).strip().startswith("http")]


def _to_iso_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return text or None


def normalize_location(raw: dict[str, Any]) -> dict[str, str]:
    nested = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    city = str(raw.get("city") or nested.get("city") or nested.get("name") or "").strip()
    region = str(raw.get("region") or nested.get("region") or "").strip()
    district = str(raw.get("district") or nested.get("district") or "").strip()
    address = str(raw.get("address") or nested.get("address") or "").strip()
    direct = str(raw.get("location") or "").strip() if isinstance(raw.get("location"), str) else ""
    location = ", ".join(dict.fromkeys(
        part for part in (region, city, district, address, direct) if part
    ))
    return {
        "city": city,
        "region": region,
        "location": location,
        "city_id": str(raw.get("city_id") or nested.get("city_id") or ""),
        "region_id": str(raw.get("region_id") or nested.get("region_id") or ""),
    }


def normalize_avito_item(raw: dict[str, Any], source: str = "unknown") -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TypeError("item_is_not_object")
    title = str(raw.get("title") or raw.get("name") or "").strip()
    price = parse_price(raw.get("price")) or parse_price(raw.get("price_value")) or parse_price(raw.get("cost")) or parse_price(raw.get("_price_int")) or parse_price(raw.get("price_str"))
    raw_url = str(raw.get("url") or raw.get("link") or "").strip()
    raw_url_lower = raw_url.casefold()
    demo_url_hidden = (
        raw_url_lower == "hidden_in_demo"
        or raw_url_lower.startswith("http://crwl.ru")
        or raw_url_lower.startswith("https://crwl.ru")
        or raw_url_lower.startswith("http://www.crwl.ru")
        or raw_url_lower.startswith("https://www.crwl.ru")
    )
    demo_mode = demo_url_hidden or str(raw.get("avito_id") or "").casefold() == "hidden_in_demo"
    url = None if demo_url_hidden else (raw_url or None)
    identity = str(raw.get("id") or raw.get("Id") or raw.get("item_id") or raw.get("source_id") or raw.get("_source_id") or raw.get("avito_id") or "").strip()
    missing = []
    if not identity and not url:
        missing.append("id_or_url")
    if not title:
        missing.append("title")
    if price is None:
        missing.append("price")
    if missing and source in {"avito", "rest_app"}:
        raise ValueError("missing_required:" + ",".join(missing))
    location = normalize_location(raw)
    description = str(raw.get("description") or raw.get("text") or "").strip()
    images = normalize_images(raw.get("images") or raw.get("photos") or raw.get("image") or raw.get("photo_urls"))
    params = raw.get("params") if isinstance(raw.get("params"), list) else []
    param_text = " ".join(
        f"{p.get('name', '')} {p.get('value', '')}" for p in params if isinstance(p, dict)
    )
    year = parse_year(raw.get("year")) or _extract_year(param_text) or _extract_year(title) or _extract_year(description)
    mileage = parse_mileage(raw.get("mileage")) or _extract_mileage(param_text) or _extract_mileage(description)
    published = _to_iso_date(raw.get("time") or raw.get("date") or raw.get("created_at") or raw.get("published_at"))
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    seller = str(raw.get("seller") or raw.get("seller_name") or user.get("name") or "").strip()
    return {
        "id": identity or url or "",
        "avito_id": identity,
        "source_id": identity or url or "",
        "source": source,
        "title": title,
        "price": price,
        "price_str": f"{price:,} ₽".replace(",", " ") if price else "цена не указана",
        **location,
        "description": description,
        "phone": str(raw.get("phone") or "").strip(),
        "seller": seller,
        "seller_type": str(raw.get("seller_type") or "private").strip().lower(),
        "images": images,
        "image": images[0] if images else None,
        "image_url": images[0] if images else "",
        "url": url,
        "published_at": published,
        "date": published,
        "year": int(year) if year else None,
        "mileage": int(mileage) if mileage else None,
        "marka": str(raw.get("marka") or raw.get("brand") or "").strip(),
        "brand": str(raw.get("brand") or raw.get("marka") or "").strip(),
        "model": str(raw.get("model") or "").strip(),
        "params": params,
        "specs": raw.get("specs") if isinstance(raw.get("specs"), dict) else {},
        "demo_url_hidden": demo_url_hidden,
        "demo_mode": demo_mode,
        "demo_price_unreliable": demo_mode,
        "raw": raw,
    }


def normalize_rest_app_items_with_diagnostics(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    total_failed = 0
    for index, item in enumerate(items):
        try:
            normalized.append(normalize_avito_item(item, source="avito"))
        except (TypeError, ValueError, KeyError) as exc:
            total_failed += 1
            text = str(exc)
            missing = text.partition("missing_required:")[2].split(",") if "missing_required:" in text else []
            if len(failures) < 5:
                failure = {"index": index, "reason": text, "missing_fields": [x for x in missing if x], "item_keys": sorted(item) if isinstance(item, dict) else []}
                failures.append(failure)
                logging.getLogger(__name__).error(
                    "[AVITO NORMALIZE ERROR] index=%d reason=%s missing_fields=%s item_keys=%s",
                    index, text, failure["missing_fields"], failure["item_keys"],
                )
    diagnostics = {"normalized_count": len(normalized), "failed_count": total_failed, "failures": failures}
    logging.getLogger(__name__).info(
        "[AVITO NORMALIZER] normalized_count=%d failed_count=%d", len(normalized), total_failed
    )
    return normalized, diagnostics


def normalize_rest_app_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return normalize_rest_app_items_with_diagnostics(items)[0]


def normalize_playwright_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_avito_item(item, source="playwright") for item in items]
