from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
import logging

from app.services.user_service import (
    get_or_create_user, get_user, activate_subscription, get_user_count,
    toggle_notifications, activate_promo_code,
)
from app.services.signal_service import (
    generate_signal, get_stats, get_recent_signals,
    get_deposit_history, get_current_balance, recompute_stats,
    get_signal_by_id, get_latest_signals_all_coins, _cache,
)
from app.services.notifier import format_signal, format_full_analysis, format_market_overview, broadcast_signal
from app.keyboards.inline import (
    main_menu, signal_kb, full_analysis_kb, stats_kb, history_kb,
    subscription_kb, back_kb, overview_kb,
)
from config.settings import settings

logger = logging.getLogger(__name__)
router = Router()
PAGE_SIZE = 8


class PromoState(StatesGroup):
    waiting_code = State()


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    user, is_new = await get_or_create_user(
        msg.from_user.id, msg.from_user.username, msg.from_user.first_name)

    if is_new:
        ends = user.trial_ends_at.strftime("%d.%m.%Y %H:%M") if user.trial_ends_at else "—"
        text = (
            f"👋 <b>Добро пожаловать в DAO Signals!</b>\n\n"
            f"🎁 Бесплатный доступ на <b>{settings.TRIAL_DAYS} дня</b>\n"
            f"Пробный период до: <b>{ends} UTC</b>\n\n"
            f"<b>Анализирую BTC, ETH, SOL — AI сигналы LONG/SHORT</b>\n\n"
            f"Нажми 🔕 <b>Уведомления</b> чтобы получать сигналы автоматически 👇"
        )
    else:
        days = user.trial_days_left()
        status = (
            "✅ Подписка активна" if user.is_subscribed
            else f"⏳ Пробный период: {days}д" if days > 0
            else "❌ Доступ истёк"
        )
        text = f"👋 С возвращением, <b>{user.first_name or 'трейдер'}</b>!\n{status}"

    notif = user.notifications_enabled if hasattr(user, 'notifications_enabled') else False
    await msg.answer(text, reply_markup=main_menu(notif), parse_mode="HTML")


