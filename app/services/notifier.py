from __future__ import annotations
import logging
from datetime import datetime, timezone
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from app.models.database import Signal
from app.services.user_service import get_users_with_notifications

logger = logging.getLogger(__name__)

POLYMARKET_URL = "https://polymarket.com/markets/crypto?via=max-chron0n"

_STARS = {
    10: "⭐⭐⭐⭐⭐", 9: "⭐⭐⭐⭐⭐",
    8:  "⭐⭐⭐⭐✩",  7: "⭐⭐⭐⭐✩",
    6:  "⭐⭐⭐✩✩",  5: "⭐⭐⭐✩✩",
    4:  "⭐⭐✩✩✩",  3: "⭐⭐✩✩✩",
    2:  "⭐✩✩✩✩",  1: "⭐✩✩✩✩",
}
_TREND_LABEL = {
    "STRONG BULL": "🐂🐂 Сильный рост",
    "WEAK BULL":   "🐂 Слабый рост",
    "NEUTRAL":     "↔️ Нейтрально",
    "WEAK BEAR":   "🐻 Слабое падение",
    "STRONG BEAR": "🐻🐻 Сильное падение",
}
_DIR_HEADER = {
    "LONG":  "📈 ЛОНГ",
    "SHORT": "📉 ШОРТ",
}


def _p(v: float) -> str:
    if v >= 1000:
        return f"${v:,.2f}"
    if v >= 1:
        return f"${v:.4f}"
    return f"${v:.6f}"


def _prob_bar(up: float) -> str:
    filled = max(0, min(10, round(up / 10)))
    bar = "█" * filled + "░" * (10 - filled)
    down = 100 - up
    return f"🟢 <b>{up:.0f}%</b> вверх  <code>{bar}</code>  <b>{down:.0f}%</b> вниз 🔴"


def _profit_line(sig: Signal) -> str:
    try:
        ep = sig.entry_price
        tp1 = sig.take_profit_1
        tp2 = sig.take_profit_2
        tp3 = sig.take_profit_3
        deposit = 1000.0
        if sig.direction == "LONG":
            p1 = (tp1 - ep) / ep * deposit
            p2 = (tp2 - ep) / ep * deposit
            p3 = (tp3 - ep) / ep * deposit
        else:
            p1 = (ep - tp1) / ep * deposit
            p2 = (ep - tp2) / ep * deposit
            p3 = (ep - tp3) / ep * deposit
        return (
            f"💵 <b>Профит с $1000:</b>\n"
            f"  TP1 → <b><u>+${p1:,.1f}</u></b>  "
            f"TP2 → <b><u>+${p2:,.1f}</u></b>  "
            f"TP3 → <b><u>+${p3:,.1f}</u></b>"
        )
    except Exception:
        return ""


def _leverage_block(sig: Signal) -> str:
    lc = sig.leverage_conservative
    la = sig.leverage_aggressive
    if not lc or not la:
        return ""
    ep = sig.entry_price
    tp1 = sig.take_profit_1
    if sig.direction == "LONG":
        pnl_cons = (tp1 - ep) / ep * lc * 100
        pnl_aggr = (tp1 - ep) / ep * la * 100
    else:
        pnl_cons = (ep - tp1) / ep * lc * 100
        pnl_aggr = (ep - tp1) / ep * la * 100
    liq_cons = _p(sig.liq_price_conservative) if sig.liq_price_conservative else "—"
    liq_aggr = _p(sig.liq_price_aggressive) if sig.liq_price_aggressive else "—"
    return (
        f"⚡ <b>Плечо:</b>\n"
        f"  Консервативно: <b>x{lc}</b>  →  TP1 <b><u>+{pnl_cons:.1f}%</u></b>  ·  Ликвидация: {liq_cons}\n"
        f"  Агрессивно:    <b>x{la}</b>  →  TP1 <b><u>+{pnl_aggr:.1f}%</u></b>  ·  Ликвидация: {liq_aggr}"
    )


