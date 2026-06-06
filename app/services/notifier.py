from __future__ import annotations
import logging
from datetime import datetime, timezone
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from app.models.database import Signal
from app.services.user_service import get_all_active_users

logger = logging.getLogger(__name__)

_DIR = {"LONG": "🟢 LONG", "SHORT": "🔴 SHORT", "NO TRADE": "🟡 NO TRADE"}
_DIR_EMOJI = {"LONG": "📈", "SHORT": "📉", "NO TRADE": "⏸"}
_STARS = {10: "★★★★★", 9: "★★★★★", 8: "★★★★☆", 7: "★★★★☆",
          6: "★★★☆☆", 5: "★★★☆☆", 4: "★★☆☆☆", 3: "★★☆☆☆", 2: "★☆☆☆☆", 1: "★☆☆☆☆"}
_TREND_EMOJI = {
    "STRONG BULL": "🐂🐂", "WEAK BULL": "🐂", "NEUTRAL": "↔️",
    "WEAK BEAR": "🐻", "STRONG BEAR": "🐻🐻",
}


def _p(v: float) -> str:
    if v >= 1000:
        return f"${v:,.2f}"
    if v >= 1:
        return f"${v:.4f}"
    return f"${v:.6f}"


def _conf_bar(conf: float) -> str:
    filled = int(conf / 10)
    return "█" * filled + "░" * (10 - filled)


def format_signal(sig: Signal) -> str:
    stars = _STARS.get(sig.signal_rating, "★★★☆☆")
    reasons = [r for r in (sig.reasons or "").split("\n") if r.strip()]
    reasons_text = "\n".join(f"  • {r}" for r in reasons[:5])
    sl_pct = abs(sig.entry_price - sig.stop_loss) / sig.entry_price * 100
    tp1_pct = abs(sig.take_profit_1 - sig.entry_price) / sig.entry_price * 100
    tp2_pct = abs(sig.take_profit_2 - sig.entry_price) / sig.entry_price * 100
    tp3_pct = abs(sig.take_profit_3 - sig.entry_price) / sig.entry_price * 100

    prob_line = (
        f"  Вероятность:  🟢 <b>{sig.prob_up:.0f}%</b> ↑  🔴 <b>{sig.prob_down:.0f}%</b> ↓\n"
        if sig.prob_up else ""
    )
    trend_emoji = _TREND_EMOJI.get(sig.trend_strength or "", "")
    trend_line = f"  Тренд:        {trend_emoji} <b>{sig.trend_strength}</b>\n" if sig.trend_strength else ""
    tf_line = f"  Горизонт:     ⏱ <b>{sig.timeframe}</b>\n" if sig.timeframe else ""
    conf_bar = _conf_bar(sig.confidence)

    # Show first 2 paragraphs of analysis in the main signal
    analysis_paragraphs = [p.strip() for p in (sig.full_analysis or "").split("\n\n") if p.strip()]
    analysis_preview = ("\n\n  ".join(analysis_paragraphs[:2]))[:800]

    dir_label = _DIR.get(sig.direction, sig.direction)
    dir_emoji = _DIR_EMOJI.get(sig.direction, "")

    return (
        f"╔══════════════════════════════\n"
        f"║  {dir_label}  {dir_emoji}  ·  <b>{sig.coin}/USDT</b>\n"
        f"║  {stars}  Рейтинг <b>{sig.signal_rating}/10</b>\n"
        f"╠══════════════════════════════\n"
        f"  Уверенность:  <b>{sig.confidence:.0f}%</b>  <code>{conf_bar}</code>\n"
        f"{trend_line}"
        f"{prob_line}"
        f"{tf_line}"
        f"╠══════════════════════════════\n"
        f"  💰 Вход:       <b>{_p(sig.entry_price)}</b>  [{sig.entry_type}]\n"
        f"  🛑 Stop Loss:  <b>{_p(sig.stop_loss)}</b>  <i>(-{sl_pct:.1f}%)</i>\n"
        f"  🎯 TP1:        <b>{_p(sig.take_profit_1)}</b>  <i>(+{tp1_pct:.1f}%)</i>\n"
        f"  🎯 TP2:        <b>{_p(sig.take_profit_2)}</b>  <i>(+{tp2_pct:.1f}%)</i>\n"
        f"  🎯 TP3:        <b>{_p(sig.take_profit_3)}</b>  <i>(+{tp3_pct:.1f}%)</i>\n"
        f"  ⚖️ R/R:        <b>1:{sig.risk_reward:.1f}</b>\n"
        f"╠══════════════════════════════\n"
        f"  <b>📊 Ключевые причины входа:</b>\n"
        f"{reasons_text}\n"
        f"╠══════════════════════════════\n"
        f"  <b>📝 Краткий анализ:</b>\n"
        f"  {analysis_preview}\n"
        f"╚══════════════════════════════\n"
        f"<i>🕐 {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC  ·  DAO Signals #{sig.id}</i>\n"
        f"<i>💡 Нажми «Полный анализ» для подробного разбора</i>"
    )


