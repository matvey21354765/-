from __future__ import annotations
import asyncio
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

COIN_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
POLY_REF = "https://polymarket.com/markets/crypto?via=max-chron0n"


async def _fetch_df(symbol: str, interval: str, limit: int = 60) -> pd.DataFrame:
    from app.services.binance import _okx_klines
    raw = await _okx_klines(symbol, interval, limit)
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_vol", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df


def _rsi(closes: pd.Series, p: int = 14) -> float:
    d = closes.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    v = (100 - 100 / (1 + g / l.replace(0, np.nan))).iloc[-1]
    return round(float(v) if not np.isnan(v) else 50.0, 1)


def _ema(closes: pd.Series, p: int) -> float:
    return float(closes.ewm(span=p, adjust=False).mean().iloc[-1])


def _macd_hist(closes: pd.Series) -> float:
    e12 = closes.ewm(span=12, adjust=False).mean()
    e26 = closes.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    sig = macd.ewm(span=9, adjust=False).mean()
    return float((macd - sig).iloc[-1])


def _bb_position(closes: pd.Series, p: int = 20) -> float:
    """0=lower band, 100=upper band"""
    sma = closes.rolling(p).mean()
    std = closes.rolling(p).std()
    upper = sma + 2 * std
    lower = sma - 2 * std
    rng = float(upper.iloc[-1] - lower.iloc[-1])
    if rng == 0:
        return 50.0
    return round(float((closes.iloc[-1] - lower.iloc[-1]) / rng * 100), 1)


def _momentum(closes: pd.Series, p: int = 5) -> float:
    """% change over last p candles"""
    return round(float((closes.iloc[-1] - closes.iloc[-p]) / closes.iloc[-p] * 100), 3)


def _vol_ratio(df: pd.DataFrame, p: int = 20) -> float:
    avg = df["volume"].rolling(p).mean().iloc[-1]
    return round(float(df["volume"].iloc[-1] / avg), 2) if avg > 0 else 1.0


def _score(df1m: pd.DataFrame, df5m: pd.DataFrame) -> dict:
    """Score -100..+100. Positive = bullish."""
    c1 = df1m["close"]
    c5 = df5m["close"]
    price = float(c1.iloc[-1])

    score = 0.0
    signals = []

    # 1m RSI
    rsi1 = _rsi(c1, 9)
    if rsi1 > 65:
        score += 15
        signals.append(f"RSI(1м)={rsi1} — перекуплен, давление вниз")
        score -= 30
    elif rsi1 < 35:
        score -= 15
        signals.append(f"RSI(1м)={rsi1} — перепродан, отскок вверх")
        score += 30
    elif rsi1 > 55:
        score += 10
    elif rsi1 < 45:
        score -= 10

    # 5m RSI
    rsi5 = _rsi(c5, 14)
    if rsi5 > 70:
        score -= 20
        signals.append(f"RSI(5м)={rsi5} — зона продажи")
    elif rsi5 < 30:
        score += 20
        signals.append(f"RSI(5м)={rsi5} — зона покупки")
    elif rsi5 > 55:
        score += 8
    elif rsi5 < 45:
        score -= 8

    # EMA trend 1m
    ema9 = _ema(c1, 9)
    ema21 = _ema(c1, 21)
    if price > ema9 > ema21:
        score += 15
        signals.append("Цена > EMA9 > EMA21 — бычий тренд на 1м")
    elif price < ema9 < ema21:
        score -= 15
        signals.append("Цена < EMA9 < EMA21 — медвежий тренд на 1м")

    # MACD 5m
    macd5 = _macd_hist(c5)
    prev_macd5 = _macd_hist(c5.iloc[:-1])
    if macd5 > 0 and macd5 > prev_macd5:
        score += 12
        signals.append("MACD(5м) растёт — импульс вверх")
    elif macd5 < 0 and macd5 < prev_macd5:
        score -= 12
        signals.append("MACD(5м) падает — импульс вниз")

    # BB position 5m
    bbp = _bb_position(c5)
    if bbp > 85:
        score -= 10
        signals.append(f"BB(5м) {bbp:.0f}% — у верхней полосы, коррекция вероятна")
    elif bbp < 15:
        score += 10
        signals.append(f"BB(5м) {bbp:.0f}% — у нижней полосы, отскок вероятен")

    # Momentum 1m (last 3 candles)
    mom1 = _momentum(c1, 3)
    if mom1 > 0.05:
        score += 10
        signals.append(f"Моментум +{mom1:.2f}% за 3 свечи")
    elif mom1 < -0.05:
        score -= 10
        signals.append(f"Моментум {mom1:.2f}% за 3 свечи")

    # Volume confirmation
    vr = _vol_ratio(df1m)
    if vr > 1.5:
        # volume confirms direction
        if score > 0:
            score += 8
            signals.append(f"Объём {vr:.1f}x — подтверждает рост")
        else:
            score -= 8
            signals.append(f"Объём {vr:.1f}x — подтверждает падение")

    # Last candle body direction
    last_bull = float(df1m["close"].iloc[-1]) > float(df1m["open"].iloc[-1])
    prev_bull = float(df1m["close"].iloc[-2]) > float(df1m["open"].iloc[-2])
    if last_bull and prev_bull:
        score += 8
    elif not last_bull and not prev_bull:
        score -= 8

    score = max(-100, min(100, score))
    confidence = round(abs(score) * 0.6 + 40)  # 40–100%
    direction = "UP" if score > 5 else "DOWN" if score < -5 else "FLAT"

    return {
        "price": price,
        "score": score,
        "direction": direction,
        "confidence": confidence,
        "rsi1": rsi1,
        "rsi5": rsi5,
        "bbp": bbp,
        "mom1": mom1,
        "vol_ratio": vr,
        "signals": signals[:3],
    }


