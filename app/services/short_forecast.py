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
    rows = [[float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[6])] for c in raw[-limit:]]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    return df


def _rsi(closes: pd.Series, p: int = 14) -> float:
    d = closes.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    v = (100 - 100 / (1 + g / l.replace(0, np.nan))).iloc[-1]
    return round(float(v) if not np.isnan(v) else 50.0, 1)


def _ema(closes: pd.Series, p: int) -> float:
    return float(closes.ewm(span=p, adjust=False).mean().iloc[-1])


def _vol_ratio(df: pd.DataFrame) -> float:
    avg = df["volume"].rolling(20).mean().iloc[-1]
    return round(float(df["volume"].iloc[-1] / avg), 2) if avg > 0 else 1.0


def _atr(df: pd.DataFrame, p: int = 14) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return float(tr.rolling(p).mean().iloc[-1])


def _sl_tp(price: float, atr: float, direction: str) -> dict:
    sl_dist = atr * 1.5
    sl_pct = sl_dist / price * 100
    if direction == "UP":
        sl  = price - sl_dist
        tp1 = price + atr * 1.5
        tp2 = price + atr * 3.0
    elif direction == "DOWN":
        sl  = price + sl_dist
        tp1 = price - atr * 1.5
        tp2 = price - atr * 3.0
    else:
        return {}
    lev = "3–5x" if sl_pct > 1.0 else "5–10x" if sl_pct > 0.5 else "10–20x"
    return {"sl": round(sl, 2), "tp1": round(tp1, 2), "tp2": round(tp2, 2),
            "sl_pct": round(sl_pct, 2), "leverage": lev}


def _macd(closes: pd.Series) -> dict:
    e12 = closes.ewm(span=12, adjust=False).mean()
    e26 = closes.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    sig  = macd.ewm(span=9, adjust=False).mean()
    hist = macd - sig
    h0, h1, h2 = float(hist.iloc[-1]), float(hist.iloc[-2]), float(hist.iloc[-3])
    price = float(closes.iloc[-1])
    mag = abs(h0) / price * 10000
    return {
        "cross_up":   h0 > 0 > h1,
        "cross_down": h0 < 0 < h1,
        "rising":     h0 > h1 > h2,
        "falling":    h0 < h1 < h2,
        "bullish":    h0 > 0,
        "magnitude":  mag,
    }