def format_full_analysis(sig: Signal) -> str:
    """Полный разбор сигнала с объяснением всех факторов."""
    stars = _STARS.get(sig.signal_rating, "★★★☆☆")
    dir_label = _DIR.get(sig.direction, sig.direction)

    # RSI interpretation
    rsi = sig.rsi_1h or 0
    if rsi >= 70:
        rsi_comment = "перекуплен ⚠️"
    elif rsi <= 30:
        rsi_comment = "перепродан 🔥"
    elif rsi >= 55:
        rsi_comment = "бычья зона 🐂"
    elif rsi <= 45:
        rsi_comment = "медвежья зона 🐻"
    else:
        rsi_comment = "нейтрально ↔️"

    # MACD
    macd_val = sig.macd_1h or 0
    macd_comment = "выше нуля (бычий)" if macd_val > 0 else "ниже нуля (медвежий)"

    # EMA structure
    price = sig.price_at_signal or sig.entry_price
    ema50 = sig.ema50_1h or 0
    ema200 = sig.ema200_1h or 0
    ema_comment = []
    if ema50 and price > ema50:
        ema_comment.append("цена выше EMA50 ✅")
    elif ema50:
        ema_comment.append("цена ниже EMA50 ❌")
    if ema200 and price > ema200:
        ema_comment.append("выше EMA200 ✅")
    elif ema200:
        ema_comment.append("ниже EMA200 ❌")
    ema_str = " | ".join(ema_comment) if ema_comment else "нет данных"

    # Funding rate
    funding = sig.funding_rate or 0
    funding_pct = funding * 100
    if funding_pct > 0.05:
        funding_comment = "лонги перегреты 🔴"
    elif funding_pct < -0.01:
        funding_comment = "шорты перегреты 🟢"
    else:
        funding_comment = "нейтрально ✅"

    # Fear & Greed
    fg = sig.fear_greed or 50
    if fg >= 75:
        fg_comment = "Крайняя жадность 🤑"
    elif fg >= 55:
        fg_comment = "Жадность 😏"
    elif fg <= 25:
        fg_comment = "Крайний страх 😱"
    elif fg <= 45:
        fg_comment = "Страх 😰"
    else:
        fg_comment = "Нейтрально 😐"

    # Bull/bear scenarios
    bull = sig.bull_scenario or "нет данных"
    bear = sig.bear_scenario or "нет данных"
    trigger = sig.key_trigger or "нет данных"

    # Full analysis text
    full = sig.full_analysis or "Анализ недоступен."

    # Reasons
    reasons = [r for r in (sig.reasons or "").split("\n") if r.strip()]
    reasons_text = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(reasons))

    return (
        f"<b>🔬 ПОЛНЫЙ АНАЛИЗ — {sig.coin}/USDT</b>\n"
        f"{dir_label}  {stars}  Рейтинг <b>{sig.signal_rating}/10</b>  Уверенность <b>{sig.confidence:.0f}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>📊 ИНДИКАТОРЫ (1H):</b>\n"
        f"  RSI:    <b>{rsi:.1f}</b> — {rsi_comment}\n"
        f"  MACD:   <b>{macd_val:+.4f}</b> — {macd_comment}\n"
        f"  EMA:    {ema_str}\n\n"
        f"<b>📈 ДЕРИВАТИВЫ:</b>\n"
        f"  Funding Rate: <b>{funding_pct:+.4f}%</b> — {funding_comment}\n"
        f"  Fear & Greed: <b>{fg}/100</b> — {fg_comment}\n\n"
        f"<b>⚙️ ПРИЧИНЫ СИГНАЛА:</b>\n"
        f"{reasons_text}\n\n"
        f"<b>🟢 БЫЧИЙ СЦЕНАРИЙ:</b>\n"
        f"  {bull}\n\n"
        f"<b>🔴 МЕДВЕЖИЙ СЦЕНАРИЙ:</b>\n"
        f"  {bear}\n\n"
        f"<b>🔑 КЛЮЧЕВОЙ ТРИГГЕР:</b>\n"
        f"  {trigger}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>📝 ДЕТАЛЬНЫЙ AI-АНАЛИЗ:</b>\n\n"
        f"{full}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>🕐 {sig.created_at.strftime('%d.%m.%Y %H:%M') if sig.created_at else '—'} UTC  ·  DAO Signals #{sig.id}</i>"
    )


