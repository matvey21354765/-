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
    timeout = aiohttp.ClientTimeout(total=5)
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


def _atr(df: pd.DataFrame, p: int = 14) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return float(tr.rolling(p).mean().iloc[-1])


def _sl_tp(price: float, atr: float, direction: str) -> dict:
    """Calculate SL/TP levels and recommended leverage based on ATR."""
    sl_mult, tp1_mult, tp2_mult = 1.5, 1.5, 3.0
    sl_dist = atr * sl_mult
    sl_pct = sl_dist / price * 100

    if direction == "UP":
        sl  = price - sl_dist
        tp1 = price + atr * tp1_mult
        tp2 = price + atr * tp2_mult
    elif direction == "DOWN":
        sl  = price + sl_dist
        tp1 = price - atr * tp1_mult
        tp2 = price - atr * tp2_mult
    else:
        return {}

    # Leverage: lower when volatile
    if sl_pct > 1.0:
        lev = "3–5x"
    elif sl_pct > 0.5:
        lev = "5–10x"
    else:
        lev = "10–20x"

    return {
        "sl": round(sl, 2), "tp1": round(tp1, 2), "tp2": round(tp2, 2),
        "sl_pct": round(sl_pct, 2), "leverage": lev,
    }


def _macd_strength(closes: pd.Series) -> dict:
    """Returns MACD with histogram magnitude for filtering weak signals."""
    e12 = closes.ewm(span=12, adjust=False).mean()
    e26 = closes.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    sig  = macd.ewm(span=9, adjust=False).mean()
    hist = macd - sig
    h_now  = float(hist.iloc[-1])
    h_prev = float(hist.iloc[-2])
    h_prev2 = float(hist.iloc[-3])
    # Magnitude relative to price for coin-agnostic comparison
    price = float(closes.iloc[-1])
    magnitude = abs(h_now) / price * 10000  # basis points
    return {
        "cross_up":   h_now > 0 > h_prev,
        "cross_down": h_now < 0 < h_prev,
        "rising":     h_now > h_prev > h_prev2,
        "falling":    h_now < h_prev < h_prev2,
        "bullish":    h_now > 0,
        "magnitude":  magnitude,
    }


