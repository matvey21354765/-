"""Извлечение и нормализация объявлений Avito из HTML/JSON-LD/DOM."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from listing_quality import classify_car_listing, parse_price


_RE_DIGITS = re.compile(r"\D+")
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")
_MILEAGE_RE = re.compile(r"(\d{1,3}(?:\s?\d{3})*)\s*(?:км|тыс\.?\s*км?)", re.IGNORECASE)
_AVT_ID_RE = re.compile(r"_?(\d+)$")


def _extract_id_from_url(url: str) -> str:
    if not url:
        return ""
    m = _AVT_ID_RE.search(url.split("?")[0].rstrip("/"))
    return m.group(1) if m else ""


def _extract_id_from_json(raw: Any) -> str:
    if isinstance(raw, str):
        m = _AVT_ID_RE.search(raw)
        if m:
            return m.group(1)
    if isinstance(raw, dict):
        for key in ("@id", "sku", "identifier", "avito_id", "id", "itemId"):
            val = raw.get(key)
            if isinstance(val, str):
                m = _AVT_ID_RE.search(val)
                if m:
                    return m.group(1)
            if isinstance(val, (int, float)):
                return str(int(val))
    return ""


def _first_text(raw: Any, *keys: str) -> str:
    if isinstance(raw, dict):
        for key in keys:
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, dict):
                nested = val.get("name") or val.get("value") or val.get("text")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


def _extract_price_value(raw: Any) -> int | None:
    """Извлекает целочисленную цену из числа, строки или JSON-LD структуры."""
    if isinstance(raw, (int, float)):
        return int(raw) if raw > 0 else None
    if isinstance(raw, str):
        cleaned = _RE_DIGITS.sub("", raw)
        if cleaned:
            return int(cleaned)
    if isinstance(raw, dict):
        for key in ("price", "value", "minPrice", "maxPrice"):
            val = raw.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return int(val)
            if isinstance(val, str):
                cleaned = _RE_DIGITS.sub("", val)
                if cleaned:
                    return int(cleaned)
        offers = raw.get("offers") or raw.get("offer")
        if isinstance(offers, dict):
            return _extract_price_value(offers)
        if isinstance(offers, list) and offers:
            return _extract_price_value(offers[0])
    return None


def _extract_images(raw: Any) -> list[str]:
    """Извлекает список URL фото из JSON-LD или DOM-структуры."""
    urls: list[str] = []
    if isinstance(raw, str):
        url = raw.strip()
        if url.startswith("http"):
            urls.append(url)
    elif isinstance(raw, list):
        for el in raw:
            urls.extend(_extract_images(el))
    elif isinstance(raw, dict):
        for key in ("image", "images", "photo", "photos", "photoUrls", "imageUrl"):
            val = raw.get(key)
            if val is not None:
                urls.extend(_extract_images(val))
    return urls


def _extract_location(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        for key in ("addressLocality", "addressRegion", "name", "locality"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        address = raw.get("address")
        if isinstance(address, dict):
            return _extract_location(address)
    return ""


def _extract_seller(raw: Any) -> dict[str, str]:
    """Возвращает {name, url, type} продавца."""
    result = {"name": "", "url": "", "type": "private"}
    if isinstance(raw, dict):
        for key in ("name", "seller_name", "shop_name", "company"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                result["name"] = val.strip()
                break
        url = raw.get("url") or raw.get("seller_url") or raw.get("link")
        if isinstance(url, str) and url.startswith("http"):
            result["url"] = url
        if raw.get("@type") == "Organization":
            result["type"] = "dealer"
    return result


def _parse_avito_date(text: str | None) -> date | None:
    if not text:
        return None
    text = text.strip().lower()
    if text == "сегодня":
        return date.today()
    if text == "вчера":
        return date.today() - timedelta(days=1)
    patterns = (
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d %m %Y",
    )
    for pat in patterns:
        try:
            return datetime.strptime(text, pat).date()
        except Exception:
            continue
    # ISO с временем
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        pass
    return None


def _extract_year(text: str, raw: dict) -> int | None:
    vehicle_date = raw.get("vehicleModelDate") or raw.get("productionDate") or raw.get("dateVehicleFirstRegistration")
    if vehicle_date:
        m = _YEAR_RE.search(str(vehicle_date))
        if m:
            return int(m.group(1))
    m = _YEAR_RE.search(text)
    if m:
        return int(m.group(1))
    return None


def _extract_mileage(text: str, raw: dict) -> int | None:
    raw_mileage = raw.get("mileageFromOdometer") or raw.get("mileage")
    if isinstance(raw_mileage, dict):
        val = raw_mileage.get("value") or raw_mileage.get("mileage")
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str):
            cleaned = _RE_DIGITS.sub("", val)
            if cleaned:
                return int(cleaned)
    if isinstance(raw_mileage, (int, float)) and raw_mileage > 0:
        return int(raw_mileage)
    if isinstance(raw_mileage, str):
        cleaned = _RE_DIGITS.sub("", raw_mileage)
        if cleaned:
            return int(cleaned)
    m = _MILEAGE_RE.search(text)
    if m:
        cleaned = _RE_DIGITS.sub("", m.group(1))
        if cleaned:
            val = int(cleaned)
            # Если написано "120 тыс км" — домножаем
            if "тыс" in text.lower() and val < 1000:
                val *= 1000
            return val
    return None


def _normalize_jsonld_item(raw: dict, base_url: str) -> dict[str, Any] | None:
    """Превращает JSON-LD Vehicle/Offer в единый item."""
    item_type = raw.get("@type", "")
    if isinstance(item_type, list):
        item_type = " ".join(item_type)
    item_type = str(item_type).lower()
    if "vehicle" not in item_type and "offer" not in item_type and "product" not in item_type:
        return None

    title = _first_text(raw, "name", "model", "title", "headline", "brand", "description")
    if not title:
        return None

    url = raw.get("url") or ""
    if isinstance(url, dict):
        url = url.get("url") or ""
    if not url:
        url = _extract_id_from_json(raw)
        if url:
            url = f"https://www.avito.ru/item/{url}"
    if url and not url.startswith("http"):
        url = urljoin(base_url, url)

    avito_id = _extract_id_from_json(raw) or _extract_id_from_url(url)
    price_val = _extract_price_value(raw.get("offers") or raw)
    price_str = f"{price_val:,} ₽".replace(",", " ") if price_val else ""
    year = _extract_year(title, raw)
    mileage = _extract_mileage(title, raw)
    location = _extract_location(raw.get("availableAtOrFrom") or raw.get("location") or raw.get("address"))
    images = _extract_images(raw.get("image") or raw.get("images"))
    seller = _extract_seller(raw.get("seller") or raw.get("merchant") or raw.get("seller_info") or {})
    description = _first_text(raw, "description", "vehicleSeatingCapacity", "bodyType")
    published = raw.get("datePublished") or raw.get("uploadDate")
    published_date = _parse_avito_date(str(published)) if published else None

    return {
        "source": "avito",
        "avito_id": avito_id,
        "source_id": avito_id,
        "_source_id": avito_id,
        "title": title,
        "price": price_str,
        "_price_int": price_val or 0,
        "price_val": price_val,
        "year": year,
        "_year": year,
        "mileage": mileage,
        "city": location,
        "location": location,
        "description": description,
        "seller": seller["name"],
        "seller_url": seller["url"],
        "seller_type": seller["type"],
        "images": images,
        "image_url": images[0] if images else "",
        "_photo_url": images[0] if images else "",
        "_photos": len(images),
        "url": url,
        "published_at": published,
        "date": str(published_date or date.today())[:10],
        "_days_on_site": max(0, (date.today() - (published_date or date.today())).days),
    }


def _normalize_dom_item(raw: dict, base_url: str) -> dict[str, Any] | None:
    """Превращает DOM-элемент (data-marker=item) в единый item."""
    url = raw.get("url", "")
    if url and not url.startswith("http"):
        url = urljoin(base_url, url)
    if not url:
        return None

    title = str(raw.get("title", "")).strip()
    if not title:
        return None

    avito_id = _extract_id_from_url(url)
    price_str = str(raw.get("price", "")).strip()
    price_val = parse_price(price_str) or 0
    if price_val == 0 and price_str:
        cleaned = _RE_DIGITS.sub("", price_str)
        if cleaned:
            price_val = int(cleaned)
    year = _extract_year(title, raw)
    mileage = _extract_mileage(title, raw)
    location = str(raw.get("location", "")).strip() or ""
    images = _extract_images(raw.get("image_url") or raw.get("image"))
    seller = _extract_seller(raw.get("seller") or {})

    return {
        "source": "avito",
        "avito_id": avito_id,
        "source_id": avito_id,
        "_source_id": avito_id,
        "title": title,
        "price": f"{price_val:,} ₽".replace(",", " ") if price_val else price_str,
        "_price_int": price_val,
        "price_val": price_val,
        "year": year,
        "_year": year,
        "mileage": mileage,
        "city": location,
        "location": location,
        "description": str(raw.get("description", "")).strip(),
        "seller": seller["name"],
        "seller_url": seller["url"],
        "seller_type": seller["type"],
        "images": images,
        "image_url": images[0] if images else "",
        "_photo_url": images[0] if images else "",
        "_photos": len(images),
        "url": url,
        "published_at": None,
        "date": str(date.today())[:10],
        "_days_on_site": 0,
    }


def _extract_jsonld_items(html: str, base_url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        scripts = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
    except Exception:
        return items
    for script in scripts:
        try:
            data = json.loads(script)
        except Exception:
            continue
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            normalized = _normalize_jsonld_item(entry, base_url)
            if normalized:
                items.append(normalized)
    return items


def _extract_dom_items_from_html(html: str, base_url: str) -> list[dict[str, Any]]:
    """Fallback-извлечение через регулярные выражения по HTML."""
    items: list[dict[str, Any]] = []
    # Ищем блоки item
    item_blocks = re.findall(
        r'<div[^>]*data-marker="item"[^>]*>(.*?)</div>\s*</div>\s*</div>',
        html, re.DOTALL | re.IGNORECASE
    )
    if not item_blocks:
        # более мягкий вариант: ищем data-marker=item целиком
        item_blocks = re.findall(
            r'data-marker="item"[^>]*>(.*?)(?=data-marker="item"|</body>|$)',
            html, re.DOTALL | re.IGNORECASE
        )
    for block in item_blocks:
        url_match = re.search(r'<a[^>]*href="([^"]+)"[^>]*data-marker="item-title"', block)
        if not url_match:
            url_match = re.search(r'<a[^>]*href="([^"]+)"[^>]*itemprop="url"', block)
        if not url_match:
            url_match = re.search(r'<a[^>]*href="(/[^"]+/avtomobili/[^"]+)"', block)
        if not url_match:
            continue
        url = url_match.group(1)
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = urljoin(base_url, url)

        title_match = re.search(r'data-marker="item-title"[^>]*>([^<]+)', block)
        if not title_match:
            title_match = re.search(r'<h3[^>]*>([^<]+)', block)
        title = title_match.group(1).strip() if title_match else ""

        price_match = re.search(r'data-marker="item-price"[^>]*>([^<]+)', block)
        price = price_match.group(1).strip() if price_match else ""

        img_match = re.search(r'<img[^>]*src="([^"]+)"[^>]*data-marker="item-image"', block)
        if not img_match:
            img_match = re.search(r'<img[^>]*data-src="([^"]+)"', block)
        if not img_match:
            img_match = re.search(r'<img[^>]*src="([^"]+)"', block)
        image_url = img_match.group(1) if img_match else ""

        if not title or not url:
            continue

        items.append({
            "url": url,
            "title": title,
            "price": price,
            "image_url": image_url,
            "location": "",
            "description": "",
            "seller": {},
        })
    return [_normalize_dom_item(it, base_url) for it in items if it]


def extract_avito_items(html: str, page_url: str = "https://www.avito.ru/") -> list[dict[str, Any]]:
    """Извлекает объявления Avito из HTML страницы.

    Порядок:
    1. JSON-LD Vehicle/Offer.
    2. DOM-структуры data-marker=item (если переданы в виде словарей).
    3. Регулярный fallback по HTML.
    """
    items = _extract_jsonld_items(html, page_url)
    if not items:
        items = _extract_dom_items_from_html(html, page_url)

    # Дедупликация по avito_id/url
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for it in items:
        key = it.get("avito_id") or it.get("url", "")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(it)
    return unique


def is_avito_car_listing(item: dict) -> dict:
    """Классифицирует объявление как автомобильное с помощью listing_quality."""
    text = " ".join([
        str(item.get("title", "")),
        str(item.get("description", "")),
        str(item.get("city", "")),
    ]).lower()
    # Подставляем price_val если есть
    check_item = {
        "title": item.get("title", ""),
        "description": item.get("description", ""),
        "price": item.get("price_val") or item.get("_price_int") or item.get("price", ""),
        "category": "автомобили",
    }
    result = classify_car_listing(check_item)
    # Дополнительно: объявление с маркой и годом почти всегда авто
    if result["score"] >= 4 or ("brand" in result["reasons"] and "year" in result["reasons"]):
        result["accepted"] = True
    return result


def avito_item_url(avito_id: str) -> str:
    if not avito_id:
        return ""
    return f"https://www.avito.ru/item/{avito_id}"
