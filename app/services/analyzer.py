from __future__ import annotations
import json
import logging
import re
import time
from typing import Optional
import aiohttp
from config.settings import settings

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "meta-llama/llama-3.1-8b-instruct:free"

_groq_blocked_until: float = 0.0

REQUIRED = {
    "direction", "confidence", "signal_rating", "entry_price",
    "stop_loss", "take_profit_1", "take_profit_2", "take_profit_3",
    "risk_reward", "reasons", "full_analysis",
}


def _key_valid(key: str) -> bool:
    return bool(key) and key not in ("", "ВСТАВЬ_СВОЙ_КЛЮЧ_СЮДА", "YOUR_KEY_HERE", "None", "none")


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

    return f"""Ты — старший квантовый аналитик крипто-рынка. Твоя задача: дать чёткий торговый сигнал на основе реальных данных.

ВАЖНО: Ты ОБЯЗАН выдать LONG или SHORT. Никогда не пиши NO TRADE. Каждая монета должна иметь уникальный анализ с конкретными числами из данных ниже.

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

ПРАВИЛА:
- direction: ТОЛЬКО "LONG" или "SHORT" — никогда NO TRADE
- confidence: реальное число 40-85, основанное на силе сигнала (не всегда 55!)
- reasons: 3 конкретные причины с реальными числами из данных выше (RSI={i1['rsi']}, цена ${_f(p)}, ATR={_f(i1['atr'])}, и т.д.)
- SL ставь за ближайший уровень поддержки/сопротивления + ATR*0.3 буфер
- TP1/2/3 ставь на реальные уровни из данных выше
- full_analysis — 3 абзаца простым языком для обычного человека, без технических терминов

Верни ТОЛЬКО JSON без какого-либо текста до или после:

{{
  "direction": "LONG или SHORT",
  "confidence": число от 40 до 85,
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
  "reasons": ["конкретная причина 1 с числами", "конкретная причина 2 с числами", "конкретная причина 3 с числами"],
  "bull_scenario": "что нужно для роста",
  "bear_scenario": "что сломает структуру",
  "key_trigger": "ключевой уровень или событие",
  "full_analysis": "2 абзаца простым языком (максимум 200 слов): куда движется рынок и почему, что делать трейдеру. Без технических аббревиатур."
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
    # Force LONG/SHORT only
    if data.get("direction") not in ("LONG", "SHORT"):
        data["direction"] = "LONG" if snap["i1h"]["rsi"] < 50 else "SHORT"
    for field in ("entry_price", "stop_loss", "take_profit_1", "take_profit_2", "take_profit_3"):
        if not isinstance(data.get(field), (int, float)) or data[field] <= 0:
            data[field] = price
    if not isinstance(data.get("signal_rating"), int):
        data["signal_rating"] = 5
    data["signal_rating"] = max(1, min(10, data["signal_rating"]))
    if not isinstance(data.get("reasons"), list):
        data["reasons"] = [str(data.get("reasons", "—"))]
    conf = float(data.get("confidence", 55))
    data["confidence"] = max(40.0, min(85.0, conf))
    return data


async def _call_gemini(prompt: str) -> Optional[str]:
    key = getattr(settings, "GEMINI_API_KEY", "")
    if not _key_valid(key):
        return None
    payload = {"contents": [{"parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048}}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.post(f"{GEMINI_URL}?key={key}", json=payload) as r:
                if r.status != 200:
                    text = await r.text()
                    logger.warning(f"Gemini error: {r.status} {text[:200]}")
                    return None
                result = await r.json()
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.warning(f"Gemini exception: {e}")
        return None


async def _call_openrouter(prompt: str) -> Optional[str]:
    key = getattr(settings, "OPENROUTER_API_KEY", "")
    if not _key_valid(key):
        return None
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://t.me/dao_signals_bot"}
    payload = {"model": OPENROUTER_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": 2048, "temperature": 0.3}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.post(OPENROUTER_URL, headers=headers, json=payload) as r:
                if r.status != 200:
                    logger.warning(f"OpenRouter error: {r.status}")
                    return None
                result = await r.json()
        return result["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning(f"OpenRouter exception: {e}")
        return None


async def _call_groq(prompt: str) -> Optional[str]:
    global _groq_blocked_until
    if time.time() < _groq_blocked_until:
        logger.info(f"Groq blocked for {_groq_blocked_until - time.time():.0f}s more")
        return None
    key = getattr(settings, "GROQ_API_KEY", "")
    if not _key_valid(key):
        logger.warning(f"Groq key invalid or missing: '{key[:10]}...'")
        return None
    logger.info(f"Groq: sending request with model={GROQ_MODEL}, key={key[:10]}...")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": GROQ_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": 4096, "temperature": 0.3}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.post(GROQ_URL, headers=headers, json=payload) as r:
                body = await r.text()
                if r.status == 429:
                    _groq_blocked_until = time.time() + 60
                    logger.warning(f"Groq 429 — blocked for 60s. Body: {body[:200]}")
                    return None
                if r.status != 200:
                    logger.error(f"Groq HTTP {r.status}: {body[:300]}")
                    return None
                result = await r.json(content_type=None)
        content = result["choices"][0]["message"]["content"]
        logger.info(f"Groq OK, response length={len(content)}")
        return content
    except Exception as e:
        logger.error(f"Groq exception: {type(e).__name__}: {e}")
        return None


async def analyze_coin(snap: dict) -> Optional[dict]:
    prompt = build_prompt(snap)
    providers = [
        ("Gemini", _call_gemini(prompt)),
        ("OpenRouter", _call_openrouter(prompt)),
        ("Groq", _call_groq(prompt)),
    ]
    for name, coro in providers:
        try:
            raw = await coro
        except Exception as e:
            logger.warning(f"[{snap['coin']}] {name} error: {e}")
            continue
        if not raw:
            continue
        data = _parse(raw)
        if data is None:
            logger.warning(f"[{snap['coin']}] {name} JSON parse failed. Raw: {raw[:200]}")
            continue
        missing = REQUIRED - set(data.keys())
        if missing:
            logger.warning(f"[{snap['coin']}] {name} missing fields: {missing}")
            continue
        logger.info(f"[{snap['coin']}] {name} OK")
        return _validate(data, snap)
    logger.error(f"[{snap['coin']}] All providers failed")
    return None
