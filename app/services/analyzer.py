from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Leverage caps by SL distance: wider stop = lower max leverage
_LEV_CAPS = [(1.0, 10), (1.5, 8), (2.5, 5), (4.0, 3), (7.0, 2), (999, 1)]


def _recommend_leverage(sl_dist_pct: float, confidence: float, trend: str) -> tuple[int, int]:
    """Return (conservative_leverage, aggressive_leverage) as integers."""
    max_lev = 1
    for threshold, cap in _LEV_CAPS:
        if sl_dist_pct <= threshold:
            max_lev = cap
            break

    # Boost by +1 if confidence >= 70% and strong trend
    if confidence >= 70 and trend in ("STRONG BULL", "STRONG BEAR"):
        max_lev = min(max_lev + 1, 10)

    conservative = max(1, max_lev // 2)
    return conservative, max_lev


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

    # ── RSI ────────────────────────────────────────────────────────────────────────
    if rsi1 < 30:
        bull += 3; reasons_bull.append(f"монета сильно перепродана (RSI {rsi1:.0f}) — продавцы устали, вероятен отскок")
    elif rsi1 < 45:
        bull += 1; reasons_bull.append(f"продавцы ослабевают (RSI {rsi1:.0f})")
    elif rsi1 > 70:
        bear += 3; reasons_bear.append(f"монета перегрета (RSI {rsi1:.0f}) — покупатели выдыхаются, вероятна коррекция")
    elif rsi1 > 55:
        bear += 1; reasons_bear.append(f"покупатели теряют силу (RSI {rsi1:.0f})")

    if rsi4 < 40:
        bull += 2; reasons_bull.append(f"среднесрочно монета перепродана (RSI 4H {rsi4:.0f})")
    elif rsi4 > 60:
        bear += 2; reasons_bear.append(f"среднесрочно монета перекуплена (RSI 4H {rsi4:.0f})")

    if rsi1d < 40:
        bull += 1; reasons_bull.append(f"на дневном графике монета перепродана (RSI 1D {rsi1d:.0f})")
    elif rsi1d > 60:
        bear += 1; reasons_bear.append(f"на дневном графике монета перекуплена (RSI 1D {rsi1d:.0f})")

    # ── MACD ─────────────────────────────────────────────────────────────────────
    if macd1["bullish_cross"]:
        bull += 3; reasons_bull.append("импульс развернулся вверх — покупатели берут контроль")
    elif macd1["bearish_cross"]:
        bear += 3; reasons_bear.append("импульс развернулся вниз — продавцы берут контроль")
    elif macd1["histogram"] > 0:
        bull += 1
    elif macd1["histogram"] < 0:
        bear += 1

    if macd4["histogram"] > 0:
        bull += 2; reasons_bull.append("среднесрочный импульс направлен вверх")
    elif macd4["histogram"] < 0:
        bear += 2; reasons_bear.append("среднесрочный импульс направлен вниз")

    # ── EMA ────────────────────────────────────────────────────────────────────────
    if p > i1["ema_50"]:
        bull += 2; reasons_bull.append(f"цена держится выше ключевой средней ${i1['ema_50']:,.0f} — тренд бычий")
    else:
        bear += 2; reasons_bear.append(f"цена упала ниже ключевой средней ${i1['ema_50']:,.0f} — тренд медвежий")

    if p > i1["ema_200"]:
        bull += 1; reasons_bull.append(f"выше долгосрочной средней ${i1['ema_200']:,.0f} — глобально растём")
    else:
        bear += 1; reasons_bear.append(f"ниже долгосрочной средней ${i1['ema_200']:,.0f} — глобально падаем")

    # ── Stochastic ─────────────────────────────────────────────────────────────────────
    if stoch["oversold"]:
        bull += 2; reasons_bull.append(f"осциллятор в зоне перепроданности ({stoch['k']:.0f}) — отскок вероятен")
    elif stoch["overbought"]:
        bear += 2; reasons_bear.append(f"осциллятор в зоне перекупленности ({stoch['k']:.0f}) — откат вероятен")

    # ── ADX + Directional ────────────────────────────────────────────────────────────────
    if adx1["strong_trend"]:
        if adx1["plus_di"] > adx1["minus_di"]:
            bull += 2; reasons_bull.append(f"сила тренда высокая (ADX {adx1['adx']:.0f}) — покупатели доминируют")
        else:
            bear += 2; reasons_bear.append(f"сила тренда высокая (ADX {adx1['adx']:.0f}) — продавцы доминируют")

    # ── Bollinger Bands ───────────────────────────────────────────────────────────────────────
    if bb["position_pct"] < 20:
        bull += 1; reasons_bull.append("цена у нижней границы диапазона — отскок возможен")
    elif bb["position_pct"] > 80:
        bear += 1; reasons_bear.append("цена у верхней границы диапазона — коррекция возможна")

    # ── Volume ──────────────────────────────────────────────────────────────────────────
    if vol["buy_ratio_pct"] > 60 and vol["ratio"] > 1.2:
        bull += 2; reasons_bull.append(f"покупателей больше ({vol['buy_ratio_pct']:.0f}% объёма) — рост интереса к покупке")
    elif vol["buy_ratio_pct"] < 40 and vol["ratio"] > 1.2:
        bear += 2; reasons_bear.append(f"продавцов больше ({100 - vol['buy_ratio_pct']:.0f}% объёма) — рост давления на продажу")

    # ── Funding Rate ─────────────────────────────────────────────────────────────────────
    if funding < -0.01:
        bull += 1; reasons_bull.append("шортистов слишком много — возможен резкий рост (шорт-сквиз)")
    elif funding > 0.05:
        bear += 1; reasons_bear.append("лонгистов слишком много — рынок перекуплен, возможна ликвидация")

    # ── Fear & Greed ──────────────────────────────────────────────────────────────────────
    if fg <= 20:
        bull += 2; reasons_bull.append(f"все боятся ({fg}/100) — исторически лучшее время для покупки")
    elif fg >= 80:
        bear += 2; reasons_bear.append(f"все жадничают ({fg}/100) — рынок перегрет, риск обвала")

    # ── Direction & Confidence ───────────────────────────────────────────────────────────────────
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

    # ── SL / TP from levels and ATR ───────────────────────────────────────────────────────────────────
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

    # Leverage recommendation based on SL distance and confidence
    lev_cons, lev_aggr = _recommend_leverage(sl_dist, conf, trend)

    # ── Reasons (top 3 for chosen direction) ───────────────────────────────────────────────────────
    chosen_reasons = (reasons_bull if direction == "LONG" else reasons_bear)[:3]
    if len(chosen_reasons) < 3:
        chosen_reasons += (reasons_bear if direction == "LONG" else reasons_bull)[:3 - len(chosen_reasons)]
    if not chosen_reasons:
        chosen_reasons = [f"Технический анализ указывает на {direction}"]

    # ── Full analysis text ────────────────────────────────────────────────────────────────────────
    dir_word = "рост" if direction == "LONG" else "падение"
    trend_word = {"STRONG BULL": "сильный бычий", "WEAK BULL": "слабый бычий",
                  "NEUTRAL": "нейтральный", "WEAK BEAR": "слабый медвежий",
                  "STRONG BEAR": "сильный медвежий"}.get(trend, "нейтральный")

    rsi_comment = (
        "монета сильно перепродана" if rsi1 < 35 else
        "монета перегрета" if rsi1 > 65 else "индикаторы нейтральны"
    )
    fg_comment = (
        "все боятся — возможно дно" if fg <= 25 else
        "все жадничают — рынок перегрет" if fg >= 75 else
        f"настроения нейтральные"
    )
    full = (
        f"{dominant} из {total} индикаторов указывают на {dir_word}. "
        f"Цена {'держится выше' if p > i1['ema_50'] else 'упала ниже'} ключевой средней "
        f"${i1['ema_50']:,.0f} — тренд {'бычий' if p > i1['ema_50'] else 'медвежий'}. "
        f"{rsi_comment.capitalize()}. {fg_comment.capitalize()} ({fg}/100). "
        f"Рекомендация: {'открыть лонг' if direction == 'LONG' else 'открыть шорт'} "
        f"с целью ${tp1:,.0f} и стопом ${sl:,.0f}."
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

    # Liquidation price at conservative leverage (LONG: liq below entry, SHORT: above)
    if direction == "LONG":
        liq_cons  = round(p * (1 - 0.9 / lev_cons), 2)
        liq_aggr  = round(p * (1 - 0.9 / lev_aggr), 2)
    else:
        liq_cons  = round(p * (1 + 0.9 / lev_cons), 2)
        liq_aggr  = round(p * (1 + 0.9 / lev_aggr), 2)

    logger.info(f"[{snap['coin']}] Rule-based: {direction} conf={conf}% rating={rating} bull={bull} bear={bear} lev={lev_cons}x/{lev_aggr}x")

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
        "leverage_conservative": lev_cons,
        "leverage_aggressive": lev_aggr,
        "liq_price_conservative": liq_cons,
        "liq_price_aggressive": liq_aggr,
    }