@router.callback_query(F.data == "main_menu")
async def cb_main(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    notif = user.notifications_enabled if user and hasattr(user, 'notifications_enabled') else False
    await call.message.edit_text("📡 <b>DAO Signals</b> — выберите действие:",
                                  reply_markup=main_menu(notif), parse_mode="HTML")
    await call.answer()


# ── Notifications toggle ──────────────────────────────────────────────────────

@router.callback_query(F.data == "toggle_notifications")
async def cb_toggle_notifications(call: CallbackQuery):
    new_state = await toggle_notifications(call.from_user.id)
    icon = "🔔" if new_state else "🔕"
    state_text = "включены" if new_state else "выключены"
    await call.answer(f"{icon} Уведомления {state_text}", show_alert=True)
    await call.message.edit_reply_markup(reply_markup=main_menu(new_state))


# ── Signals ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sig_"))
async def cb_signal(call: CallbackQuery):
    coin = call.data.split("_")[1]
    await call.answer(f"⚙️ Анализирую {coin}...")
    await call.message.edit_text(
        f"⏳ <b>Анализирую {coin}/USDT...</b>\n<i>~10–20 секунд</i>",
        parse_mode="HTML",
    )
    try:
        sig = await generate_signal(coin, use_cache=True)
    except Exception as e:
        logger.error(f"Signal error: {e}")
        await call.message.edit_text("❌ Ошибка генерации. Попробуй позже.", reply_markup=back_kb())
        return
    if not sig:
        await call.message.edit_text("⚠️ Не удалось получить данные. Попробуй позже.", reply_markup=back_kb())
        return
    await call.message.edit_text(format_signal(sig), reply_markup=signal_kb(coin, sig.id), parse_mode="HTML")


@router.callback_query(F.data.startswith("refresh_"))
async def cb_refresh(call: CallbackQuery):
    coin = call.data.split("_")[1]
    _cache.pop(coin, None)
    await call.answer(f"🔄 Обновляю {coin}...")
    await call.message.edit_text(
        f"⏳ <b>Получаю свежий анализ {coin}/USDT...</b>\n<i>~15–25 секунд</i>",
        parse_mode="HTML",
    )
    try:
        sig = await generate_signal(coin, use_cache=False)
    except Exception as e:
        logger.error(f"Refresh error: {e}")
        await call.message.edit_text("❌ Ошибка. Попробуй через минуту.", reply_markup=back_kb())
        return
    if not sig:
        await call.message.edit_text("⚠️ AI временно недоступен. Попробуй через минуту.", reply_markup=back_kb())
        return
    await call.message.edit_text(format_signal(sig), reply_markup=signal_kb(coin, sig.id), parse_mode="HTML")


# ── Full Analysis ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("full_"))
async def cb_full_analysis(call: CallbackQuery):
    try:
        sig_id = int(call.data.split("_")[1])
    except (IndexError, ValueError):
        await call.answer("❌ Ошибка", show_alert=True)
        return
    await call.answer("📝 Загружаю полный анализ...")
    sig = await get_signal_by_id(sig_id)
    if not sig:
        await call.message.edit_text("⚠️ Сигнал не найден.", reply_markup=back_kb())
        return
    text = format_full_analysis(sig)
    if len(text) > 4096:
        text = text[:4090] + "…"
    await call.message.edit_text(text, reply_markup=full_analysis_kb(sig.coin, sig.id), parse_mode="HTML")


@router.callback_query(F.data.startswith("back_sig_"))
async def cb_back_to_signal(call: CallbackQuery):
    try:
        sig_id = int(call.data.split("_")[2])
    except (IndexError, ValueError):
        await call.answer("❌ Ошибка", show_alert=True)
        return
    sig = await get_signal_by_id(sig_id)
    if not sig:
        await call.message.edit_text("⚠️ Сигнал не найден.", reply_markup=back_kb())
        return
    await call.message.edit_text(format_signal(sig), reply_markup=signal_kb(sig.coin, sig.id), parse_mode="HTML")
    await call.answer()


# ── Market Overview ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "overview")
async def cb_overview(call: CallbackQuery):
    await call.answer("🌐 Загружаю обзор рынка...")
    await call.message.edit_text(
        "⏳ <b>Загружаю последние данные BTC · ETH · SOL...</b>",
        parse_mode="HTML",
    )
    try:
        signals = await get_latest_signals_all_coins()
    except Exception as e:
        logger.error(f"Overview error: {e}")
        await call.message.edit_text("❌ Ошибка загрузки. Попробуй позже.", reply_markup=back_kb())
        return
    if not any(signals):
        await call.message.edit_text(
            "⚠️ Нет данных. Запроси сигнал по любой монете, чтобы появились данные.",
            reply_markup=overview_kb(),
            parse_mode="HTML",
        )
        return
    text = format_market_overview([s for s in signals if s])
    await call.message.edit_text(text, reply_markup=overview_kb(), parse_mode="HTML")


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "stats_menu")
async def cb_stats_menu(call: CallbackQuery):
    await call.message.edit_text("📊 <b>Статистика</b> — выбери период:",
                                  reply_markup=stats_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.in_({"stats_7d", "stats_30d", "stats_90d", "stats_all"}))
async def cb_stats(call: CallbackQuery):
    period = call.data.replace("stats_", "")
    labels = {"7d": "7 дней", "30d": "30 дней", "90d": "90 дней", "all": "Всё время"}
    s = await get_stats(period)

    if not s or s.total_signals == 0:
        text = f"📊 <b>Статистика · {labels[period]}</b>\n\nНет завершённых сделок."
    else:
        bar = "█" * int(s.win_rate / 10) + "░" * (10 - int(s.win_rate / 10))
        text = (
            f"📊 <b>Статистика · {labels[period]}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Сделок:        <b>{s.total_signals}</b>  ✅{s.wins} / ❌{s.losses}\n"
            f"Win Rate:      <b>{s.win_rate:.1f}%</b>  {bar}\n"
            f"Avg R/R:       <b>1:{s.avg_rr:.2f}</b>\n"
            f"Profit Factor: <b>{s.profit_factor:.2f}</b>\n"
            f"Мат. ожидание: <b>{'+' if s.expectancy >= 0 else ''}{s.expectancy:.2f}%</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Общий PnL:     <b>{'+' if s.total_pnl_pct >= 0 else ''}{s.total_pnl_pct:.2f}%</b>\n"
            f"Ср. выигрыш:   <b>+{s.avg_win_pct:.2f}%</b>\n"
            f"Ср. убыток:    <b>-{s.avg_loss_pct:.2f}%</b>\n"
            f"Макс. просадка:<b>-{s.max_drawdown:.2f}%</b>\n"
            f"Лучшая монета: <b>{s.best_coin or '—'}</b>\n"
        )
    await call.message.edit_text(text, reply_markup=stats_kb(), parse_mode="HTML")
    await call.answer()


