from __future__ import annotations
import asyncio
import logging
import re
from typing import Optional
import aiohttp

logger = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"

# Map coin keywords in market question → our coin symbol
_COIN_MAP = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
}


async def _fetch(url: str, params: dict = None) -> list | dict | None:
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"Accept": "application/json"},
        ) as s:
            async with s.get(url, params=params) as r:
                if r.status != 200:
                    logger.warning(f"Polymarket {url}: HTTP {r.status}")
                    return None
                return await r.json(content_type=None)
    except Exception as e:
        logger.warning(f"Polymarket fetch error: {e}")
        return None


def _parse_prices(market: dict) -> tuple[float, float]:
    """Return (yes_prob, no_prob) as 0-100 floats."""
    prices = market.get("outcomePrices") or []
    if isinstance(prices, str):
        import json as _j
        try:
            prices = _j.loads(prices)
        except Exception:
            return 50.0, 50.0
    if not prices:
        return 50.0, 50.0
    try:
        yes = float(prices[0])
        if yes > 1:
            yes /= 100
        return round(yes * 100, 1), round((1 - yes) * 100, 1)
    except Exception:
        return 50.0, 50.0


def _detect_coin(text: str) -> Optional[str]:
    text = text.lower()
    for kw, coin in _COIN_MAP.items():
        if kw in text:
            return coin
    return None


async def fetch_crypto_markets(limit: int = 50) -> list[dict]:
    """Fetch active crypto markets from Polymarket predictions/crypto page."""
    # Try tag_slug=crypto first
    data = await _fetch(f"{GAMMA_URL}/markets", {
        "active": "true", "closed": "false",
        "tag_slug": "crypto", "limit": limit,
    })
    markets = []
    if data:
        markets = data if isinstance(data, list) else data.get("markets", [])

    # Also try events endpoint
    if not markets:
        data = await _fetch(f"{GAMMA_URL}/events", {
            "active": "true", "closed": "false",
            "tag_slug": "crypto", "limit": limit,
        })
        if data:
            events = data if isinstance(data, list) else data.get("events", [])
            for ev in events:
                for m in (ev.get("markets") or []):
                    markets.append(m)

    return markets


def _analyze_market(market: dict, snaps: dict) -> Optional[dict]:
    """Match market to a coin and return analysis."""
    from app.services.analyzer import analyze_coin

    question = market.get("question") or market.get("title") or ""
    coin = _detect_coin(question)
    if not coin or coin not in snaps:
        return None

    snap = snaps[coin]
    result = analyze_coin(snap)
    if not result:
        return None

    yes_prob, no_prob = _parse_prices(market)
    direction = result["direction"]
    confidence = result["confidence"]
    reasons = result.get("reasons", [])[:2]
    price = snap["price"]
    slug = market.get("slug", "")
    url = f"https://polymarket.com/event/{slug}"

    # Determine our answer to the market question
    # Detect if it's a price UP/DOWN question
    q_low = question.lower()
    is_bullish_q = any(w in q_low for w in ["up", "higher", "above", "rise", "pump", "bull", "gain"])
    is_bearish_q = any(w in q_low for w in ["down", "lower", "below", "fall", "drop", "bear", "loss"])

    if is_bullish_q:
        our_bet = "ДА 🟢" if direction == "LONG" else "НЕТ 🔴"
        our_prob = yes_prob if direction == "LONG" else no_prob
        crowd_prob = yes_prob
    elif is_bearish_q:
        our_bet = "ДА 🟢" if direction == "SHORT" else "НЕТ 🔴"
        our_prob = yes_prob if direction == "SHORT" else no_prob
        crowd_prob = yes_prob
    else:
        # Generic question - just show direction
        our_bet = "ВВЕРХ 📈" if direction == "LONG" else "ВНИЗ 📉"
        our_prob = confidence
        crowd_prob = yes_prob

    # Edge = difference between our confidence and crowd probability
    crowd_on_our_side = crowd_prob if our_bet.startswith("ДА") or our_bet.startswith("ВВЕРХ") else (100 - crowd_prob)
    edge = round(confidence - crowd_on_our_side, 1)

    trend_map = {
        "STRONG BULL": "🐂🐂 сильный рост",
        "WEAK BULL": "🐂 слабый рост",
        "NEUTRAL": "↔️ нейтрально",
        "WEAK BEAR": "🐻 слабое падение",
        "STRONG BEAR": "🐻🐻 сильное падение",
    }
    trend = trend_map.get(result.get("trend_strength", ""), "↔️")
    reasons_text = "\n".join(f"  • {r}" for r in reasons)

    return {
        "coin": coin,
        "question": question,
        "url": url,
        "our_bet": our_bet,
        "confidence": confidence,
        "crowd_prob": crowd_prob,
        "edge": edge,
        "trend": trend,
        "price": price,
        "reasons_text": reasons_text,
    }


def _format_market_signal(m: dict) -> str:
    edge_str = f"+{m['edge']:.0f}%" if m['edge'] > 0 else f"{m['edge']:.0f}%"
    edge_label = "наш перевес" if m['edge'] > 0 else "рынок лучше"
    return (
        f"🎯 <b>POLYMARKET — {m['coin']}/USDT</b>\n"
        f"🔗 <a href=\"{m['url']}\">{m['question']}</a>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Тренд: {m['trend']}\n"
        f"  Цена: <b>${m['price']:,.2f}</b>  ·  Уверенность: <b>{m['confidence']:.0f}%</b>\n"
        f"{m['reasons_text']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 Ставить: <b>{m['our_bet']}</b>\n"
        f"  Толпа: <b>{m['crowd_prob']:.0f}%</b>  ·  {edge_label}: <b>{edge_str}</b>"
    )


async def get_crypto_predictions(snaps: dict, top_n: int = 5) -> list[str]:
    """Fetch crypto markets and return formatted signals for top matches."""
    markets = await fetch_crypto_markets(limit=100)
    if not markets:
        logger.warning("Polymarket: no crypto markets returned")
        return []

    analyzed = []
    for m in markets:
        result = _analyze_market(m, snaps)
        if result:
            analyzed.append(result)

    # Sort by edge (our confidence vs crowd) descending
    analyzed.sort(key=lambda x: x["edge"], reverse=True)

    # Return top N, deduplicate by coin (best per coin)
    seen_coins: set[str] = set()
    texts = []
    for a in analyzed:
        if a["coin"] not in seen_coins:
            seen_coins.add(a["coin"])
            texts.append(_format_market_signal(a))
        if len(texts) >= top_n:
            break

    return texts