def format_signal(sig: Signal) -> str:
    stars = _STARS.get(sig.signal_rating, "⭐⭐⭐✩✩")
    header = _DIR_HEADER.get(sig.direction, sig.direction)
    trend = _TREND_LABEL.get(sig.trend_strength or "", "↔️ Нейтрально")
    up = sig.prob_up or (100 - sig.confidence if sig.direction == "SHORT" else sig.confidence)
    sl_pct  = abs(sig.entry_price - sig.stop_loss)    / sig.entry_price * 100
    tp1_pct = abs(sig.take_profit_1 - sig.entry_price) / sig.entry_price * 100
    tp2_pct = abs(sig.take_profit_2 - sig.entry_price) / sig.entry_price * 100
    tp3_pct = abs(sig.take_profit_3 - sig.entry_price) / sig.entry_price * 100
    reasons = [r.strip() for r in (sig.reasons or "").split("\n") if r.strip()]
    reasons_text = "\n".join(f"  • {r}" for r in reasons[:3])
    profit = _profit_line(sig)
    leverage = _leverage_block(sig)
    time_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lev_sep = "━━━━━━━━━━━━━━━━━━━━\n" if leverage else ""
    lev_line = f"{leverage}\n" if leverage else ""
    return (
        f"{header}  <b>{sig.coin}/USDT</b>  {stars}\n"
        f"{time_str}  ·  #{sig.id}  ·  уверен. <b>{sig.confidence:.0f}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{_prob_bar(up)}\n"
        f"Тренд: {trend}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Цена:   <b>{_p(sig.entry_price)}</b>\n"
        f"🛒 Вход:   <b>{_p(sig.entry_price)}</b>  [{sig.entry_type}]\n"
        f"🛑 SL:     <b>{_p(sig.stop_loss)}</b>  <i>-{sl_pct:.1f}%</i>\n"
        f"🎯 TP1:    <b>{_p(sig.take_profit_1)}</b>  <i>+{tp1_pct:.1f}%</i>\n"
        f"🎯 TP2:    <b>{_p(sig.take_profit_2)}</b>  <i>+{tp2_pct:.1f}%</i>\n"
        f"🎯 TP3:    <b>{_p(sig.take_profit_3)}</b>  <i>+{tp3_pct:.1f}%</i>\n"
        f"⚖️ R/R:    <b>1:{sig.risk_reward:.1f}</b>  ·  {sig.timeframe or '4-12 часов'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Почему:\n"
        f"{reasons_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{profit}\n"
        f"{lev_sep}"
        f"{lev_line}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <a href=\"{POLYMARKET_URL}\">Ставка на Polymarket</a>\n"
        f"<i>💡 Нажми «Полный анализ» для подробного разбора</i>"
    )


def format_full_analysis(sig: Signal) -> str:
    stars = _STARS.get(sig.signal_rating, "⭐⭐⭐✩✩")
    header = _DIR_HEADER.get(sig.direction, sig.direction)
    trend = _TREND_LABEL.get(sig.trend_strength or "", "↔️ Нейтрально")
    rsi = sig.rsi_1h or 0
    if rsi >= 70:   rsi_txt = "перекуплен — скоро коррекция ⚠️"
    elif rsi <= 30: rsi_txt = "перепродан — возможен отскок 🔥"
    elif rsi >= 55: rsi_txt = "в зоне покупателей 🐂"
    elif rsi <= 45: rsi_txt = "в зоне продавцов 🐻"
    else:           rsi_txt = "нейтральная зона ↔️"
    funding = (sig.funding_rate or 0) * 100
    if funding > 0.05:    fund_txt = "лонги перегреты 🔴"
    elif funding < -0.01: fund_txt = "шорты перегреты 🟢"
    else:                 fund_txt = "нейтрально ✅"
    fg = sig.fear_greed or 50
    if fg >= 75:   fg_txt = "крайняя жадность 🤑 — рынок перегрет"
    elif fg >= 55: fg_txt = "жадность 😏"
    elif fg <= 25: fg_txt = "крайний страх 😱 — возможно дно"
    elif fg <= 45: fg_txt = "страх 😰"
    else:          fg_txt = "нейтрально 😐"
    price = sig.price_at_signal or sig.entry_price
    ema50 = sig.ema50_1h or 0
    ema200 = sig.ema200_1h or 0
    ema_txt = []
    if ema50:  ema_txt.append(f"{'выше' if price > ema50 else 'ниже'} EMA50 ${ema50:,.0f}")
    if ema200: ema_txt.append(f"{'выше' if price > ema200 else 'ниже'} EMA200 ${ema200:,.0f}")
    reasons = [r.strip() for r in (sig.reasons or "").split("\n") if r.strip()]
    reasons_text = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(reasons))
    profit = _profit_line(sig)
    leverage = _leverage_block(sig)
    return (
        f"🔬 <b>ПОЛНЫЙ АНАЛИЗ — {sig.coin}/USDT</b>\n"
        f"{header}  {stars}  Уверенность <b>{sig.confidence:.0f}%</b>\n"
        f"Тренд: {trend}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>📊 Что говорят индикаторы:</b>\n"
        f"  RSI(1H): <b>{rsi:.1f}</b> — {rsi_txt}\n"
        f"  Цена: {', '.join(ema_txt) or 'нет данных'}\n"
        f"  Funding: <b>{funding:+.4f}%</b> — {fund_txt}\n"
        f"  Fear & Greed: <b>{fg}/100</b> — {fg_txt}\n\n"
        f"<b>⚙️ Причины сигнала:</b>\n"
        f"{reasons_text}\n\n"
        f"<b>🟢 Если пойдёт вверх:</b>\n"
        f"  {sig.bull_scenario or '—'}\n\n"
        f"<b>🔴 Если пойдёт вниз:</b>\n"
        f"  {sig.bear_scenario or '—'}\n\n"
        f"<b>🔑 Ключевой уровень:</b>\n"
        f"  {sig.key_trigger or '—'}\n\n"
        f"<b>📝 Простым языком:</b>\n"
        f"  {sig.full_analysis or '—'}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{profit}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{leverage + chr(10) if leverage else ''}"
        f"{'━━━━━━━━━━━━━━━━━━━━' + chr(10) if leverage else ''}"
        f"🔗 <a href=\"{POLYMARKET_URL}\">Поставить на Polymarket</a>\n"
        f"<i>🕐 {sig.created_at.strftime('%d.%m.%Y %H:%M') if sig.created_at else '—'} UTC  ·  #{sig.id}</i>"
    )


