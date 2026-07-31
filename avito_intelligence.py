"""Интеллектуальная оценка объявлений Avito."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from listing_quality import parse_price


# Минимальные/максимальные пороги для score
_MIN_REASONABLE_PRICE = 10_000
_MAX_REASONABLE_PRICE = 99_000_000

_JUNK_PHRASES = (
    "срочно даром",
    "обмен",
    "разбор",
    "запчасти",
    "на запчасти",
    "разборка",
    "по запчастям",
    "ремонт",
    "кузовной ремонт",
    "покраска",
    "под заказ",
    "привезём",
    "привезем",
    "аукцион",
    "растамож",
    "доставка автомобиля",
)

_PRIVATE_SELLER_HINTS = (
    "собственник",
    "один хозяин",
    "владелец",
    "хозяин",
    "частное лицо",
)

_DEALER_HINTS = (
    "автосалон",
    "официальный дилер",
    "дилерский центр",
    " trade-in ",
    "трейд-ин",
    "кредит",
    "рассрочка",
)


_JUNK_RE = re.compile(
    "|".join(re.escape(p) for p in _JUNK_PHRASES),
    re.IGNORECASE,
)
_PRIVATE_SELLER_RE = re.compile(
    "|".join(re.escape(p) for p in _PRIVATE_SELLER_HINTS),
    re.IGNORECASE,
)
_DEALER_RE = re.compile(
    "|".join(re.escape(p) for p in _DEALER_HINTS),
    re.IGNORECASE,
)


def _price_to_int(price: Any) -> int | None:
    if isinstance(price, (int, float)):
        val = int(price)
        return val if val > 0 else None
    if isinstance(price, str):
        val = parse_price(price)
        return val if val and val > 0 else None
    return None


def score_avito_deal(item: dict[str, Any], market_price: int | None = None) -> dict[str, Any]:
    """Возвращает оценку сделки 0-100 и объяснение.

    Учитывает:
    - цену ниже рынка,
    - наличие фото,
    - свежесть объявления,
    - тип продавца (частник лучше),
    - отсутствие мусорных фраз.
    """
    score = 0
    reasons: list[str] = []
    flags: list[str] = []

    price = _price_to_int(item.get("price_val") or item.get("_price_int") or item.get("price"))
    title = str(item.get("title", "")).lower()
    description = str(item.get("description", "")).lower()
    text = f"{title}\n{description}"
    images = item.get("images") or []
    image_url = item.get("image_url") or item.get("_photo_url") or ""
    has_photos = bool(images) or bool(image_url)
    seller_type = str(item.get("seller_type", "private")).lower()
    seller_name = str(item.get("seller", "")).lower()
    days_on_site = int(item.get("_days_on_site", 0) or 0)

    # Цена ниже рынка
    if price and market_price and market_price > 0 and price < market_price:
        diff = market_price - price
        pct = min(diff / market_price, 0.5)
        score += int(pct * 60)
        reasons.append(f"ниже рынка на ~{diff:,} ₽".replace(",", " "))
        flags.append("below_market")
    elif price and market_price and market_price > 0:
        reasons.append("цена около рынка")
    else:
        reasons.append("нет данных о рынке")

    # Фото
    if has_photos:
        score += 10
        reasons.append("есть фото")
    else:
        flags.append("no_photos")
        reasons.append("нет фото")

    # Свежесть
    if days_on_site <= 1:
        score += 10
        reasons.append("свежее объявление")
    elif days_on_site <= 3:
        score += 5
        reasons.append("объявление 2-3 дн. назад")
    else:
        reasons.append(f"опубликовано {days_on_site} дн. назад")

    # Продавец
    is_private = seller_type in ("private", "person") or bool(_PRIVATE_SELLER_RE.search(text))
    is_dealer = seller_type in ("dealer", "organization") or bool(_DEALER_RE.search(text)) or bool(_DEALER_RE.search(seller_name))
    if is_private and not is_dealer:
        score += 10
        reasons.append("частник")
        flags.append("private_seller")
    elif is_dealer:
        reasons.append("дилер")
    else:
        reasons.append("продавец не определён")

    # Хороший текст: пробег и год
    has_year = bool(re.search(r"\b(19\d{2}|20\d{2})\b", title))
    has_mileage = bool(re.search(r"\d{1,3}(?:\s?\d{3})*\s*км", title, re.IGNORECASE))
    if has_year:
        score += 3
        reasons.append("указан год")
    if has_mileage:
        score += 3
        reasons.append("указан пробег")

    # Мусор
    if _JUNK_RE.search(text):
        score -= 25
        reasons.append("подозрительные фразы в тексте")
        flags.append("junk_phrases")

    # Нереальная цена
    if price is not None and (price < _MIN_REASONABLE_PRICE or price > _MAX_REASONABLE_PRICE):
        score -= 15
        reasons.append("недостоверная цена")
        flags.append("suspicious_price")

    score = max(0, min(100, score))

    return {
        "score": score,
        "reasons": reasons,
        "flags": flags,
        "deal_good": score >= 70,
        "market_price": market_price,
        "price": price,
        "has_photos": has_photos,
        "private_seller": is_private and not is_dealer,
        "days_on_site": days_on_site,
    }


def format_avito_analysis(analysis: dict[str, Any]) -> str:
    """Форматирует блок "Почему" для Telegram."""
    if not analysis:
        return ""
    score = analysis.get("score")
    if score is None:
        return ""
    reasons = [r for r in analysis.get("reasons", []) if r]
    if not reasons:
        return f"⭐ Оценка: {score}/100"

    lines = [f"⭐ Оценка: {score}/100", "Почему:"]
    for reason in reasons[:5]:
        lines.append(f"✅ {reason}")
    flags = analysis.get("flags", [])
    if "below_market" in flags:
        lines.insert(1, "🔥 Ниже рынка")
    return "\n".join(lines)


def format_avito_card(item: dict[str, Any]) -> str:
    """Единый текст карточки Avito для Telegram."""
    title = str(item.get("title", "") or "Автомобиль").strip()
    price = str(item.get("price", "") or "цена не указана").strip()
    city = str(item.get("city", "") or item.get("location", "") or "Город не указан").strip()
    date_str = str(item.get("date", "") or "сегодня")[:10]

    year = item.get("_year") or item.get("year")
    mileage = item.get("mileage")
    specs_parts: list[str] = []
    if year:
        specs_parts.append(f"{year} г.")
    if mileage:
        specs_parts.append(f"{mileage:,} км".replace(",", " "))
    specs = " · ".join(specs_parts)

    analysis = item.get("analysis") or score_avito_deal(item)
    analysis_text = format_avito_analysis(analysis)

    lines = [f"🔵 Avito {title}", f"💰 {price}", f"📍 {city}", f"📅 {date_str}"]
    if specs:
        lines.append(f"📝 {specs}")
    if analysis_text:
        lines.append(analysis_text)

    return "\n".join(lines)
