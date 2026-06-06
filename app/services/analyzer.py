from __future__ import annotations
import json
import logging
import re
from typing import Optional
import aiohttp
from config.settings import settings

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

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
    funding_pct = snap["funding_rate"] * 100
    sup_str = " | ".join(f"${_f(s)}" for s in snap["supports"]) or "нет данных"
    res_str = " | ".join(f"${_f(r)}" for r in snap["resistances"]) or "нет данных"
    patterns_str = "\n".join(f"  - {pt}" for pt in snap["patterns"]) if snap["patterns"] else "  - Нет явных паттернов"

    return f"""Ты — старший квантовый аналитик DAO-фонда. Специализация: поиск сделок с максимальным математическим ожиданием.
Твой принцип: лучше пропустить 10 сигналов, чем войти в одну плохую сделку.

РЫНОЧНЫЙ СНИМОК: {c}/USDT
Цена: ${_f(p)} | Изм.24ч: {snap['change_24h']:+.2f}%
Диапазон 24ч: ${_f(snap['low_24h'])} — ${_f(snap['high_24h'])}
Объём 24ч: ${_f(snap['volume_24h']/1e6, 1)}M
Волатильность: 7д={snap['volatility_7d']}% | 30д={snap['volatility_30d']}%

ИНДИКАТОРЫ 1H:
RSI={i1['rsi']} | MACD={macd['macd']:+.4f} hist={macd['histogram']:+.4f} {'БЫЧЬЕ ПЕРЕСЕЧЕНИЕ' if macd['bullish_cross'] else 'МЕДВЕЖЬЕ ПЕРЕСЕЧЕНИЕ' if macd['bearish_cross'] else ''}
EMA20={_f(i1['ema_20'])} | EMA50={_f(i1['ema_50'])} | EMA200={_f(i1['ema_200'])}
Цена vs EMA50: {'ВЫШЕ' if p > i1['ema_50'] else 'НИЖЕ'} | vs EMA200: {'ВЫШЕ' if p > i1['ema_200'] else 'НИЖЕ'}
BB: pos={bb['position_pct']:.1f}% bw={bb['bandwidth']:.2f}% {'СЖАТИЕ' if bb['squeeze'] else ''}
Stoch: K={stoch['k']} D={stoch['d']} {'ПЕРЕКУПЛЕН' if stoch['overbought'] else 'ПЕРЕПРОДАН' if stoch['oversold'] else ''}
ATR={_f(i1['atr'])} | ADX={adx['adx']} +DI={adx['plus_di']} -DI={adx['minus_di']} {'СИЛЬНЫЙ ТРЕНД' if adx['strong_trend'] else 'ФЛЕТ'}

ИНДИКАТОРЫ 4H:
RSI={i4['rsi']} | MACD hist={i4['macd']['histogram']:+.4f} | ADX={i4['adx']['adx']}
EMA50={_f(i4['ema_50'])} | EMA200={_f(i4['ema_200'])}

ИНДИКАТОРЫ 1D:
RSI={i1d['rsi']} | MACD hist={i1d['macd']['histogram']:+.4f} | ADX={i1d['adx']['adx']}
EMA50={_f(i1d['ema_50'])} | EMA200={_f(i1d['ema_200'])}

ПАТТЕРНЫ СВЕЧЕЙ:
{patterns_str}

УРОВНИ:
Сопротивления: {res_str}
Поддержки: {sup_str}

ДЕРИВАТИВЫ:
Funding Rate: {funding_pct:+.4f}% {'ЛОНГИ ПЕРЕГРЕТЫ' if funding_pct > 0.05 else 'ШОРТЫ ПЕРЕГРЕТЫ' if funding_pct < -0.01 else 'НЕЙТРАЛЬНО'}
Open Interest: {snap['open_interest']:,.0f} | L/S Ratio: {snap['long_short_ratio']:.2f}

ОБЪЁМ:
Текущий/Ср.20: {vol['ratio']:.2f}x ({vol['trend']}) | Покупки: {vol['buy_ratio_pct']}% | OBV: {vol['obv_trend']}

НАСТРОЕНИЯ:
Fear & Greed: {snap['fear_greed']}/100 {'ЖАДНОСТЬ' if snap['fear_greed'] > 60 else 'СТРАХ' if snap['fear_greed'] < 40 else 'НЕЙТРАЛЬНО'}

ЗАДАЧА:
1. Проведи полный мультитаймфреймовый анализ (1H + 4H + 1D)
2. Найди сетап с R/R >= 1.8 и уверенностью >= 55%
3. Если сетапа нет — верни NO TRADE
4. SL — за ближайшим уровнем + буфер ATR*0.5
5. TP должны совпадать с реальными уровнями
6. full_analysis пиши на русском языке, 4-5 абзацев

Верни ТОЛЬКО JSON без какого-либо текста до или после:

{{
  "direction": "LONG или SHORT или NO TRADE",
  "confidence": число от 0 до 100,
  "signal_rating": число от 1 до 10,
  "trend_strength": "STRONG BULL или WEAK BULL или NEUTRAL или WEAK BEAR или STRONG BEAR",
  "prob_up": число от 0 до 100,
  "prob_down": число от 0 до 100,
  "entry_price": число,
  "entry_type": "MARKET или LIMIT",
  "stop_loss": число,
  "take_profit_1": число,
  "take_profit_2": число,
  "take_profit_3": число,
  "risk_reward": число,
  "sl_distance_pct": число,
  "timeframe": "4-12 часов или 1-3 дня или 3-7 дней",
  "reasons": ["причина 1", "причина 2", "причина 3", "причина 4", "причина 5"],
  "bull_scenario": "что нужно для роста",
  "bear_scenario": "что сломает структуру",
  "key_trigger": "ключевой уровень или событие",
  "full_analysis": "4-5 абзацев на русском: структура рынка, индикаторы, объёмы, позиционирование участников, итог и рекомендация. Без markdown."
}}"""


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
    confidence = float(data.get("confidence", 0))
    rr = float(data.get("risk_reward", 0))
    if data.get("direction") != "NO TRADE":
        if confidence < settings.MIN_CONFIDENCE or rr < settings.MIN_RR:
            data["direction"] = "NO TRADE"
    for field in ("entry_price", "stop_loss", "take_profit_1", "take_profit_2", "take_profit_3"):
        if not isinstance(data.get(field), (int, float)) or data[field] <= 0:
            data[field] = price
    if not isinstance(data.get("signal_rating"), int):
        data["signal_rating"] = 5
    data["signal_rating"] = max(1, min(10, data["signal_rating"]))
    if not isinstance(data.get("reasons"), list):
        data["reasons"] = [str(data.get("reasons", "—"))]
    return data


async def analyze_coin(snap: dict) -> Optional[dict]:
    headers = {
        "Authorization": f"Bearer {settings.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": build_prompt(snap)}],
        "max_tokens": 2048,
        "temperature": 0.3,
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.post(GROQ_URL, headers=headers, json=payload) as r:
                r.raise_for_status()
                result = await r.json()
        raw = result["choices"][0]["message"]["content"]
        data = _parse(raw)
        if data is None:
            logger.error(f"[{snap['coin']}] JSON parse failed. Raw: {raw[:400]}")
            return None
        missing = REQUIRED - set(data.keys())
        if missing:
            logger.error(f"[{snap['coin']}] Missing: {missing}")
            return None
        return _validate(data, snap)
    except Exception as e:
        logger.error(f"[{snap['coin']}] Groq error: {e}")
        return None
