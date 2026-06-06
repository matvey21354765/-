from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
import aiohttp

logger = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com/markets"

_SEARCH_TERMS = {
    "BTC": ["bitcoin up or down", "bitcoin higher", "bitcoin price june", "btc up or down"],
    "ETH": ["ethereum up or down", "ethereum higher", "ethereum price june", "eth up or down"],
    "SOL": ["solana up or down", "solana higher", "solana price june", "sol up or down"],
}

_COIN_NAME = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana"}


async def _fetch(url: str, params: dict = None) -> list | dict | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url, params=params) as r:
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
    except Exception as e:
        logger.warning(f"Polymarket fetch: {e}")
        return None


async def find_daily_market(coin: str) -> Optional[dict]:
    """Find today's up/down market for a coin."""
    today = datetime.now(timezone.utc)
    day = str(today.day)  # "6" without leading zero, cross-platform
    month = today.strftime("%B").lower()  # "june"
    year = today.strftime("%Y")
    date_strs = [
        f"{month} {day}",    # "june 6"
        f"{month} {day.zfill(2)}",  # "june 06"
        f"{day} {month}",    # "6 june"
        year,                 # "2026"
    ]

    for term in _SEARCH_TERMS.get(coin, []):
        data = await _fetch(GAMMA_URL, {
            "active": "true", "closed": "false",
            "keyword": term, "limit": 30,
        })
        if not data:
            continue
        markets = data if isinstance(data, list) else data.get("markets", [])

        for m in markets:
            q = (m.get("question") or "").lower()
            slug = (m.get("slug") or "").lower()

            # Prefer markets mentioning today's date
            has_today = any(ds in q or ds in slug for ds in date_strs)
            is_updown = any(w in q for w in ["up or down", "higher or lower", "above", "below"])

            if not is_updown:
                continue

            try:
                prices = m.get("outcomePrices") or []
                if isinstance(prices, str):
                    import json as _j
                    prices = _j.loads(prices)
                if not prices:
                    continue
                yes_prob = float(prices[0])
                if yes_prob > 1:
                    yes_prob /= 100
            except Exception:
                continue

            slug_val = m.get("slug", "")
            url = f"https://polymarket.com/event/{slug_val}"

            return {
                "coin": coin,
                "question": m.get("question", ""),
                "yes_prob": round(yes_prob * 100, 1),
                "no_prob": round((1 - yes_prob) * 100, 1),
                "slug": slug_val,
                "url": url,
                "has_today": has_today,
                "market_prob_edge": abs(yes_prob - 0.5) < 0.15,  # crowd uncertain (35-65%)
            }

    return None


def build_poly_signal(coin: str, market: dict, snap: dict) -> str:
    """Combine technical analysis with Polymarket market."""
    from app.services.analyzer import analyze_coin
    result = analyze_coin(snap)
    if not result:
        return ""

    direction = result["direction"]
    confidence = result["confidence"]
    reasons = result.get("reasons", [])[:2]
    price = snap["price"]

    # Map our direction to market outcome
    # Most "up or down" markets: YES = price goes UP
    q_lower = market["question"].lower()
    if direction == "LONG":
        our_bet = "ДА (вырастет) 🟢"
        market_prob = market["yes_prob"]
        edge = market["no_prob"] - market["yes_prob"]  # market undervalues YES
    else:
        our_bet = "НЕТ (упадёт) 🔴"
        market_prob = market["no_prob"]
        edge = market["yes_prob"] - market["no_prob"]  # market undervalues NO

    edge_text = f"+{edge:.0f}%" if edge > 0 else f"{edge:.0f}%"
    crowd_says = f"рынок даёт {market_prob:.0f}% на {'рост' if direction == 'LONG' else 'падение'}"

    reasons_text = "\n".join(f"  • {r}" for r in reasons)

    trend_map = {
        "STRONG BULL": "🐂🐂 сильный рост",
        "WEAK BULL": "🐂 слабый рост",
        "NEUTRAL": "↔️ нейтрально",
        "WEAK BEAR": "🐻 слабое падение",
        "STRONG BEAR": "🐻🐻 сильное падение",
    }
    trend = trend_map.get(result.get("trend_strength", ""), "↔️")

    return (
        f"🎯 <b>СТАВКА НА POLYMARKET — {coin}/USDT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"❓ <i>{market['question']}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Наш анализ:</b> {trend}\n"
        f"  Цена: <b>${price:,.2f}</b>  ·  Уверенность: <b>{confidence:.0f}%</b>\n"
        f"{reasons_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <b>Ставить:</b> {our_bet}\n"
        f"  Толпа даёт: <b>{market_prob:.0f}%</b>  ·  Наш прогноз даёт перевес: <b>{edge_text}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <a href=\"{market['url']}\">{market['url']}</a>"
    )


async def get_daily_poly_signals(snaps: dict[str, dict]) -> list[str]:
    """Get Polymarket daily signals for all coins."""
    signals = []
    for coin, snap in snaps.items():
        market = await find_daily_market(coin)
        if not market:
            logger.info(f"[{coin}] No Polymarket daily market found")
            continue
        text = build_poly_signal(coin, market, snap)
        if text:
            signals.append(text)
            logger.info(f"[{coin}] Polymarket signal: {market['question'][:60]}")
        await asyncio.sleep(0.5)
    return signals
