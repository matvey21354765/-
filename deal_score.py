"""DealScore: оценка выгодности объявления Avito 0-100."""
from __future__ import annotations

import re
from typing import Any

from avito_normalizer import normalize_avito_item
from listing_quality import parse_price


MIN_REASONABLE_PRICE = 10_000
MAX_REASONABLE_PRICE = 99_000_000


JUNK_PHRASES = (
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
    "куплю",
    "ищу",
    "снимаю",
    "аренда",
    "услуги",
    "запчасть",
)

PRIVATE_HINTS = (
    "собственник",
    "один хозяин",
    "владелец",
    "хозяин",
    "частное лицо",
    "частник",
)

DEALER_HINTS = (
    "автосалон",
    "официальный дилер",
    "дилерский центр",
    "trade-in",
    "трейд-ин",
    "кредит",
    "рассрочка",
    "перекуп",
    "автодилер",
    "компания",
)

URGENT_HINTS = (
    "срочно",
    "торг",
    "нужны деньги",
    "срочная продажа",
    "срочно продам",
    "торг уместен",
    "реальному покупателю торг",
)

RISK_HINTS = (
    "после дтп",
    "требует ремонта",
    "не на ходу",
    "вложений не требует",
    "битая",
    "утопленник",
    "страховая",
    "с запахом",
    "обман",
)


def _price_to_int(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        val = int(value)
        return val if val > 0 else None
    if isinstance(value, str):
        val = parse_price(value)
        return val if val and val > 0 else None
    return None


def calculate_deal_score(
    item: dict[str, Any],
    market_price: int | None = None,
) -> dict[str, Any]:
    """Возвращает DealScore 0-100 и детали анализа."""
    normalized = normalize_avito_item(item)
    score = 0
    reasons: list[str] = []
    flags: list[str] = []
    potential_profit = 0

    price = normalized["price"]
    title = normalized["title"].lower()
    description = normalized["description"].lower()
    text = f"{title}\n{description}"
    images = normalized["images"] or []
    year = normalized["year"]
    mileage = normalized["mileage"]
    seller_type = normalized["seller_type"]
    seller_name = normalized["seller"]

    # 1. Цена ниже рынка (до 70 баллов)
    if price and market_price and market_price > 0 and price < market_price:
        diff = market_price - price
        pct = min(diff / market_price, 0.5)
        score += int(pct * 70)
        potential_profit = diff
        reasons.append(f"ниже рынка на ~{diff:,} ₽ ({int(pct*100)}%)".replace(",", " "))
        flags.append("below_market")
    elif price and market_price and market_price > 0:
        reasons.append("цена около рынка")
    else:
        reasons.append("нет данных о рынке")

    # 2. Срочная продажа / торг (до 10 баллов)
    if any(h in text for h in URGENT_HINTS):
        score += 10
        reasons.append("срочная продажа / торг")
        flags.append("urgent")

    # 3. Частник (до 15 баллов)
    is_private = seller_type in ("private", "person") or any(h in text for h in PRIVATE_HINTS)
    is_dealer = seller_type in ("dealer", "company") or any(h in text for h in DEALER_HINTS) or any(h in seller_name.lower() for h in DEALER_HINTS)
    if is_private and not is_dealer:
        score += 15
        reasons.append("частный продавец")
        flags.append("private_seller")
    elif is_dealer:
        reasons.append("продавец похож на дилера/перекупа")
        flags.append("dealer_or_reseller")
        score -= 5

    # 4. Фото (до 15 баллов)
    if images:
        score += 15
        reasons.append("есть фото")
    else:
        reasons.append("нет фото")
        flags.append("no_photos")

    # 5. Маленький пробег (до 10 баллов)
    if mileage is not None and mileage < 100_000:
        score += 5
        if mileage < 50_000:
            score += 5
        reasons.append("низкий пробег")
    elif mileage is not None:
        reasons.append(f"пробег {mileage:,} км".replace(",", " "))

    # 6. Год (до 15 баллов)
    if year and 2015 <= year <= 2026:
        score += 15
        reasons.append(f"свежий год: {year}")

    # 7. Риски / минусы
    if any(h in text for h in RISK_HINTS):
        score -= 15
        reasons.append("обнаружены риски в описании")
        flags.append("risk_phrases")

    if any(h in text for h in JUNK_PHRASES):
        score -= 25
        reasons.append("подозрительные фразы в описании")
        flags.append("junk_phrases")

    if price < MIN_REASONABLE_PRICE or price > MAX_REASONABLE_PRICE:
        score -= 15
        reasons.append("недостоверная цена")
        flags.append("suspicious_price")

    score = max(0, min(100, score))

    return {
        "score": score,
        "reasons": reasons,
        "flags": flags,
        "deal_good": score >= 80,
        "market_price": market_price,
        "price": price,
        "potential_profit": potential_profit,
        "has_photos": bool(images),
        "private_seller": is_private and not is_dealer,
        "dealer": is_dealer,
        "mileage": mileage,
        "year": year,
    }


def format_deal_score(score_data: dict[str, Any]) -> str:
    """Форматирует DealScore для Telegram."""
    score = score_data.get("score")
    if score is None:
        return ""
    lines = [f"⭐ DealScore: {score}/100"]
    if score_data.get("deal_good"):
        lines.append("🔥 ВЫГОДНАЯ СДЕЛКА")
    market = score_data.get("market_price")
    price = score_data.get("price")
    profit = score_data.get("potential_profit")
    if market and price:
        lines.append(f"💰 Цена: {price:,} ₽".replace(",", " "))
        lines.append(f"📊 Рынок: {market:,} ₽".replace(",", " "))
    if profit and profit > 0:
        lines.append(f"💵 Потенциал: +{profit:,} ₽".replace(",", " "))
    if score_data.get("reasons"):
        lines.append("📌 Причина:")
        for reason in score_data["reasons"][:5]:
            lines.append(f"• {reason}")
    return "\n".join(lines)
