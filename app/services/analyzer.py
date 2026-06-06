from __future__ import annotations
import json
import logging
import re
import time
from typing import Optional
import aiohttp
from config.settings import settings

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

_groq_blocked_until: float = 0.0

REQUIRED = {
    "direction", "confidence", "signal_rating", "entry_price",
    "stop_loss", "take_profit_1", "take_profit_2", "take_profit_3",
    "risk_reward", "reasons", "full_analysis",
}


def _f(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}"


def build_prompt(snap: dict) -> str:
    c = snap["coin"]
    p = snap["price"]
    i1 = snap["i1h"]
    i4 = snap["i4h"]
    i1d = snap["i1d"]
    vol = snap["volume"]
    bb = i1["bollinger"]
    macd = i1["macd"]
    adx = i1["adx"]
    stoch = i1["stochastic"]
    atr = i1["atr"]
    funding_pct = snap["funding_rate"] * 100
    sup_str = ", ".join(f"${_f(s)}" for s in snap["supports"]) or "нет"
    res_str = ", ".join(f"${_f(r)}" for r in snap["resistances"]) or "нет"

    # Pre-calculate SL/TP suggestions so model doesn't need to do math
    if p > i1["ema_50"]:
        suggested_sl = round(p - atr * 1.5, 2)
        suggested_tp1 = round(p + atr * 2, 2)
        suggested_tp2 = round(p + atr * 4, 2)
        suggested_tp3 = round(p + atr * 6, 2)
    else:
        suggested_sl = round(p + atr * 1.5, 2)
        suggested_tp1 = round(p - atr * 2, 2)
        suggested_tp2 = round(p - atr * 4, 2)
        suggested_tp3 = round(p - atr * 6, 2)

    return f"""Ты — трейдинговый аналитик. Дай торговый сигнал для {c}/USDT.

ДАННЫЕ:
Цена: {p} | Изм.24ч: {snap['change_24h']:+.2f}%
RSI(1H): {i1['rsi']} | RSI(4H): {i4['rsi']} | RSI(1D): {i1d['rsi']}
MACD(1H): {macd['macd']:+.4f} hist={macd['histogram']:+.4f}
EMA20={i1['ema_20']} EMA50={i1['ema_50']} EMA200={i1['ema_200']}
Цена {'ВЫШЕ' if p > i1['ema_50'] else 'НИЖЕ'} EMA50 | {'ВЫШЕ' if p > i1['ema_200'] else 'НИЖЕ'} EMA200
ATR={atr} | ADX={adx['adx']} | Stoch K={stoch['k']} D={stoch['d']}
BB pos={bb['position_pct']:.1f}%
Funding: {funding_pct:+.4f}% | L/S: {snap['long_short_ratio']:.2f}
Fear&Greed: {snap['fear_greed']}/100
Объём/ср: {vol['ratio']:.2f}x | Покупки: {vol['buy_ratio_pct']}%
Поддержки: {sup_str}
Сопротивления: {res_str}
Волатильность 7д: {snap['volatility_7d']}%

ПРЕДЛАГАЕМЫЕ УРОВНИ (можешь скорректировать):
SL: {suggested_sl} | TP1: {suggested_tp1} | TP2: {suggested_tp2} | TP3: {suggested_tp3}

ПРАВИЛА:
- direction: только "LONG" или "SHORT"
- ВСЕ числовые значения — только готовые числа, НИКАКИХ формул или выражений
- stop_loss, take_profit_1/2/3 — конкретные числа типа 61500.00
- reasons: 3 строки с реальными числами из данных
- full_analysis: 2-3 предложения простым языком

Ответь JSON-объектом со следующими ключами:
direction, confidence, signal_rating, trend_strength, prob_up, prob_down,
entry_price, entry_type, stop_loss, take_profit_1, take_profit_2, take_profit_3,
risk_reward, sl_distance_pct, timeframe, reasons, bull_scenario, bear_scenario,
key_trigger, full_analysis"""


def _parse(raw: str) -> Optional[dict]:
    raw = re.sub(r"^```[a-z]*\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None


def _validate(data: dict, snap: dict) -> dict:
    price = snap["price"]
    if data.get("direction") not in ("LONG", "SHORT"):
        data["direction"] = "LONG" if snap["i1h"]["rsi"] < 50 else "SHORT"
    for field in ("entry_price", "stop_loss", "take_profit_1", "take_profit_2", "take_profit_3"):
        v = data.get(field)
        # Reject if not a plain number (e.g. expression string)
        if not isinstance(v, (int, float)) or v <= 0:
            data[field] = price
    if not isinstance(data.get("signal_rating"), int):
        data["signal_rating"] = 5
    data["signal_rating"] = max(1, min(10, data["signal_rating"]))
    if not isinstance(data.get("reasons"), list):
        data["reasons"] = [str(data.get("reasons", "—"))]
    conf = float(data.get("confidence", 60))
    data["confidence"] = max(40.0, min(85.0, conf))
    for field in ("full_analysis", "bull_scenario", "bear_scenario", "key_trigger"):
        v = data.get(field)
        if isinstance(v, list):
            data[field] = " ".join(str(x) for x in v)
        elif v is not None:
            data[field] = str(v)
    valid_trends = {"STRONG BULL", "WEAK BULL", "NEUTRAL", "WEAK BEAR", "STRONG BEAR"}
    if data.get("trend_strength") not in valid_trends:
        rsi = snap["i1h"]["rsi"]
        if rsi >= 65: data["trend_strength"] = "STRONG BULL"
        elif rsi >= 55: data["trend_strength"] = "WEAK BULL"
        elif rsi <= 35: data["trend_strength"] = "STRONG BEAR"
        elif rsi <= 45: data["trend_strength"] = "WEAK BEAR"
        else: data["trend_strength"] = "NEUTRAL"
    valid_tf = {"4-12 часов", "1-3 дня", "3-7 дней", "4-12ч", "1-3д", "3-7д"}
    if data.get("timeframe") not in valid_tf:
        data["timeframe"] = "4-12 часов"
    return data


async def analyze_coin(snap: dict) -> Optional[dict]:
    global _groq_blocked_until
    if time.time() < _groq_blocked_until:
        logger.warning(f"Groq blocked for {_groq_blocked_until - time.time():.0f}s")
        return None

    key = settings.GROQ_API_KEY
    if not key or key in ("", "ВСТАВЬ_СВОЙ_КЛЮЧ_СЮДА"):
        logger.error("GROQ_API_KEY not set")
        return None

    prompt = build_prompt(snap)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},  # forces valid JSON
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.post(GROQ_URL, headers=headers, json=payload) as r:
                body = await r.text()
                if r.status == 429:
                    _groq_blocked_until = time.time() + 60
                    logger.warning("Groq 429 — blocked 60s")
                    return None
                if r.status != 200:
                    logger.error(f"Groq HTTP {r.status}: {body[:300]}")
                    return None
                result = json.loads(body)

        raw = result["choices"][0]["message"]["content"]
        logger.info(f"[{snap['coin']}] Groq OK, length={len(raw)}")

        data = _parse(raw)
        if data is None:
            logger.error(f"[{snap['coin']}] JSON parse failed: {raw[:200]}")
            return None

        missing = REQUIRED - set(data.keys())
        if missing:
            logger.warning(f"[{snap['coin']}] Missing fields: {missing}, filling defaults")
            for f in missing:
                data[f] = "" if f in ("reasons", "full_analysis") else 0

        return _validate(data, snap)

    except Exception as e:
        logger.error(f"[{snap['coin']}] Groq exception: {type(e).__name__}: {e}")
        return None
