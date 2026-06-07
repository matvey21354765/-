from __future__ import annotations
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

POLY_REF = "https://polymarket.com/markets/crypto?via=max-chron0n"


async def get_short_forecast(coin: str) -> dict:
    """Build short-term forecast from the latest saved signal in DB."""
    from app.services.signal_service import get_recent_signals
    from app.services.binance import fetch_ticker, SYMBOL_MAP

    # Get latest signal for this coin
    signals = await get_recent_signals(coin=coin, limit=1)

    if not signals:
        # No signal yet — try to get ticker price
        try:
            symbol = SYMBOL_MAP.get(coin, coin + "USDT")
            ticker = await fetch_ticker(symbol)
            price = float(ticker["lastPrice"])
            change = float(ticker["priceChangePercent"])
        except Exception:
            price = 0.0
            change = 0.0
        return {
            "coin": coin, "price": price, "change_24h": change,
            "score": 0.0, "direction": "FLAT", "confidence": 50,
            "rsi": 50.0, "signals": ["Сначала запроси сигнал по монете (кнопки BTC/ETH/SOL)"],
            "no_signal": True,
        }

    sig = signals[0]

    score = 0.0
    signals_list = []

    # RSI
    rsi = sig.rsi_1h or 50.0
    if rsi >= 70:
        score -= 22
        signals_list.append(f"RSI {rsi:.0f} — перекуплен, риск отката ⚠️")
    elif rsi <= 30:
        score += 22
        signals_list.append(f"RSI {rsi:.0f} — перепродан, отскок вероятен 💡")
    elif rsi >= 60:
        score += 13
        signals_list.append(f"RSI {rsi:.0f} — зона покупателей 🐂")
    elif rsi <= 40:
        score -= 13
        signals_list.append(f"RSI {rsi:.0f} — зона продавцов 🐻")

    # Signal direction
    if sig.direction == "LONG":
        score += 20
        signals_list.append(f"Последний сигнал: ЛОНГ (уверен. {sig.confidence:.0f}%) 📈")
    elif sig.direction == "SHORT":
        score -= 20
        signals_list.append(f"Последний сигнал: ШОРТ (уверен. {sig.confidence:.0f}%) 📉")

    # Funding rate
    funding = (sig.funding_rate or 0) * 100
    if funding > 0.05:
        score -= 8
        signals_list.append(f"Фандинг +{funding:.3f}% — лонги перегреты")
    elif funding < -0.01:
        score += 8
        signals_list.append(f"Фандинг {funding:.3f}% — шорты перегреты")

    # Fear & Greed
    fg = sig.fear_greed or 50
    if fg >= 75:
        score -= 10
    elif fg <= 25:
        score += 10

    score = max(-100.0, min(100.0, score))
    confidence = int(abs(score) * 0.5 + 48)
    confidence = min(confidence, 93)

    direction = "UP" if score > 10 else "DOWN" if score < -10 else "FLAT"

    # Get current price via ticker (single fast call)
    try:
        symbol = SYMBOL_MAP.get(coin, coin + "USDT")
        ticker = await fetch_ticker(symbol)
        price = float(ticker["lastPrice"])
        change = float(ticker["priceChangePercent"])
    except Exception:
        price = sig.entry_price or 0
        change = 0.0

    return {
        "coin": coin,
        "price": price,
        "change_24h": change,
        "score": score,
        "direction": direction,
        "confidence": confidence,
        "rsi": rsi,
        "signals": signals_list[:3],
        "no_signal": False,
    }


def format_forecast(f: dict) -> str:
    coin = f["coin"]
    price = f["price"]
    direction = f["direction"]
    conf = f["confidence"]
    sigs = f["signals"]
    change = f["change_24h"]
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    if direction == "UP":
        dir_emoji, dir_text = "🟢", "РОСТ ↑"
        poly_action = "✅ Ставить YES — цена вырастет"
    elif direction == "DOWN":
        dir_emoji, dir_text = "🔴", "ПАДЕНИЕ ↓"
        poly_action = "❌ Ставить NO — цена не вырастет"
    else:
        dir_emoji, dir_text = "⚪", "БОКОВИК ↔"
        poly_action = "⏸ Воздержись от ставки"

    filled = max(0, min(10, round(conf / 10)))
    bar = "█" * filled + "░" * (10 - filled)

    price_str = f"${price:,.2f}" if price >= 1000 else f"${price:.4f}" if price >= 1 else f"${price:.6f}"
    ch_sign = "+" if change >= 0 else ""
    sigs_text = "\n".join(f"  • {s}" for s in sigs) if sigs else "  • Нейтральные условия"

    note = ""
    if f.get("no_signal"):
        note = "\n<i>💡 Запроси сигнал по монете для точного прогноза</i>"

    return (
        f"{dir_emoji} <b>{coin}/USDT — {dir_text}</b>\n"
        f"⏱ Горизонт: <b>5–30 минут</b>  ·  {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Цена: <b>{price_str}</b>  <i>({ch_sign}{change:.2f}% за 24ч)</i>\n"
        f"📊 Уверенность: <b>{conf}%</b>  <code>{bar}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Сигналы:</b>\n{sigs_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{poly_action}\n"
        f"🔗 <a href=\"{POLY_REF}\">Ставить на Polymarket</a>"
        f"{note}"
    )