def _score(df1m: pd.DataFrame, df5m: pd.DataFrame, df15m: pd.DataFrame) -> dict:
    c1, c5, c15 = df1m["close"], df5m["close"], df15m["close"]
    price = float(c1.iloc[-1])
    rsi5  = _rsi(c5, 14)
    rsi15 = _rsi(c15, 14)
    vr    = _vol_ratio(df5m)

    # ── FLAT: dead market ───────────────────────────────────────────────────
    atr5    = _atr(df5m, 14)
    atr_pct = atr5 / price * 100
    if atr_pct < 0.07:
        return {"price": price, "score": 0.0, "direction": "FLAT", "confidence": 38,
                "rsi1": rsi5, "rsi5": rsi5,
                "signals": [f"Флэт — ATR {atr_pct:.3f}%, рынок без движения"]}

    m5  = _macd(c5)
    m15 = _macd(c15)

    # 15m trend
    ema21_15 = _ema(c15, 21)
    ema9_15  = _ema(c15, 9)
    trend_bull = price > ema21_15 * 1.0003 and ema9_15 > ema21_15
    trend_bear = price < ema21_15 * 0.9997 and ema9_15 < ema21_15

    # 5m candle majority (last 6)
    c5v = df5m["close"].iloc[-6:].values
    o5v = df5m["open"].iloc[-6:].values
    bulls = sum(1 for c, o in zip(c5v, o5v) if c > o)
    majority_bull = bulls >= 4
    majority_bear = bulls <= 2

    # 1m momentum (last 3)
    c1v = df1m["close"].iloc[-4:].values
    o1v = df1m["open"].iloc[-4:].values
    last3_bull = all(c1v[i] > o1v[i] for i in range(1, 4))
    last3_bear = all(c1v[i] < o1v[i] for i in range(1, 4))
    accel_up   = last3_bull and c1v[3] > c1v[2] > c1v[1]
    accel_down = last3_bear and c1v[3] < c1v[2] < c1v[1]

    roc5 = (float(c5.iloc[-1]) - float(c5.iloc[-4])) / float(c5.iloc[-4]) * 100 if len(c5) >= 4 else 0

    # ── STRICT GATE ─────────────────────────────────────────────────────────
    # Require: 5m MACD cross OR (15m MACD cross AND 5m MACD aligned)
    # AND 15m trend must agree with direction
    cross_up_strong   = m5["cross_up"] or (m15["cross_up"]   and m5["bullish"])
    cross_down_strong = m5["cross_down"] or (m15["cross_down"] and not m5["bullish"])

    rsi_oversold  = rsi5 <= 25
    rsi_overbought = rsi5 >= 75

    anchor_bull = (cross_up_strong   or rsi_oversold)  and trend_bull
    anchor_bear = (cross_down_strong or rsi_overbought) and trend_bear

    if not anchor_bull and not anchor_bear:
        return {"price": price, "score": 0.0, "direction": "FLAT", "confidence": 40,
                "rsi1": rsi5, "rsi5": rsi5,
                "signals": ["Нет подтверждённого сигнала — ждём"]}

    # ── VOTES (max ±9) ──────────────────────────────────────────────────────
    if m5["cross_up"]:                                              macd_vote = 3
    elif m5["bullish"] and m5["rising"] and m5["magnitude"] > 0.1: macd_vote = 2
    elif m5["bullish"]:                                             macd_vote = 1
    elif m5["cross_down"]:                                          macd_vote = -3
    elif not m5["bullish"] and m5["falling"] and m5["magnitude"] > 0.1: macd_vote = -2
    elif not m5["bullish"]:                                         macd_vote = -1
    else:                                                           macd_vote = 0

    trend_vote   = 2 if trend_bull else -2 if trend_bear else 0
    candle_vote  = 2 if majority_bull else -2 if majority_bear else 0
    mom_vote     = 1 if (accel_up or last3_bull) else -1 if (accel_down or last3_bear) else 0
    roc_vote     = 1 if roc5 > 0.15 else -1 if roc5 < -0.15 else 0

    total = macd_vote + trend_vote + candle_vote + mom_vote + roc_vote  # max ±9

    # ── DECISION: strict confluence ─────────────────────────────────────────
    # Block UP if RSI overbought; block DOWN if RSI oversold
    # Require total >= 6 (was 5) for even higher quality
    if total >= 6 and anchor_bull and rsi5 < 70:
        direction = "UP"
    elif total <= -6 and anchor_bear and rsi5 > 30:
        direction = "DOWN"
    else:
        direction = "FLAT"

    # ── CONFIDENCE ──────────────────────────────────────────────────────────
    if direction == "FLAT":
        conf = 40
    else:
        base  = 58 + abs(total) * 4
        base += 8 if (m5["cross_up"] or m5["cross_down"]) else 0
        base += 5 if vr >= 1.2 else 0
        base += 5 if (rsi15 < 40 and direction == "UP") or (rsi15 > 60 and direction == "DOWN") else 0
        conf  = min(base, 93)

    # ── SIGNALS TEXT ────────────────────────────────────────────────────────
    sigs = []
    if direction == "UP":
        if m5["cross_up"]:      sigs.append("MACD(5м) кросс вверх 📈")
        elif m15["cross_up"]:   sigs.append("MACD(15м) кросс вверх + 5м бычий 📈")
        if trend_bull:          sigs.append("Тренд вверх: EMA9 > EMA21 (15м) ↗")
        if majority_bull:       sigs.append(f"{bulls}/6 свечей 5м зелёные 🟢")
        if accel_up:            sigs.append("3 ускоряющихся 1м свечи 🚀")
        elif last3_bull:        sigs.append("3 бычьих 1м свечи подряд 🟢")
        if roc5 > 0.15:         sigs.append(f"ROC 5м: +{roc5:.2f}%")
        if rsi_oversold:        sigs.append(f"RSI(5м) {rsi5} — перепродан 💡")
    elif direction == "DOWN":
        if m5["cross_down"]:    sigs.append("MACD(5м) кросс вниз 📉")
        elif m15["cross_down"]: sigs.append("MACD(15м) кросс вниз + 5м медвежий 📉")
        if trend_bear:          sigs.append("Тренд вниз: EMA9 < EMA21 (15м) ↘")
        if majority_bear:       sigs.append(f"{6-bulls}/6 свечей 5м красные 🔴")
        if accel_down:          sigs.append("3 ускоряющихся 1м свечи вниз 📉")
        elif last3_bear:        sigs.append("3 медвежьих 1м свечи подряд 🔴")
        if roc5 < -0.15:        sigs.append(f"ROC 5м: {roc5:.2f}%")
        if rsi_overbought:      sigs.append(f"RSI(5м) {rsi5} — перекуплен ⚠️")
    else:
        sigs.append(f"Скор {total:+d}/±9 — нет чёткого сигнала")

    atr1   = _atr(df1m)
    levels = _sl_tp(price, atr1, direction)

    return {
        "price": price, "score": float(total * 10), "direction": direction,
        "confidence": conf, "rsi1": rsi5, "rsi5": rsi5,
        "signals": sigs[:3], **levels,
    }