def _score(df1m: pd.DataFrame, df5m: pd.DataFrame, df15m: pd.DataFrame) -> dict:
    """
    Quality-over-quantity scoring: fewer signals, much higher accuracy.
    Only fires UP/DOWN when MACD + trend + at least one confirming indicator agree.
    FLAT = safer than a wrong signal.
    """
    c1, c5, c15 = df1m["close"], df5m["close"], df15m["close"]
    price = float(c1.iloc[-1])

    # ── RANGING MARKET FILTER: if 5m ATR is too small relative to price → FLAT ─
    atr5 = _atr(df5m, 14)
    atr_pct = atr5 / price * 100
    # Below 0.08% ATR on 5m = dead market, no clean moves
    if atr_pct < 0.08:
        rsi1 = _rsi(c1, 9)
        rsi5 = _rsi(c5, 14)
        atr1 = _atr(df1m)
        levels = _sl_tp(price, atr1, "FLAT")
        return {
            "price": price, "score": 0.0, "direction": "FLAT",
            "confidence": 38, "rsi1": rsi1, "rsi5": rsi5,
            "signals": [f"ATR(5м) {atr_pct:.3f}% — рынок во флэте, нет движения"],
        }

    # ── 15m TREND (master filter) ───────────────────────────────────────────
    ema21_15 = _ema(c15, 21)
    ema50_15 = _ema(c15, 50)
    rsi15    = _rsi(c15, 14)
    # Strict trend: EMA aligned AND price on correct side AND RSI not opposing
    trend_up   = (ema21_15 > ema50_15) and (price > ema21_15) and (rsi15 < 72)
    trend_down = (ema21_15 < ema50_15) and (price < ema21_15) and (rsi15 > 28)
    trend_neutral = not trend_up and not trend_down

    # ── 5m MACD — primary signal (must fire for any trade) ──────────────────
    m5 = _macd_strength(c5)
    rsi5 = _rsi(c5, 14)
    ema9_5  = _ema(c5, 9)
    ema21_5 = _ema(c5, 21)
    bbp5    = _bb_pos(c5)

    # ── 1m confirmation ─────────────────────────────────────────────────────
    rsi1   = _rsi(c1, 9)
    ema9_1 = _ema(c1, 9)
    m1     = _macd_strength(c1)
    vr     = _vol_ratio(df1m)

    # Last 5 candles direction
    c5v = df1m["close"].iloc[-5:].values
    o5v = df1m["open"].iloc[-5:].values
    bulls5 = sum(1 for c, o in zip(c5v, o5v) if c > o)
    candle_bull = bulls5 >= 3
    candle_bear = bulls5 <= 2

    # ── SCORING: use integer vote system ────────────────────────────────────
    # Each factor votes +1 bull / -1 bear / 0 neutral
    votes = []

    # 5m MACD — weight 2 (most reliable)
    if m5["cross_up"]:
        votes += [1, 1]   # crossover = 2 votes
    elif m5["cross_down"]:
        votes += [-1, -1]
    elif m5["rising"] and m5["bullish"] and m5["magnitude"] > 0.3:
        votes += [1]
    elif m5["falling"] and not m5["bullish"] and m5["magnitude"] > 0.3:
        votes += [-1]
    else:
        votes += [0]

    # 15m trend — weight 2
    if trend_up:
        votes += [1, 1]
    elif trend_down:
        votes += [-1, -1]
    else:
        votes += [0]

    # 5m EMA alignment — weight 1
    if float(c5.iloc[-1]) > ema9_5 > ema21_5:
        votes += [1]
    elif float(c5.iloc[-1]) < ema9_5 < ema21_5:
        votes += [-1]
    else:
        votes += [0]

    # RSI 5m — only genuine extremes count, neutral zone = 0
    if rsi5 <= 32:
        votes += [1]
    elif rsi5 >= 68:
        votes += [-1]
    else:
        votes += [0]

    # 1m MACD direction — weight 1
    if m1["bullish"] and m1["rising"]:
        votes += [1]
    elif not m1["bullish"] and m1["falling"]:
        votes += [-1]
    else:
        votes += [0]

    # Candle momentum 1m — weight 1
    if candle_bull:
        votes += [1]
    elif candle_bear:
        votes += [-1]
    else:
        votes += [0]

    # 5m Price ROC (rate-of-change over last 3 candles) — weight 1
    # Most direct measure of short-term momentum
    if len(c5) >= 4:
        roc5 = (float(c5.iloc[-1]) - float(c5.iloc[-4])) / float(c5.iloc[-4]) * 100
        if roc5 > 0.15:
            votes += [1]
        elif roc5 < -0.15:
            votes += [-1]
        else:
            votes += [0]

    # 1m Price ROC over last 5 candles — weight 1
    if len(c1) >= 6:
        roc1 = (float(c1.iloc[-1]) - float(c1.iloc[-6])) / float(c1.iloc[-6]) * 100
        if roc1 > 0.05:
            votes += [1]
        elif roc1 < -0.05:
            votes += [-1]
        else:
            votes += [0]

    # Volume spike confirmation — weight 1 (direction-aware)
    if vr >= 1.8:
        bull_vote = sum(1 for v in votes if v > 0)
        bear_vote = sum(1 for v in votes if v < 0)
        if bull_vote > bear_vote:
            votes += [1]
        elif bear_vote > bull_vote:
            votes += [-1]

    # ── DECISION ────────────────────────────────────────────────────────────
    total = sum(votes)
    max_votes = len(votes)
    bull_pct = sum(1 for v in votes if v > 0) / max_votes
    bear_pct = sum(1 for v in votes if v < 0) / max_votes

    # Require strong majority (>60%) AND net score
    # Also block if RSI in extreme opposite zone
    if total >= 3 and bull_pct >= 0.55:
        if rsi5 >= 75 or rsi1 >= 80:  # already overbought — too risky
            direction = "FLAT"
        elif trend_down and total < 5:  # against 15m trend — need very strong signal
            direction = "FLAT"
        else:
            direction = "UP"
    elif total <= -3 and bear_pct >= 0.55:
        if rsi5 <= 25 or rsi1 <= 20:  # already oversold — too risky
            direction = "FLAT"
        elif trend_up and total > -5:
            direction = "FLAT"
        else:
            direction = "DOWN"
    else:
        direction = "FLAT"

    # ── CONFIDENCE ──────────────────────────────────────────────────────────
    abs_total = abs(total)
    trend_aligned = (direction == "UP" and trend_up) or (direction == "DOWN" and trend_down)
    macd_cross = m5["cross_up"] or m5["cross_down"]
    vol_conf = vr >= 1.5

    if direction == "FLAT":
        conf = 40
    else:
        base = 52
        base += abs_total * 5       # more votes = more confident
        base += 10 if trend_aligned else 0
        base += 8  if macd_cross    else 0
        base += 5  if vol_conf      else 0
        conf = min(base, 94)

    # ── SIGNALS TEXT ────────────────────────────────────────────────────────
    roc5_val = (float(c5.iloc[-1]) - float(c5.iloc[-4])) / float(c5.iloc[-4]) * 100 if len(c5) >= 4 else 0
    sigs = []
    if direction == "UP":
        if trend_up:                sigs.append("Тренд 15м: бычий ↗")
        if m5["cross_up"]:          sigs.append("MACD(5м) кросс вверх 📈")
        elif m5["rising"]:          sigs.append("MACD(5м) растёт")
        if roc5_val > 0.15:         sigs.append(f"Импульс 5м: +{roc5_val:.2f}% за 3 свечи 🚀")
        if rsi5 <= 35:              sigs.append(f"RSI(5м) {rsi5} — перепродан 💡")
        if vr >= 1.8:               sigs.append(f"Объём {vr}x — рост подтверждён 🔥")
        if not sigs:                sigs.append("Бычий импульс по всем таймфреймам")
    elif direction == "DOWN":
        if trend_down:              sigs.append("Тренд 15м: медвежий ↘")
        if m5["cross_down"]:        sigs.append("MACD(5м) кросс вниз 📉")
        elif m5["falling"]:         sigs.append("MACD(5м) падает")
        if roc5_val < -0.15:        sigs.append(f"Импульс 5м: {roc5_val:.2f}% за 3 свечи 📉")
        if rsi5 >= 65:              sigs.append(f"RSI(5м) {rsi5} — перекуплен ⚠️")
        if vr >= 1.8:               sigs.append(f"Объём {vr}x — падение подтверждено 🔥")
        if not sigs:                sigs.append("Медвежий импульс по всем таймфреймам")
    else:
        if trend_neutral:           sigs.append("15м тренд неопределён — боковик")
        elif abs(roc5_val) < 0.05:  sigs.append(f"Импульс слабый ({roc5_val:+.3f}%) — ждём движения")
        else:                       sigs.append("Сигналы противоречат друг другу")

    score = float(total * 10)
    score = max(-100.0, min(100.0, score))
    atr = _atr(df1m)
    levels = _sl_tp(price, atr, direction)

    return {
        "price": price, "score": score, "direction": direction,
        "confidence": conf, "rsi1": rsi1, "rsi5": rsi5,
        "signals": sigs[:3], **levels,
    }