async def get_short_forecast(coin: str) -> dict:
    symbol = COIN_SYMBOL.get(coin, coin + "USDT")
    df1m, df5m = await asyncio.gather(
        _fetch_df(symbol, "1m", 60),
        _fetch_df(symbol, "5m", 60),
    )
    result = _score(df1m, df5m)
    result["coin"] = coin
    result["symbol"] = symbol
    return result


def format_forecast(f: dict) -> str:
    coin = f["coin"]
    price = f["price"]
    direction = f["direction"]
    conf = f["confidence"]
    score = f["score"]
    signals = f["signals"]
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    if direction == "UP":
        dir_emoji = "🟢"
        dir_text = "РОСТ ↑"
        poly_action = "YES (вырастет)"
        poly_emoji = "🟢"
    elif direction == "DOWN":
        dir_emoji = "🔴"
        dir_text = "ПАДЕНИЕ ↓"
        poly_action = "NO (не вырастет)"
        poly_emoji = "🔴"
    else:
        dir_emoji = "⚪"
        dir_text = "БОКОВИК ↔"
        poly_action = "воздержись от ставки"
        poly_emoji = "⚪"

    # Confidence bar
    filled = max(0, min(10, round(conf / 10)))
    bar = "█" * filled + "░" * (10 - filled)

    # Price format
    if price >= 1000:
        price_str = f"${price:,.2f}"
    elif price >= 1:
        price_str = f"${price:.4f}"
    else:
        price_str = f"${price:.6f}"

    signals_text = "\n".join(f"  • {s}" for s in signals) if signals else "  • Нейтральные условия"

    return (
        f"{dir_emoji} <b>{coin}/USDT — {dir_text}</b>\n"
        f"⏱ Горизонт: <b>5–10 минут</b>  ·  {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Цена сейчас: <b>{price_str}</b>\n"
        f"📊 Уверенность: <b>{conf}%</b>  <code>{bar}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Что говорит анализ:</b>\n"
        f"{signals_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{poly_emoji} <b>Ставка на Polymarket:</b> {poly_action}\n"
        f"🔗 <a href=\"{POLY_REF}\">Открыть рынок</a>\n"
        f"<i>⚠️ Прогноз на 5–10м. Не является финансовым советом.</i>"
    )
