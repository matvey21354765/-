from __future__ import annotations
import asyncio
import logging
from typing import Optional
import aiohttp
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
SYMBOL_MAP = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Binance public REST — hardcoded, never read from .env
_SPOT_URLS = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
_FUTURES_URL = "https://fapi.binance.com"

_binance_blocked: bool = False

# OKX fallback mappings
_OKX_MAP = {"BTCUSDT": "BTC-USDT", "ETHUSDT": "ETH-USDT", "SOLUSDT": "SOL-USDT"}
_OKX_SWAP_MAP = {"BTCUSDT": "BTC-USDT-SWAP", "ETHUSDT": "ETH-USDT-SWAP", "SOLUSDT": "SOL-USDT-SWAP"}
_OKX_INTERVAL = {"1h": "1H", "4h": "4H", "1d": "1D"}


async def _get(url: str, params: dict = None) -> dict | list:
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
        async with s.get(url, params=params) as r:
            r.raise_for_status()
            return await r.json()


async def _get_spot(path: str, params: dict = None) -> dict | list:
    """Try each Binance spot mirror; on geo-block fall back to OKX immediately."""
    global _binance_blocked
    if _binance_blocked:
        raise RuntimeError("Binance geo-blocked")
    last_err: Exception = RuntimeError("All Binance spot mirrors failed")
    for base in _SPOT_URLS:
        try:
            return await _get(f"{base}{path}", params)
        except aiohttp.ClientResponseError as e:
            if e.status in (451, 403):
                _binance_blocked = True
                raise
            last_err = e
            logger.debug(f"Spot mirror {base} failed: {e}")
        except Exception as e:
            last_err = e
            logger.debug(f"Spot mirror {base} failed: {e}")
    raise last_err


async def _okx_klines(symbol: str, interval: str, limit: int) -> list:
    inst = _OKX_MAP.get(symbol, symbol)
    bar = _OKX_INTERVAL.get(interval, "1H")
    url = "https://www.okx.com/api/v5/market/candles"
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
        async with s.get(url, params={"instId": inst, "bar": bar, "limit": limit}) as r:
            r.raise_for_status()
            d = await r.json()
    # OKX returns newest first; reverse to oldest-first like Binance
    candles = d.get("data", [])
    candles.reverse()
    # Convert to Binance kline format: [open_time,open,high,low,close,volume,...]
    result = []
    for c in candles:
        ts, o, h, l, cl, vol = c[0], c[1], c[2], c[3], c[4], c[5]
        result.append([int(ts), o, h, l, cl, vol, int(ts), vol, 0, vol, vol, "0"])
    return result


async def _okx_ticker(symbol: str) -> dict:
    inst = _OKX_MAP.get(symbol, symbol)
    url = "https://www.okx.com/api/v5/market/ticker"
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
        async with s.get(url, params={"instId": inst}) as r:
            r.raise_for_status()
            d = await r.json()
    t = d["data"][0]
    open24 = float(t.get("open24h") or t["last"])
    last = float(t["last"])
    change_pct = (last - open24) / open24 * 100 if open24 else 0
    return {
        "lastPrice": t["last"],
        "priceChangePercent": str(round(change_pct, 2)),
        "quoteVolume": t.get("volCcy24h", "0"),
        "highPrice": t.get("high24h", t["last"]),
        "lowPrice": t.get("low24h", t["last"]),
    }