# ── History ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("hist_"))
async def cb_history(call: CallbackQuery):
    parts = call.data.split("_")
    coin_filter = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0
    coin = None if coin_filter == "ALL" else coin_filter

    all_sigs = await get_recent_signals(coin=coin, limit=PAGE_SIZE * 10)
    total = len(all_sigs)
    page_sigs = all_sigs[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    has_next = (page + 1) * PAGE_SIZE < total

    if not page_sigs:
        text = "📋 <b>История</b>\n\nСигналов нет."
    else:
        st = {"WIN": "✅", "LOSS": "❌", "ACTIVE": "⏳", "EXPIRED": "⏸"}
        dr = {"LONG": "▲", "SHORT": "▼", "NO TRADE": "◆"}
        lines = []
        for s in page_sigs:
            pnl = f"{'+' if (s.outcome_pnl_pct or 0) >= 0 else ''}{s.outcome_pnl_pct:.2f}%" if s.outcome_pnl_pct else "—"
            lines.append(
                f"{st.get(s.status, '?')} <b>{s.coin}</b>{dr.get(s.direction, '')} "
                f"⭐{s.signal_rating} <code>${s.entry_price:,.2f}</code> → <b>{pnl}</b> "
                f"<i>{s.created_at.strftime('%d.%m %H:%M')}</i>"
            )
        header = f"📋 <b>История{' · ' + coin_filter if coin else ''}</b>  ({total})\n━━━━━━━━━━━━━━━━━━━━\n"
        text = header + "\n".join(lines)

    await call.message.edit_text(text, reply_markup=history_kb(coin_filter, page, has_next), parse_mode="HTML")
    await call.answer()


# ── Subscription ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "subscription")
async def cb_subscription(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    if user and user.is_subscribed and user.subscription_ends_at:
        status = f"✅ Активна до <b>{user.subscription_ends_at.strftime('%d.%m.%Y')}</b>"
    elif user and user.trial_active():
        status = f"⏳ Пробный период: <b>{user.trial_days_left()} дн.</b>"
    else:
        status = "❌ Нет доступа"
    text = (
        f"💳 <b>Подписка DAO Signals</b>\n\n"
        f"Статус: {status}\n\n"
        f"<b>Тарифы:</b>\n"
        f"  • 1 месяц   — <b>$29</b>\n"
        f"  • 3 месяца  — <b>$69</b>  (−21%)\n"
        f"  • 6 месяцев — <b>$149</b>  (−14%)\n\n"
        f"<b>Как оплатить:</b>\n"
        f"  1. Напиши @nn0likkkkk или @n3m1r\n"
        f"  2. Получи промокод\n"
        f"  3. Введи его кнопкой ниже 👇\n\n"
        f"<b>Включено:</b>\n"
        f"  • Сигналы LONG/SHORT каждый час\n"
        f"  • BTC, ETH, SOL\n"
        f"  • Уведомления и полный анализ"
    )
    await call.message.edit_text(text, reply_markup=subscription_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "enter_promo")
async def cb_enter_promo(call: CallbackQuery, state: FSMContext):
    await state.set_state(PromoState.waiting_code)
    await call.message.edit_text(
        "🎁 <b>Введи промокод:</b>\n\n"
        "<i>Промокод чувствителен к регистру — вводи заглавными буквами</i>",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(PromoState.waiting_code)
async def msg_promo_code(msg: Message, state: FSMContext):
    await state.clear()
    code = msg.text.strip().upper()
    success, text = await activate_promo_code(msg.from_user.id, code)
    user = await get_user(msg.from_user.id)
    notif = user.notifications_enabled if user and hasattr(user, 'notifications_enabled') else False
    await msg.answer(
        f"{text}\n\n{'Теперь у тебя есть полный доступ к сигналам!' if success else 'Попробуй другой код или напиши @nn0likkkkk'}",
        reply_markup=main_menu(notif),
        parse_mode="HTML",
    )




# ── Admin ─────────────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id not in settings.ADMIN_IDS:
        return
    counts = await get_user_count()
    s30 = await get_stats("30d")
    await msg.answer(
        f"🔧 <b>Admin Panel</b>\n\n"
        f"Всего: {counts['total']} | Триал: {counts['trial']} | Подписка: {counts['subscribed']}\n\n"
        f"30д: сделок={s30.total_signals if s30 else 0} winrate={s30.win_rate if s30 else 0:.1f}%\n\n"
        f"/force BTC|ETH|SOL — принудительный сигнал\n"
        f"/give_sub ID MONTHS — выдать подписку\n"
        f"/recompute — пересчитать статистику",
        parse_mode="HTML")


@router.message(Command("force"))
async def cmd_force(msg: Message):
    if msg.from_user.id not in settings.ADMIN_IDS:
        return
    parts = msg.text.split()
    coin = parts[1].upper() if len(parts) > 1 else "BTC"
    if coin not in ["BTC", "ETH", "SOL"]:
        await msg.answer("BTC, ETH или SOL")
        return
    await msg.answer(f"⏳ Генерирую {coin}...")
    sig = await generate_signal(coin, use_cache=False)
    if sig:
        sent, _ = await broadcast_signal(msg.bot, sig)
        await msg.answer(f"✅ #{sig.id} {sig.direction} разослан {sent} подписчикам")
    else:
        await msg.answer("❌ Ошибка генерации")


@router.message(Command("give_sub"))
async def cmd_give_sub(msg: Message):
    if msg.from_user.id not in settings.ADMIN_IDS:
        return
    parts = msg.text.split()
    if len(parts) < 3:
        await msg.answer("/give_sub USER_ID MONTHS")
        return
    try:
        ok = await activate_subscription(int(parts[1]), int(parts[2]))
        await msg.answer(f"{'✅' if ok else '❌'} Подписка {parts[2]}мес для {parts[1]}")
    except ValueError:
        await msg.answer("❌ Неверные параметры")


@router.message(Command("promocodes"))
async def cmd_promocodes(msg: Message):
    if msg.from_user.id not in settings.ADMIN_IDS:
        return
    from app.models.database import PromoCode, AsyncSessionLocal
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(PromoCode).where(PromoCode.is_used == False).order_by(PromoCode.months))
        codes = res.scalars().all()
    by_months: dict[int, list] = {1: [], 3: [], 6: []}
    for c in codes:
        by_months.setdefault(c.months, []).append(c.code)
    lines = ["📋 <b>Активные промокоды</b>\n"]
    for m, lst in sorted(by_months.items()):
        lines.append(f"<b>{m} мес. ({len(lst)} шт.):</b>")
        lines.append("\n".join(lst[:50]))  # first 50 per group
        if len(lst) > 50:
            lines.append(f"... и ещё {len(lst)-50}")
        lines.append("")
    text = "\n".join(lines)
    # split if too long
    for i in range(0, len(text), 4000):
        await msg.answer(text[i:i+4000], parse_mode="HTML")


@router.message(Command("polymarket"))
async def cmd_polymarket(msg: Message):
    if msg.from_user.id not in settings.ADMIN_IDS:
        return
    await msg.answer("⏳ Ищу лучшие ставки на Polymarket...")
    from app.services.polymarket import get_high_confidence_markets, format_polymarket_alert
    markets = await get_high_confidence_markets(min_conf=70.0)
    if not markets:
        await msg.answer("😔 Нет рынков с уверенностью ≥70% прямо сейчас")
        return
    text = format_polymarket_alert(markets)
    await msg.answer(text, parse_mode="HTML", disable_web_page_preview=False)


@router.message(Command("reload_promos"))
async def cmd_reload_promos(msg: Message):
    if msg.from_user.id not in settings.ADMIN_IDS:
        return
    from app.models.database import PromoCode, AsyncSessionLocal
    from app.services.user_service import save_promo_codes
    from sqlalchemy import delete
    await msg.answer("⏳ Перезагружаю промокоды...")
    async with AsyncSessionLocal() as db:
        await db.execute(delete(PromoCode).where(PromoCode.is_used == False))
        await db.commit()
    total = await save_promo_codes()
    await msg.answer(f"✅ Загружено {total} промокодов (100x1м + 100x3м + 100x6м)")


@router.message(Command("recompute"))
async def cmd_recompute(msg: Message):
    if msg.from_user.id not in settings.ADMIN_IDS:
        return
    await recompute_stats()
    await msg.answer("✅ Статистика пересчитана")
