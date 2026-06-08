from __future__ import annotations
import asyncio
import logging
import time
import numpy as np
import pandas as pd
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

COIN_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
KRAKEN_PAIR  = {"BTC": "XBTUSD",  "ETH": "ETHUSD",  "SOL": "SOLUSD"}
POLY_REF = "https://polymarket.com/markets/crypto?via=max-chron0n"


async def _kraken_df(coin: str, interval_min: int, limit: int = 60) -> pd.DataFrame:
    import aiohttp
    pair = KRAKEN_PAIR.get(coin, "XBTUSD")
    since = int(time.time()) - interval_min * 60 * (limit + 5)
    url = "https://api.kraken.com/0/public/OHLC"
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(url, params={"pair": pair, "interval": interval_min, "since": since}) as r:
            r.raise_for_status()
            d = await r.json()
    if d.get("error"):
        raise RuntimeError(f"Kraken: {d['error']}")
    raw = next((v for k, v in d["result"].items() if k != "last"), [])
    if not raw:
        raise RuntimeError(f"Kraken empty for {pair} {interval_min}m")
    # [time, open, high, low, close, vwap, volume, count]
    rows = [[float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[6])] for c in raw[-limit:]]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    return df


def _rsi(closes: pd.Series, p: int = 9) -> float:
    d = closes.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    v = (100 - 100 / (1 + g / l.replace(0, np.nan))).iloc[-1]
    return round(float(v) if not np.isnan(v) else 50.0, 1)


def _ema(closes: pd.Series, p: int) -> float:
    return float(closes.ewm(span=p, adjust=False).mean().iloc[-1])


def _macd(closes: pd.Series) -> dict:
    e12 = closes.ewm(span=12, adjust=False).mean()
    e26 = closes.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    sig  = macd.ewm(span=9, adjust=False).mean()
    hist = macd - sig
    return {
        "cross_up":   float(hist.iloc[-1]) > 0 > float(hist.iloc[-2]),
        "cross_down": float(hist.iloc[-1]) < 0 < float(hist.iloc[-2]),
        "rising":     float(hist.iloc[-1]) > float(hist.iloc[-2]),
    }


def _bb_pos(closes: pd.Series, p: int = 20) -> float:
    sma = closes.rolling(p).mean()
    std = closes.rolling(p).std()
    rng = float((sma + 2*std - (sma - 2*std)).iloc[-1])
    if rng == 0:
        return 50.0
    return round(float((closes.iloc[-1] - (sma - 2*std).iloc[-1]) / rng * 100), 1)


def _vol_ratio(df: pd.DataFrame) -> float:
    avg = df["volume"].rolling(20).mean().iloc[-1]
    return round(float(df["volume"].iloc[-1] / avg), 2) if avg > 0 else 1.0


def _score(df1m: pd.DataFrame, df5m: pd.DataFrame) -> dict:
    c1, c5 = df1m["close"], df5m["close"]
    price = float(c1.iloc[-1])
    score = 0.0
    signals = []

    # RSI 1m
    rsi1 = _rsi(c1, 9)
    if rsi1 >= 72:
        score -= 22; signals.append(f"RSI(1м) {rsi1} — перекуплен ⚠️")
    elif rsi1 <= 28:
        score += 22; signals.append(f"RSI(1м) {rsi1} — перепродан 💡")
    elif rsi1 >= 58:
        score += 10
    elif rsi1 <= 42:
        score -= 10

    # RSI 5m
    rsi5 = _rsi(c5, 14)
    if rsi5 >= 70:
        score -= 15; signals.append(f"RSI(5м) {rsi5} — зона продажи 🔴")
    elif rsi5 <= 30:
        score += 15; signals.append(f"RSI(5м) {rsi5} — зона покупки 🟢")

    # EMA тренд 1m
    ema9  = _ema(c1, 9)
    ema21 = _ema(c1, 21)
    if price > ema9 > ema21:
        score += 14; signals.append("EMA9 > EMA21 — восходящий тренд ↗")
    elif price < ema9 < ema21:
        score -= 14; signals.append("EMA9 < EMA21 — нисходящий тренд ↘")

    # MACD 5m
    m5 = _macd(c5)
    if m5["cross_up"]:
        score += 18; signals.append("MACD(5м) кросс вверх 📈")
    elif m5["cross_down"]:
        score -= 18; signals.append("MACD(5м) кросс вниз 📉")
    elif m5["rising"]:
        score += 7
    else:
        score -= 7

    # Bollinger 5m
    bbp = _bb_pos(c5)
    if bbp > 88:
        score -= 10; signals.append(f"BB(5м) у верхней полосы — откат вероятен")
    elif bbp < 12:
        score += 10; signals.append(f"BB(5м) у нижней полосы — отскок вероятен")

    # Объём 1m
    vr = _vol_ratio(df1m)
    if vr >= 1.8:
        if score > 0: score += 8; signals.append(f"Объём {vr}x — подтверждает рост 🔥")
        else:         score -= 8; signals.append(f"Объём {vr}x — подтверждает падение 🔥")

    # Последние 3 свечи
    last3 = df1m["close"].iloc[-3:].values
    open3 = df1m["open"].iloc[-3:].values
    bulls = sum(1 for c, o in zip(last3, open3) if c > o)
    if bulls == 3:   score += 8
    elif bulls == 0: score -= 8

    score = max(-100.0, min(100.0, score))
    conf  = min(int(abs(score) * 0.55 + 42), 94)
    direction = "UP" if score > 8 else "DOWN" if score < -8 else "FLAT"

    return {
        "price": price, "score": score, "direction": direction,
        "confidence": conf, "rsi1": rsi1, "rsi5": rsi5,
        "signals": signals[:3],
    }


