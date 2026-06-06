from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def analyze_coin(snap: dict) -> Optional[dict]:
    """Rule-based technical analysis — no AI API needed."""
    p = snap["price"]
    i1 = snap["i1h"]
    i4 = snap["i4h"]
    i1d = snap["i1d"]
    vol = snap["volume"]
    macd1 = i1["macd"]
    macd4 = i4["macd"]
    adx1 = i1["adx"]
    stoch = i1["stochastic"]
    bb = i1["bollinger"]
    atr = i1["atr"]
    rsi1 = i1["rsi"]
    rsi4 = i4["rsi"]
    rsi1d = i1d["rsi"]
    funding = snap["funding_rate"] * 100
    fg = snap["fear_greed"]
    ls = snap["long_short_ratio"]

    bull = 0  # bullish score
    bear = 0  # bearish score
    reasons_bull = []
    reasons_bear = []

    # ── RSI ──────────────────────────────────────────────────────────────────
    if rsi1 < 30:
        bull += 3; reasons_bull.append(f"RSI(1H)={rsi1:.1f} — зона перепроданности, разворот вверх вероятен")
    elif rsi1 < 45:
        bull += 1; reasons_bull.append(f"RSI(1H)={rsi1:.1f} — слабость продавцов")
    elif rsi1 > 70:
        bear += 3; reasons_bear.append(f"RSI(1H)={rsi1:.1f} — перекупленность, коррекция вниз вероятна")
    elif rsi1 > 55:
        bear += 1; reasons_bear.append(f"RSI(1H)={rsi1:.1f} — давление покупателей ослабевает")

    if rsi4 < 40:
        bull += 2; reasons_bull.append(f"RSI(4H)={rsi4:.1f} — среднесрочная перепроданность")
    elif rsi4 > 60:
        bear += 2; reasons_bear.append(f"RSI(4H)={rsi4:.1f} — среднесрочная перекупленность")

    if rsi1d < 40:
        bull += 1; reasons_bull.append(f"RSI(1D)={rsi1d:.1f} — дневной тренд перепродан")
    elif rsi1d > 60:
        bear += 1; reasons_bear.append(f"RSI(1D)={rsi1d:.1f} — дневной тренд перекуплен")

    # ── MACD ─────────────────────────────────────────────────────────────────
    if macd1["bullish_cross"]:
        bull += 3; reasons_bull.append(f"MACD(1H) бычье пересечение — сигнал к росту")
    elif macd1["bearish_cross"]:
        bear += 3; reasons_bear.append(f"MACD(1H) медвежье пересечение — сигнал к падению")
    elif macd1["histogram"] > 0:
        bull += 1
    elif macd1["histogram"] < 0:
        bear += 1

    if macd4["histogram"] > 0:
        bull += 2; reasons_bull.append(f"MACD(4H) в плюсе — среднесрочный импульс вверх")
    elif macd4["histogram"] < 0:
        bear += 2; reasons_bear.append(f"MACD(4H) в минусе — среднесрочный импульс вниз")

    # ── EMA ──────────────────────────────────────────────────────────────────
    if p > i1["ema_50"]:
        bull += 2; reasons_bull.append(f"Цена ${p:,.0f} выше EMA50=${i1['ema_50']:,.0f} — бычья структура")
    else:
        bear += 2; reasons_bear.append(f"Цена ${p:,.0f} ниже EMA50=${i1['ema_50']:,.0f} — медвежья структура")

    if p > i1["ema_200"]:
        bull += 1; reasons_bull.append(f"Цена выше EMA200=${i1['ema_200']:,.0f} — долгосрочный бычий тренд")
    else:
        bear += 1; reasons_bear.append(f"Цена ниже EMA200=${i1['ema_200']:,.0f} — долгосрочный медвежий тренд")

    # ── Stochastic ───────────────────────────────────────────────────────────
    if stoch["oversold"]:
        bull += 2; reasons_bull.append(f"Stochastic K={stoch['k']:.0f} — перепроданность, отскок возможен")
    elif stoch["overbought"]:
        bear += 2; reasons_bear.append(f"Stochastic K={stoch['k']:.0f} — перекупленность, откат возможен")

    # ── ADX + Directional ────────────────────────────────────────────────────
    if adx1["strong_trend"]:
        if adx1["plus_di"] > adx1["minus_di"]:
            bull += 2; reasons_bull.append(f"ADX={adx1['adx']:.0f} сильный тренд вверх (+DI={adx1['plus_di']:.0f} > -DI={adx1['minus_di']:.0f})")
        else:
            bear += 2; reasons_bear.append(f"ADX={adx1['adx']:.0f} сильный тренд вниз (-DI={adx1['minus_di']:.0f} > +DI={adx1['plus_di']:.0f})")

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    if bb["position_pct"] < 20:
        bull += 1; reasons_bull.append(f"Цена у нижней границы Bollinger ({bb['position_pct']:.0f}%) — возможен отскок")
    elif bb["position_pct"] > 80:
        bear += 1; reasons_bear.append(f"Цена у верхней границы Bollinger ({bb['position_pct']:.0f}%) — возможна коррекция")

    # ── Volume ───────────────────────────────────────────────────────────────
    if vol["buy_ratio_pct"] > 60 and vol["ratio"] > 1.2:
        bull += 2; reasons_bull.append(f"Объём покупок {vol['buy_ratio_pct']:.0f}% при объёме {vol['ratio']:.1f}x нормы — давление покупателей")
    elif vol["buy_ratio_pct"] < 40 and vol["ratio"] > 1.2:
        bear += 2; reasons_bear.append(f"Объём продаж {100 - vol['buy_ratio_pct']:.0f}% при объёме {vol['ratio']:.1f}x нормы — давление продавцов")

    # ── Funding Rate ─────────────────────────────────────────────────────────
    if funding < -0.01:
        bull += 1; reasons_bull.append(f"Funding Rate {funding:+.4f}% отрицательный — шорты перегреты, вероятен шорт-сквиз")
    elif funding > 0.05:
        bear += 1; reasons_bear.append(f"Funding Rate {funding:+.4f}% высокий — лонги перегреты")

    # ── Fear & Greed ─────────────────────────────────────────────────────────
    if fg <= 20:
        bull += 2; reasons_bull.append(f"Fear & Greed={fg}/100 — экстремальный страх, исторически хороший момент для покупки")
    elif fg >= 80:
        bear += 2; reasons_bear.append(f"Fear & Greed={fg}/100 — экстремальная жадность, рынок перегрет")

    # ── Direction & Confidence ───────────────────────────────────────────────
    total = bull + bear
    direction = "LONG" if bull >= bear else "SHORT"
    dominant = bull if direction == "LONG" else bear
    conf = round(min(85, 40 + (dominant / max(total, 1)) * 55), 1)

    # Signal rating 1-10
    score_diff = abs(bull - bear)
    rating = max(1, min(10, 3 + score_diff))

    # Trend strength
    ratio = dominant / max(total, 1)
    if ratio >= 0.75:
        trend = "STRONG BULL" if direction == "LONG" else "STRONG BEAR"
    elif ratio >= 0.60:
        trend = "WEAK BULL" if direction == "LONG" else "WEAK BEAR"
    else:
        trend = "NEUTRAL"

    # ── SL / TP from levels and ATR ──────────────────────────────────────────
    supports = snap["supports"]
    resistances = snap["resistances"]

    if direction == "LONG":
        sl = supports[0] - atr * 0.3 if supports else round(p - atr * 2, 2)
        tp1 = resistances[0] if resistances else round(p + atr * 2, 2)
        tp2 = resistances[1] if len(resistances) > 1 else round(p + atr * 4, 2)
        tp3 = resistances[2] if len(resistances) > 2 else round(p + atr * 6, 2)
    else:
        sl = resistances[0] + atr * 0.3 if resistances else round(p + atr * 2, 2)
        tp1 = supports[0] if supports else round(p - atr * 2, 2)
        tp2 = supports[1] if len(supports) > 1 else round(p - atr * 4, 2)
        tp3 = supports[2] if len(supports) > 2 else round(p - atr * 6, 2)

    sl = round(sl, 2)
    tp1 = round(tp1, 2)
    tp2 = round(tp2, 2)
    tp3 = round(tp3, 2)

    sl_dist = abs(p - sl) / p * 100
    tp1_dist = abs(tp1 - p) / p * 100
    rr = round(tp1_dist / sl_dist, 2) if sl_dist > 0 else 1.5

    # ── Reasons (top 3 for chosen direction) ─────────────────────────────────
    chosen_reasons = (reasons_bull if direction == "LONG" else reasons_bear)[:3]
    if len(chosen_reasons) < 3:
        chosen_reasons += (reasons_bear if direction == "LONG" else reasons_bull)[:3 - len(chosen_reasons)]
    if not chosen_reasons:
        chosen_reasons = [f"Технический анализ указывает на {direction}"]

    # ── Full analysis text ────────────────────────────────────────────────────
    dir_word = "рост" if direction == "LONG" else "падение"
    trend_word = {"STRONG BULL": "сильный бычий", "WEAK BULL": "слабый бычий",
                  "NEUTRAL": "нейтральный", "WEAK BEAR": "слабый медвежий",
                  "STRONG BEAR": "сильный медвежий"}.get(trend, "нейтральный")

    full = (
        f"Текущий тренд по {snap['coin']} — {trend_word}. "
        f"Из {total} сигналов индикаторов {dominant} указывают на {dir_word}. "
        f"Цена {'выше' if p > i1['ema_50'] else 'ниже'} ключевой скользящей EMA50, "
        f"RSI(1H)={rsi1:.0f} {'— зона перепроданности' if rsi1 < 35 else '— зона перекупленности' if rsi1 > 65 else '— нейтральная зона'}. "
        f"Fear & Greed: {fg}/100. "
        f"Рекомендация: {'открывать лонг' if direction == 'LONG' else 'открывать шорт'} с целью "
        f"${tp1:,.0f}, стоп-лосс ${sl:,.0f}."
    )

    bull_scenario = (
        f"Прорыв выше ${resistances[0]:,.0f}" if resistances
        else f"Рост выше ${round(p * 1.03, 0):,.0f}"
    )
    bear_scenario = (
        f"Пробой ниже ${supports[0]:,.0f}" if supports
        else f"Падение ниже ${round(p * 0.97, 0):,.0f}"
    )
    key_trigger = (
        f"Уровень ${resistances[0]:,.0f}" if direction == "LONG" and resistances
        else f"Уровень ${supports[0]:,.0f}" if direction == "SHORT" and supports
        else f"ATR зона ${round(p - atr, 0):,.0f}–${round(p + atr, 0):,.0f}"
    )

    logger.info(f"[{snap['coin']}] Rule-based: {direction} conf={conf}% rating={rating} bull={bull} bear={bear}")

    return {
        "direction": direction,
        "confidence": conf,
        "signal_rating": rating,
        "trend_strength": trend,
        "prob_up": round(bull / max(total, 1) * 100),
        "prob_down": round(bear / max(total, 1) * 100),
        "entry_price": p,
        "entry_type": "MARKET",
        "stop_loss": sl,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "take_profit_3": tp3,
        "risk_reward": rr,
        "sl_distance_pct": round(sl_dist, 2),
        "timeframe": "4-12 часов",
        "reasons": chosen_reasons,
        "bull_scenario": bull_scenario,
        "bear_scenario": bear_scenario,
        "key_trigger": key_trigger,
        "full_analysis": full,
    }