async def get_short_forecast(coin: str) -> dict:
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

    return await _forecast_from_db(coin)


async def _forecast_from_db(coin: str) -> dict:
    import aiohttp
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

    try:
        from app.services.signal_service import get_recent_signals
        sigs = await get_recent_signals(coin=coin, limit=1)
    except Exception:
        sigs = []

    score, signals_list = 0.0, []
    if sigs:
        sig = sigs[0]
        rsi = sig.rsi_1h or 50.0
        if rsi >= 70:   score -= 22; signals_list.append(f"RSI {rsi:.0f} — перекуплен ⚠️")
        elif rsi <= 30: score += 22; signals_list.append(f"RSI {rsi:.0f} — перепродан 💡")
        if sig.direction == "LONG":
            score += 18; signals_list.append(f"Последний сигнал: ЛОНГ {sig.confidence:.0f}% 📈")
        elif sig.direction == "SHORT":
            score -= 18; signals_list.append(f"Последний сигнал: ШОРТ {sig.confidence:.0f}% 📉")
        if not price and sig.entry_price:
            price = sig.entry_price
        if change > 3:    score += 10; signals_list.append(f"Рост +{change:.1f}% за 24ч 📈")
        elif change < -3: score -= 10; signals_list.append(f"Падение {change:.1f}% за 24ч 📉")
    else:
        if change > 2:    score += 15; signals_list.append(f"Рост +{change:.1f}% за 24ч 📈")
        elif change < -2: score -= 15; signals_list.append(f"Падение {change:.1f}% за 24ч 📉")
        else:             signals_list.append("Нет данных — используй BTC/ETH/SOL для точного прогноза")

    score = max(-100.0, min(100.0, score))
    conf  = min(int(abs(score) * 0.45 + 45), 88)
    direction = "UP" if score > 8 else "DOWN" if score < -8 else "FLAT"
    return {
        "coin": coin, "price": price, "change_24h": change,
        "score": score, "direction": direction, "confidence": conf,
        "rsi1": sigs[0].rsi_1h if sigs else 50.0,
        "rsi5": 50.0, "signals": signals_list[:3], "source": "db",
    }


def format_forecast(f: dict) -> str:
    import html as _html
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
    sigs_text = "\n".join(f"  • {_html.escape(s)}" for s in sigs) if sigs else "  • Нейтральные условия"
    src_note = "" if source == "kraken" else "\n<i>📡 Данные: технический анализ</i>"

    sl  = f.get("sl"); tp1 = f.get("tp1"); tp2 = f.get("tp2")
    lev = f.get("leverage", ""); slp = f.get("sl_pct", 0)

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
