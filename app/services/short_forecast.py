from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

COIN_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
POLY_REF = "https://polymarket.com/markets/crypto?via=max-chron0n"


async def get_short_forecast(coin: str) -> dict:
    """Use existing working snapshot data to generate a short-term forecast."""
    from app.services.binance import get_full_snapshot

    snap = await get_full_snapshot(coin)

    price = snap["price"]
    i1h = snap["i1h"]
    i4h = snap["i4h"]
    funding = snap["funding_rate"]
    fg = snap["fear_greed"]
    ls = snap["long_short_ratio"]
    vol = snap["volume"]
    change_24h = snap["change_24h"]

    score = 0.0
    signals = []

    # RSI 1h
    rsi = i1h["rsi"]
    if rsi >= 70:
        score -= 20
        signals.append(f"RSI {rsi} — перекуплен, давление вниз ⚠️")
    elif rsi <= 30:
        score += 20
        signals.append(f"RSI {rsi} — перепродан, отскок вверх 💡")
    elif rsi >= 60:
        score += 12
        signals.append(f"RSI {rsi} — зона покупателей 🐂")
    elif rsi <= 40:
        score -= 12
        signals.append(f"RSI {rsi} — зона продавцов 🐻")

    # MACD 1h
    macd = i1h["macd"]
    if macd["bullish_cross"]:
        score += 18
        signals.append("MACD кросс вверх на 1ч — бычий импульс 📈")
    elif macd["bearish_cross"]:
        score -= 18
        signals.append("MACD кросс вниз на 1ч — медвежий импульс 📉")
    elif macd["bullish"]:
        score += 8
    else:
        score -= 8

    # EMA trend 1h
    ema20 = i1h["ema_20"]
    ema50 = i1h["ema_50"]
    if price > ema20 > ema50:
        score += 14
        signals.append(f"Цена выше EMA20/EMA50 — восходящий тренд ↗")
    elif price < ema20 < ema50:
        score -= 14
        signals.append(f"Цена ниже EMA20/EMA50 — нисходящий тренд ↘")

    # Bollinger 1h
    bb = i1h["bollinger"]
    bbp = bb["position_pct"]
    if bbp > 90:
        score -= 10
        signals.append(f"У верхней BB — возможен откат вниз")
    elif bbp < 10:
        score += 10
        signals.append(f"У нижней BB — возможен отскок вверх")

    # Funding rate
    fund_pct = funding * 100
    if fund_pct > 0.05:
        score -= 8
        signals.append(f"Фандинг +{fund_pct:.3f}% — лонги перегреты")
    elif fund_pct < -0.01:
        score += 8
        signals.append(f"Фандинг {fund_pct:.3f}% — шорты перегреты")

    # Long/Short ratio
    if ls > 1.3:
        score += 6
    elif ls < 0.8:
        score -= 6

    # Volume
    if vol["high_volume"]:
        if score > 0:
            score += 8
            signals.append(f"Объём {vol['ratio']}x — подтверждает рост 🔥")
        else:
            score -= 8
            signals.append(f"Объём {vol['ratio']}x — подтверждает падение 🔥")

    # 4h trend confirmation
    macd4h = i4h["macd"]
    if macd4h["bullish"]:
        score += 6
    else:
        score -= 6

    score = max(-100.0, min(100.0, score))
    confidence = int(abs(score) * 0.5 + 45)
    confidence = min(confidence, 95)

    direction = "UP" if score > 8 else "DOWN" if score < -8 else "FLAT"

    return {
        "coin": coin,
        "price": price,
        "score": score,
        "direction": direction,
        "confidence": confidence,
        "rsi": rsi,
        "change_24h": change_24h,
        "signals": signals[:3],
    }


def format_forecast(f: dict) -> str:
    coin = f["coin"]
    price = f["price"]
    direction = f["direction"]
    conf = f["confidence"]
    signals = f["signals"]
    change = f["change_24h"]
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    if direction == "UP":
        dir_emoji, dir_text = "🟢", "РОСТ ↑"
        poly_action = "✅ YES — цена вырастет"
    elif direction == "DOWN":
        dir_emoji, dir_text = "🔴", "ПАДЕНИЕ ↓"
        poly_action = "❌ NO — цена не вырастет"
    else:
        dir_emoji, dir_text = "⚪", "БОКОВИК ↔"
        poly_action = "⏸ Воздержись от ставки"

    filled = max(0, min(10, round(conf / 10)))
    bar = "█" * filled + "░" * (10 - filled)

    if price >= 1000:
        price_str = f"${price:,.2f}"
    elif price >= 1:
        price_str = f"${price:.4f}"
    else:
        price_str = f"${price:.6f}"

    ch_sign = "+" if change >= 0 else ""
    signals_text = "\n".join(f"  • {s}" for s in signals) if signals else "  • Нейтральные условия"

    return (
        f"{dir_emoji} <b>{coin}/USDT — {dir_text}</b>\n"
        f"⏱ Горизонт: <b>5–30 минут</b>  ·  {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Цена: <b>{price_str}</b>  <i>({ch_sign}{change:.2f}% за 24ч)</i>\n"
        f"📊 Уверенность: <b>{conf}%</b>  <code>{bar}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Сигналы:</b>\n{signals_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{poly_action}\n"
        f"🔗 <a href=\"{POLY_REF}\">Ставить на Polymarket</a>\n"
        f"<i>⚠️ Не является финансовым советом</i>"
    )