async def fetch_klines(symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    try:
        raw = await _get_spot("/api/v3/klines",
                              {"symbol": symbol, "interval": interval, "limit": limit})
    except Exception:
        raw = await _okx_klines(symbol, interval, limit)
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_vol", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ("open", "high", "low", "close", "volume", "quote_vol", "taker_buy_base", "taker_buy_quote"):
        df[c] = df[c].astype(float)
    return df


async def fetch_ticker(symbol: str) -> dict:
    try:
        return await _get_spot("/api/v3/ticker/24hr", {"symbol": symbol})
    except Exception:
        return await _okx_ticker(symbol)


async def fetch_funding_rate(symbol: str) -> float:
    try:
        d = await _get(f"{_FUTURES_URL}/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(d.get("lastFundingRate", 0))
    except Exception:
        return 0.0


async def fetch_open_interest(symbol: str) -> float:
    try:
        d = await _get(f"{_FUTURES_URL}/fapi/v1/openInterest", {"symbol": symbol})
        return float(d.get("openInterest", 0))
    except Exception:
        return 0.0


async def fetch_long_short_ratio(symbol: str) -> float:
    try:
        d = await _get(f"{_FUTURES_URL}/futures/data/globalLongShortAccountRatio",
                       {"symbol": symbol, "period": "1h", "limit": 1})
        return float(d[0]["longShortRatio"]) if d else 1.0
    except Exception:
        return 1.0


async def fetch_fear_greed() -> int:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get("https://api.alternative.me/fng/?limit=1") as r:
                d = await r.json()
                return int(d["data"][0]["value"])
    except Exception:
        return 50


def calc_rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    v = rsi.iloc[-1]
    return round(float(v) if not np.isnan(v) else 50.0, 2)


def calc_macd(closes: pd.Series) -> dict:
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    return {
        "macd": round(float(macd.iloc[-1]), 6),
        "signal": round(float(signal.iloc[-1]), 6),
        "histogram": round(float(hist.iloc[-1]), 6),
        "bullish_cross": float(hist.iloc[-1]) > 0 and float(hist.iloc[-2]) <= 0,
        "bearish_cross": float(hist.iloc[-1]) < 0 and float(hist.iloc[-2]) >= 0,
        "bullish": float(hist.iloc[-1]) > float(hist.iloc[-2]),
    }


def calc_ema(closes: pd.Series, period: int) -> float:
    return round(float(closes.ewm(span=period, adjust=False).mean().iloc[-1]), 4)


def calc_bollinger(closes: pd.Series, period: int = 20) -> dict:
    sma = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    upper = sma + 2 * std
    lower = sma - 2 * std
    c = closes.iloc[-1]
    rng = float(upper.iloc[-1] - lower.iloc[-1])
    pos = float((c - lower.iloc[-1]) / rng * 100) if rng > 0 else 50.0
    bw = float(rng / sma.iloc[-1] * 100)
    return {
        "upper": round(float(upper.iloc[-1]), 4),
        "middle": round(float(sma.iloc[-1]), 4),
        "lower": round(float(lower.iloc[-1]), 4),
        "bandwidth": round(bw, 3),
        "position_pct": round(pos, 2),
        "squeeze": bw < 3.0,
    }


def calc_stochastic(df: pd.DataFrame, k: int = 14, d: int = 3) -> dict:
    lo = df["low"].rolling(k).min()
    hi = df["high"].rolling(k).max()
    pct_k = 100 * (df["close"] - lo) / (hi - lo)
    pct_d = pct_k.rolling(d).mean()
    return {
        "k": round(float(pct_k.iloc[-1]), 2),
        "d": round(float(pct_d.iloc[-1]), 2),
        "overbought": float(pct_k.iloc[-1]) > 80,
        "oversold": float(pct_k.iloc[-1]) < 20,
    }


def calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    v = tr.rolling(period).mean().iloc[-1]
    return round(float(v), 4)


def calc_adx(df: pd.DataFrame, period: int = 14) -> dict:
    hi, lo = df["high"], df["low"]
    up_move = hi.diff()
    dn_move = -lo.diff()
    plus_dm = up_move.where((up_move > dn_move) & (up_move > 0), 0.0)
    minus_dm = dn_move.where((dn_move > up_move) & (dn_move > 0), 0.0)
    atr_s = calc_atr(df, period)
    sp = plus_dm.rolling(period).mean()
    sm = minus_dm.rolling(period).mean()
    plus_di = 100 * sp / (atr_s + 1e-10)
    minus_di = 100 * sm / (atr_s + 1e-10)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    adx = dx.rolling(period).mean()
    val = float(adx.iloc[-1]) if not np.isnan(adx.iloc[-1]) else 25.0
    return {
        "adx": round(val, 2),
        "plus_di": round(float(plus_di.iloc[-1]), 2),
        "minus_di": round(float(minus_di.iloc[-1]), 2),
        "strong_trend": val > 25,
        "very_strong": val > 40,
    }


def calc_volume(df: pd.DataFrame) -> dict:
    avg20 = df["volume"].rolling(20).mean().iloc[-1]
    cur = df["volume"].iloc[-1]
    ratio = float(cur / avg20) if avg20 > 0 else 1.0
    r5 = df["volume"].tail(5).mean()
    p5 = df["volume"].iloc[-10:-5].mean()
    trend = "increasing" if r5 > p5 * 1.1 else "decreasing" if r5 < p5 * 0.9 else "stable"
    buy_vol = df["taker_buy_base"].tail(10).sum()
    total = df["volume"].tail(10).sum()
    buy_ratio = float(buy_vol / total * 100) if total > 0 else 50.0
    obv = (np.sign(df["close"].diff()) * df["volume"]).cumsum()
    obv_trend = "up" if float(obv.iloc[-1]) > float(obv.iloc[-5]) else "down"
    return {
        "ratio": round(ratio, 2),
        "trend": trend,
        "buy_ratio_pct": round(buy_ratio, 1),
        "obv_trend": obv_trend,
        "high_volume": ratio > 1.5,
    }


def find_levels(df: pd.DataFrame, n: int = 4) -> tuple[list, list]:
    win = 5
    highs = df["high"].values
    lows = df["low"].values
    price = float(df["close"].iloc[-1])
    p_hi, p_lo = [], []
    for i in range(win, len(df) - win):
        if highs[i] == max(highs[i - win:i + win + 1]):
            p_hi.append(highs[i])
        if lows[i] == min(lows[i - win:i + win + 1]):
            p_lo.append(lows[i])
    supports = sorted([x for x in p_lo if x < price], reverse=True)[:n]
    resistances = sorted([x for x in p_hi if x > price])[:n]
    return [round(x, 4) for x in supports], [round(x, 4) for x in resistances]


def calc_volatility(df: pd.DataFrame, days: int) -> float:
    if len(df) < days + 1:
        return 0.0
    ret = df["close"].pct_change().dropna().tail(days)
    return round(float(ret.std() * np.sqrt(365) * 100), 2)


def detect_patterns(df: pd.DataFrame) -> list[str]:
    patterns = []
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = abs(c - o)
    wick_top = h - c.where(c > o, o)
    wick_bot = c.where(c > o, o) - l
    if len(df) >= 2:
        lb = float(body.iloc[-1])
        lwt = float(wick_top.iloc[-1])
        lwb = float(wick_bot.iloc[-1])
        if lwb > lb * 2 and lwt < lb * 0.5:
            patterns.append("Hammer — потенциальный разворот вверх")
        if lwt > lb * 2 and lwb < lb * 0.5:
            patterns.append("Shooting Star — потенциальный разворот вниз")
    if len(df) >= 1:
        lb = float(body.iloc[-1])
        lr = float(h.iloc[-1] - l.iloc[-1])
        if lr > 0 and lb / lr < 0.1:
            patterns.append("Doji — нерешительность рынка")
    if len(df) >= 3:
        l3c = c.iloc[-3:]
        l3o = o.iloc[-3:]
        if all(l3c.values > l3o.values):
            patterns.append("3 бычьих свечи подряд")
        if all(l3c.values < l3o.values):
            patterns.append("3 медвежьих свечи подряд")
    return patterns


def calc_indicators(df: pd.DataFrame) -> dict:
    closes = df["close"]
    return {
        "rsi": calc_rsi(closes),
        "macd": calc_macd(closes),
        "ema_20": calc_ema(closes, 20),
        "ema_50": calc_ema(closes, 50),
        "ema_200": calc_ema(closes, 200),
        "bollinger": calc_bollinger(closes),
        "stochastic": calc_stochastic(df),
        "atr": calc_atr(df),
        "adx": calc_adx(df),
    }


async def get_full_snapshot(coin: str) -> dict:
    symbol = SYMBOL_MAP.get(coin, coin + "USDT")
    ticker, df_1h, df_4h, df_1d, funding, oi, ls_ratio, fg = await asyncio.gather(
        fetch_ticker(symbol),
        fetch_klines(symbol, "1h", 200),
        fetch_klines(symbol, "4h", 100),
        fetch_klines(symbol, "1d", 90),
        fetch_funding_rate(symbol),
        fetch_open_interest(symbol),
        fetch_long_short_ratio(symbol),
        fetch_fear_greed(),
    )
    price = float(ticker["lastPrice"])
    i1h = calc_indicators(df_1h)
    i4h = calc_indicators(df_4h)
    i1d = calc_indicators(df_1d)
    supports, resistances = find_levels(df_1d)
    vol = calc_volume(df_1h)
    patterns = detect_patterns(df_1h)
    return {
        "coin": coin, "symbol": symbol, "price": price,
        "change_24h": float(ticker["priceChangePercent"]),
        "volume_24h": float(ticker["quoteVolume"]),
        "high_24h": float(ticker["highPrice"]),
        "low_24h": float(ticker["lowPrice"]),
        "funding_rate": funding, "open_interest": oi,
        "oi_trend": "growing", "long_short_ratio": ls_ratio,
        "fear_greed": fg, "i1h": i1h, "i4h": i4h, "i1d": i1d,
        "supports": supports, "resistances": resistances,
        "volume": vol, "patterns": patterns,
        "volatility_7d": calc_volatility(df_1d, 7),
        "volatility_30d": calc_volatility(df_1d, 30),
    }