async def get_short_forecast(coin: str) -> dict:
    # Try Kraken first (real 1m/5m candles)
    try:
        df1m, df5m, df15m = await asyncio.gather(
            _kraken_df(coin, 1, 30),
            _kraken_df(coin, 5, 40),
            _kraken_df(coin, 15, 60),
        )
        result = _score(df1m, df5m, df15m)
        result["coin"] = coin
        result["source"] = "kraken"
        try:
            from app.services.leaderboard import log_forecast
            await log_forecast(coin, result["direction"], result["price"])
        except Exception:
            pass
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
        dir_emoji, dir_text = "🟢", "ЛОНГ ↑"
        poly_action = "✅ Polymarket: ставить YES"
    elif direction == "DOWN":
        dir_emoji, dir_text = "🔴", "ШОРТ ↓"
        poly_action = "❌ Polymarket: ставить NO"
    else:
        dir_emoji, dir_text = "⚪", "БОКОВИК ↔"
        poly_action = "⏸ Polymarket: пропусти"

    def _p(v: float) -> str:
        return f"${v:,.2f}" if v >= 1000 else f"${v:.4f}" if v >= 1 else f"${v:.6f}"

    bar = "█" * max(0, min(10, round(conf / 10))) + "░" * (10 - max(0, min(10, round(conf / 10))))
    import html as _html
    sigs_text = "\n".join(f"  • {_html.escape(s)}" for s in sigs) if sigs else "  • Нейтральные условия"
    src_note = "" if source == "kraken" else "\n<i>📡 Данные: технический анализ</i>"

    sl  = f.get("sl")
    tp1 = f.get("tp1")
    tp2 = f.get("tp2")
    lev = f.get("leverage", "")
    slp = f.get("sl_pct", 0)

    if sl and tp1 and tp2 and direction != "FLAT":
        tp1_pct = abs(tp1 - price) / price * 100
        levels_block = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Вход: <b>{_p(price)}</b>\n"
            f"🛑 SL: <b>{_p(sl)}</b>  <i>(-{slp:.2f}%)</i>\n"
            f"🎯 TP1: <b>{_p(tp1)}</b>  <i>(+{tp1_pct:.2f}%)</i>\n"
            f"🎯 TP2: <b>{_p(tp2)}</b>  <i>(1:2)</i>\n"
            f"⚡ Плечо: <b>{lev}</b>\n"
        )
    else:
        levels_block = f"━━━━━━━━━━━━━━━━━━━━\n💵 Цена: <b>{_p(price)}</b>\n"

    return (
        f"{dir_emoji} <b>{coin}/USDT — {dir_text}</b>\n"
        f"⏱ Горизонт: <b>5–10 минут</b>  ·  {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Уверенность: <b>{conf}%</b>  <code>{bar}</code>\n"
        f"{levels_block}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Сигналы:</b>\n{sigs_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{poly_action}\n"
        f"🔗 <a href=\"{POLY_REF}\">Ставить на Polymarket</a>"
        f"{src_note}"
    )