async def get_short_forecast(coin: str) -> dict:
    # Try Kraken first (real 1m/5m candles)
    try:
        df1m, df5m = await asyncio.gather(
            _kraken_df(coin, 1, 60),
            _kraken_df(coin, 5, 60),
        )
        result = _score(df1m, df5m)
        result["coin"] = coin
        result["source"] = "kraken"
        return result
    except Exception as e:
        logger.warning(f"Kraken failed for {coin}: {e} — falling back to DB+CoinGecko")

    # Fallback: use last saved signal from DB + CoinGecko price
    return await _forecast_from_db(coin)


async def _forecast_from_db(coin: str) -> dict:
    """Fallback forecast using saved signal indicators + CoinGecko current price."""
    import aiohttp

    # Get current price from CoinGecko (always works)
    cg_ids = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
    cg_id = cg_ids.get(coin, "bitcoin")
    price, change = 0.0, 0.0
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd&include_24hr_change=true"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(url) as r:
                d = await r.json()
        price  = float(d[cg_id]["usd"])
        change = float(d[cg_id].get("usd_24h_change", 0))
    except Exception as e:
        logger.warning(f"CoinGecko failed: {e}")

    # Get latest signal from DB for indicator data
    try:
        from app.services.signal_service import get_recent_signals
        sigs = await get_recent_signals(coin=coin, limit=1)
    except Exception:
        sigs = []

    score, signals_list = 0.0, []

    if sigs:
        sig = sigs[0]
        rsi = sig.rsi_1h or 50.0

        if rsi >= 70:
            score -= 22; signals_list.append(f"RSI {rsi:.0f} — перекуплен ⚠️")
        elif rsi <= 30:
            score += 22; signals_list.append(f"RSI {rsi:.0f} — перепродан 💡")
        elif rsi >= 58:
            score += 12; signals_list.append(f"RSI {rsi:.0f} — зона покупателей 🐂")
        elif rsi <= 42:
            score -= 12; signals_list.append(f"RSI {rsi:.0f} — зона продавцов 🐻")

        if sig.direction == "LONG":
            score += 18; signals_list.append(f"Последний сигнал: ЛОНГ {sig.confidence:.0f}% 📈")
        elif sig.direction == "SHORT":
            score -= 18; signals_list.append(f"Последний сигнал: ШОРТ {sig.confidence:.0f}% 📉")

        fund = (sig.funding_rate or 0) * 100
        if fund > 0.05:
            score -= 8; signals_list.append(f"Фандинг +{fund:.3f}% — лонги перегреты")
        elif fund < -0.01:
            score += 8; signals_list.append(f"Фандинг {fund:.3f}% — шорты перегреты")

        fg = sig.fear_greed or 50
        if fg >= 75:   score -= 8
        elif fg <= 25: score += 8

        if not price and sig.entry_price:
            price = sig.entry_price

        # 24h change as extra signal
        if change > 3:   score += 10; signals_list.append(f"Рост +{change:.1f}% за 24ч 📈")
        elif change < -3: score -= 10; signals_list.append(f"Падение {change:.1f}% за 24ч 📉")
    else:
        # No signals at all — use price change only
        if change > 2:   score += 15; signals_list.append(f"Рост +{change:.1f}% за 24ч 📈")
        elif change < -2: score -= 15; signals_list.append(f"Падение {change:.1f}% за 24ч 📉")
        else:             signals_list.append("Нажми BTC/ETH/SOL в меню для точного прогноза")

    score = max(-100.0, min(100.0, score))
    conf  = min(int(abs(score) * 0.45 + 45), 88)
    direction = "UP" if score > 8 else "DOWN" if score < -8 else "FLAT"

    return {
        "coin": coin, "price": price, "change_24h": change,
        "score": score, "direction": direction, "confidence": conf,
        "rsi1": sigs[0].rsi_1h if sigs else 50.0,
        "rsi5": 50.0, "signals": signals_list[:3],
        "source": "db",
    }


def format_forecast(f: dict) -> str:
    coin, direction, conf = f["coin"], f["direction"], f["confidence"]
    price  = f.get("price", 0.0)
    sigs   = f.get("signals", [])
    source = f.get("source", "")
    now    = datetime.now(timezone.utc).strftime("%H:%M UTC")

    if direction == "UP":
        dir_emoji, dir_text = "🟢", "РОСТ ↑"
        poly_action = "✅ Ставить YES — цена вырастет"
    elif direction == "DOWN":
        dir_emoji, dir_text = "🔴", "ПАДЕНИЕ ↓"
        poly_action = "❌ Ставить NO — цена не вырастет"
    else:
        dir_emoji, dir_text = "⚪", "БОКОВИК ↔"
        poly_action = "⏸ Пропусти — нет чёткого движения"

    bar = "█" * max(0, min(10, round(conf / 10))) + "░" * (10 - max(0, min(10, round(conf / 10))))
    price_str = f"${price:,.2f}" if price >= 1000 else f"${price:.4f}" if price >= 1 else f"${price:.6f}"
    import html as _html
    sigs_text = "\n".join(f"  • {_html.escape(s)}" for s in sigs) if sigs else "  • Нейтральные условия"

    src_note = "" if source == "kraken" else "\n<i>📡 Данные: технический анализ</i>"

    return (
        f"{dir_emoji} <b>{coin}/USDT — {dir_text}</b>\n"
        f"⏱ Горизонт: <b>5–10 минут</b>  ·  {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Цена: <b>{price_str}</b>\n"
        f"📊 Уверенность: <b>{conf}%</b>  <code>{bar}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Сигналы:</b>\n{sigs_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{poly_action}\n"
        f"🔗 <a href=\"{POLY_REF}\">Ставить на Polymarket</a>"
        f"{src_note}"
    )