def format_market_overview(signals: list[Signal]) -> str:
    lines = [
        "<b>📡 ОБЗОР РЫНКА — BTC · ETH · SOL</b>\n"
        f"<i>{datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC</i>\n"
        "━━━━━━━━━━━━━━━━━━━━"
    ]
    for sig in signals:
        if sig is None:
            continue
        header = _DIR_HEADER.get(sig.direction, sig.direction)
        stars = _STARS.get(sig.signal_rating, "⭐⭐⭐✩✩")
        trend = _TREND_LABEL.get(sig.trend_strength or "", "↔️")
        sl_pct  = abs(sig.entry_price - sig.stop_loss)    / sig.entry_price * 100
        tp1_pct = abs(sig.take_profit_1 - sig.entry_price) / sig.entry_price * 100
        lines.append(
            f"\n{header}  <b>{sig.coin}/USDT</b>  {stars}\n"
            f"  Уверен. <b>{sig.confidence:.0f}%</b>  ·  {trend}\n"
            f"  🛒 <b>{_p(sig.entry_price)}</b>  "
            f"🛑 {_p(sig.stop_loss)} <i>-{sl_pct:.1f}%</i>  "
            f"🎯 {_p(sig.take_profit_1)} <i>+{tp1_pct:.1f}%</i>\n"
            f"  ⚖️ R/R 1:{sig.risk_reward:.1f}  ·  {sig.timeframe or '—'}\n"
            f"  ─────────────────────"
        )
    lines.append(f"\n🔗 <a href=\"{POLYMARKET_URL}\">Ставки на Polymarket</a>")
    lines.append("<i>Нажми на монету для полного анализа</i>")
    return "\n".join(lines)


async def broadcast_signal(bot: Bot, sig: Signal) -> tuple[int, int]:
    from app.keyboards.inline import signal_kb
    users = await get_users_with_notifications()
    text = format_signal(sig)
    kb = signal_kb(sig.coin, sig.id)
    sent = blocked = 0
    for user in users:
        if not user.has_access():
            continue
        try:
            await bot.send_message(user.telegram_id, text, reply_markup=kb,
                                   parse_mode="HTML", disable_web_page_preview=True)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramBadRequest as e:
            logger.warning(f"Bad request {user.telegram_id}: {e}")
        except Exception as e:
            logger.error(f"Send error {user.telegram_id}: {e}")
    logger.info(f"Broadcast #{sig.id}: sent={sent} blocked={blocked}")
    return sent, blocked
