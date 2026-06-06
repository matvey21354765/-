from __future__ import annotations
import asyncio
import logging
from typing import Optional
import aiohttp

logger = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_URL  = "https://clob.polymarket.com"

# Keywords to find BTC/ETH/SOL markets on Polymarket
_KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"],
}


async def _fetch(url: str, params: dict = None) -> list | dict | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url, params=params) as r:
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
    except Exception as e:
        logger.warning(f"Polymarket fetch error: {e}")
        return None


async def get_best_market(coin: str) -> Optional[dict]:
    """Find the highest-confidence active market for a coin."""
    keywords = _KEYWORDS.get(coin, [coin.lower()])

    for kw in keywords:
        data = await _fetch(GAMMA_URL, {
            "active": "true", "closed": "false",
            "keyword": kw, "limit": 20,
        })
        if not data:
            continue

        markets = data if isinstance(data, list) else data.get("markets", [])
        best = None
        best_conf = 0.0

        for m in markets:
            try:
                prices = m.get("outcomePrices") or []
                if isinstance(prices, str):
                    import json as _json
                    prices = _json.loads(prices)
                if not prices:
                    continue

                # prices[0] = YES probability (0-1 or 0-100)
                yes_prob = float(prices[0])
                if yes_prob > 1:
                    yes_prob /= 100

                # confidence = how far from 50% (closer to 0% or 100%)
                conf = abs(yes_prob - 0.5) * 2  # 0..1

                if conf > best_conf:
                    best_conf = conf
                    best = {
                        "coin": coin,
                        "question": m.get("question", ""),
                        "yes_prob": round(yes_prob * 100, 1),
                        "no_prob":  round((1 - yes_prob) * 100, 1),
                        "confidence": round(conf * 100, 1),
                        "market_slug": m.get("slug", ""),
                        "end_date": m.get("endDate", ""),
                        "url": f"https://polymarket.com/event/{m.get('slug', '')}",
                    }
            except Exception:
                continue

        if best and best["confidence"] >= 70:
            return best

    return None


async def get_high_confidence_markets(min_conf: float = 85.0) -> list[dict]:
    """Return markets with confidence >= min_conf for BTC/ETH/SOL."""
    results = []
    for coin in ["BTC", "ETH", "SOL"]:
        m = await get_best_market(coin)
        if m and m["confidence"] >= min_conf:
            results.append(m)
        await asyncio.sleep(0.5)
    return results


def format_polymarket_alert(markets: list[dict]) -> str:
    if not markets:
        return ""
    lines = ["🎯 <b>ВЫСОКАЯ УВЕРЕННОСТЬ НА POLYMARKET</b>\n━━━━━━━━━━━━━━━━━━━━"]
    for m in markets:
        outcome = "ДА 🟢" if m["yes_prob"] >= 50 else "НЕТ 🔴"
        prob = m["yes_prob"] if m["yes_prob"] >= 50 else m["no_prob"]
        lines.append(
            f"\n<b>{m['coin']}</b> — уверенность <b>{m['confidence']:.0f}%</b>\n"
            f"❓ {m['question']}\n"
            f"➡️ Ответ: <b>{outcome}</b> ({prob:.0f}%)\n"
            f"🔗 <a href=\"{m['url']}\">Сделать ставку</a>"
        )
    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)
