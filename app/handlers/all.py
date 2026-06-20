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
    subscription_kb, back_kb, overview_kb, start_kb,
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
    user, _ = await get_or_create_user(
        msg.from_user.id, msg.from_user.username, msg.from_user.first_name)

    name = user.first_name or "трейдер"
    text = (
        f"👋 Привет, <b>{name}</b>!\n\n"
        f"🤖 <b>PredictBot</b> — сигналы LONG/SHORT для BTC, ETH, SOL\n\n"
        f"Нажми кнопку ниже ↓"
    )
    await msg.answer(text, reply_markup=start_kb(), parse_mode="HTML")


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    notif = getattr(user, "notifications_enabled", False) if user else False
    await call.message.edit_text(
        "📡 <b>PredictBot</b> — выберите действие:",
        reply_markup=main_menu(notif), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "open_menu")
async def cb_open_menu(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    notif = getattr(user, "notifications_enabled", False) if user else False
    await call.message.edit_text(
        "📡 <b>PredictBot</b> — выберите действие:",
        reply_markup=main_menu(notif), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "main_menu")
async def cb_main(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    notif = user.notifications_enabled if user and hasattr(user, 'notifications_enabled') else False
    await call.message.edit_text("📡 <b>PredictBot</b> — выберите действие:",
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
    from app.services.user_service import get_or_create_user
    user, _ = await get_or_create_user(call.from_user.id, call.from_user.username, call.from_user.full_name)
    if not user.has_access():
        await call.answer("🔒 Только для подписчиков", show_alert=True)
        await call.message.edit_text(
            "🔒 <b>Сигналы BTC/ETH/SOL — по подписке</b>\n\n"
            "Получи полный анализ с направлением, входом, SL и TP.",
            reply_markup=subscription_kb(), parse_mode="HTML"
        )
        return
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
    from app.services.user_service import get_or_create_user
    user, _ = await get_or_create_user(call.from_user.id, call.from_user.username, call.from_user.full_name)
    if not user.has_access():
        await call.answer("🔒 Только для подписчиков", show_alert=True)
        await call.message.edit_text(
            "🔒 <b>Обзор рынка — по подписке</b>\n\n"
            "Полный анализ BTC, ETH и SOL в одном экране.",
            reply_markup=subscription_kb(), parse_mode="HTML"
        )
        return
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
    else:
        status = "❌ Нет доступа"
    text = (
        f"💳 <b>Подписка PredictBot</b>\n\n"
        f"Статус: {status}\n\n"
        f"<b>Тарифы PredictBot:</b>\n"
        f"  • 1 месяц   — <s>$30</s> → <b>$19</b> 🔥\n"
        f"  • 3 месяца  — <b>$49</b>\n"
        f"  • Навсегда  — <b>$149</b>\n\n"
        f"<b>🔒 Вместе с приваткой @n3m1r (выгоднее):</b>\n"
        f"  • 1 месяц   — <b>$39</b>\n"
        f"  • 3 месяца  — <b>$69</b>\n"
        f"  • Навсегда  — <b>$249</b>\n\n"
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
    await msg.answer("⏳ Ищу дневные ставки на Polymarket...")
    from app.services.binance import get_full_snapshot
    from app.services.polymarket import get_daily_poly_signals
    import asyncio
    snaps = {}
    for coin in settings.COINS:
        try:
            snaps[coin] = await get_full_snapshot(coin)
        except Exception:
            pass
    texts = await get_daily_poly_signals(snaps)
    if not texts:
        await msg.answer("😔 Дневные рынки не найдены. Попробуй позже.")
        return
    for text in texts:
        await msg.answer(text, parse_mode="HTML", disable_web_page_preview=False)
        await asyncio.sleep(0.3)


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


@router.message(Command("revoke_promo"))
async def cmd_revoke_promo(msg: Message):
    """Admin: revoke a promo code and remove the subscription of whoever used it.
    Usage: /revoke_promo DAO1M-WNNHJ7XV"""
    if msg.from_user.id not in settings.ADMIN_IDS:
        return
    parts = msg.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Использование: /revoke_promo <КОД>")
        return
    code = parts[1].strip().upper()
    from app.models.database import PromoCode, User, AsyncSessionLocal
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(PromoCode).where(PromoCode.code == code))
        promo = res.scalar_one_or_none()
        if not promo:
            await msg.answer(f"❌ Промокод <code>{code}</code> не найден в БД", parse_mode="HTML")
            return
        used_by = promo.used_by
        promo.is_used = False
        promo.used_by = None
        promo.used_at = None
        if used_by:
            res2 = await db.execute(select(User).where(User.telegram_id == used_by))
            user = res2.scalar_one_or_none()
            if user:
                user.is_subscribed = False
                user.subscription_ends_at = None
                await db.commit()
                await msg.answer(
                    f"✅ Промокод <code>{code}</code> отозван.\n"
                    f"Подписка пользователя <code>{used_by}</code> (@{user.username or '—'}) удалена.",
                    parse_mode="HTML"
                )
                return
        await db.commit()
    await msg.answer(
        f"✅ Промокод <code>{code}</code> сброшен (никем не был использован или пользователь не найден).",
        parse_mode="HTML"
    )


@router.message(Command("recompute"))
async def cmd_recompute(msg: Message):
    if msg.from_user.id not in settings.ADMIN_IDS:
        return
    await recompute_stats()
    await msg.answer("✅ Статистика пересчитана")


@router.message(Command("test_data"))
async def cmd_test_data(msg: Message):
    if msg.from_user.id not in settings.ADMIN_IDS:
        return
    await msg.answer("⏳ Тестирую источники данных...")
    import aiohttp
    from app.services.short_forecast import _bybit_klines, _kraken_klines
    from app.services.binance import _okx_klines, _TIMEOUT

    lines = []
    for name, coro in [
        ("OKX 1m",    _okx_klines("BTCUSDT", "1m", 5)),
        ("OKX 5m",    _okx_klines("BTCUSDT", "5m", 5)),
        ("Bybit 1m",  _bybit_klines("BTCUSDT", "1m", 5)),
        ("Bybit 5m",  _bybit_klines("BTCUSDT", "5m", 5)),
        ("Kraken 1m", _kraken_klines("BTCUSDT", "1m", 5)),
        ("Kraken 5m", _kraken_klines("BTCUSDT", "5m", 5)),
    ]:
        try:
            data = await coro
            lines.append(f"✅ {name}: {len(data)} свечей, last close={data[-1][4] if data else '?'}")
        except Exception as e:
            lines.append(f"❌ {name}: {type(e).__name__}: {e}")

    await msg.answer("\n".join(lines))