def format_market_overview(signals: list[Signal]) -> str:
    """Краткий обзор всех 3 монет."""
    lines = [
        "<b>📡 ОБЗОР РЫНКА — BTC · ETH · SOL</b>\n"
        f"<i>{datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC</i>\n"
        "━━━━━━━━━━━━━━━━━━━━"
    ]
    for sig in signals:
        if sig is None:
            continue
        dir_label = _DIR.get(sig.direction, sig.direction)
        dir_emoji = _DIR_EMOJI.get(sig.direction, "")
        stars = _STARS.get(sig.signal_rating, "★★★☆☆")
        rsi = sig.rsi_1h or 0
        funding = (sig.funding_rate or 0) * 100
        fg = sig.fear_greed or 50

        # Quick status for each coin
        rsi_icon = "🔴" if rsi >= 70 else "🟢" if rsi <= 30 else "🟡"
        fund_icon = "🔴" if funding > 0.05 else "🟢" if funding < -0.01 else "🟡"

        lines.append(
            f"\n<b>{sig.coin}/USDT</b>  {dir_label} {dir_emoji}\n"
            f"  {stars}  Рейтинг <b>{sig.signal_rating}/10</b>  Уверен. <b>{sig.confidence:.0f}%</b>\n"
            f"  💰 Вход: <b>${sig.entry_price:,.2f}</b>  ⚖️ R/R: <b>1:{sig.risk_reward:.1f}</b>\n"
            f"  RSI {rsi_icon} <b>{rsi:.1f}</b>  Funding {fund_icon} <b>{funding:+.3f}%</b>  F&G <b>{fg}</b>\n"
            f"  Тренд: <b>{sig.trend_strength or '—'}</b>  Горизонт: <b>{sig.timeframe or '—'}</b>"
        )
        if sig.direction != "NO TRADE":
            sl_pct = abs(sig.entry_price - sig.stop_loss) / sig.entry_price * 100
            tp1_pct = abs(sig.take_profit_1 - sig.entry_price) / sig.entry_price * 100
            lines.append(
                f"  🛑 SL: <b>${sig.stop_loss:,.2f}</b> (-{sl_pct:.1f}%)  "
                f"🎯 TP1: <b>${sig.take_profit_1:,.2f}</b> (+{tp1_pct:.1f}%)"
            )
        lines.append("  ─────────────────────")

    lines.append("\n<i>💡 Нажми на монету для детального анализа</i>")
    return "\n".join(lines)


async def broadcast_signal(bot: Bot, sig: Signal) -> tuple[int, int]:
    from app.keyboards.inline import signal_kb
    from app.services.user_service import get_users_with_notifications
    users = await get_users_with_notifications()
    text = format_signal(sig)
    kb = signal_kb(sig.coin, sig.id)
    sent = blocked = 0
    for user in users:
        if not user.has_access():
            continue
        try:
            await bot.send_message(user.telegram_id, text, reply_markup=kb, parse_mode="HTML")
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramBadRequest as e:
            logger.warning(f"Bad request {user.telegram_id}: {e}")
        except Exception as e:
            logger.error(f"Send error {user.telegram_id}: {e}")
    logger.info(f"Broadcast #{sig.id}: sent={sent} blocked={blocked}")
    return sent, blocked
