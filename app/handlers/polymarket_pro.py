from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton as Btn
from app.services.user_service import get_user
from app.keyboards.inline import back_kb, main_menu

router = Router()

POLY_REF = "https://polymarket.com/markets/crypto?via=max-chron0n"
POLY_REGISTER = "https://polymarket.com/?via=max-chron0n"

_ONBOARDING = [
    # step 0
    (
        "🎯 <b>Polymarket — что это?</b>\n\n"
        "Polymarket — крупнейший рынок предсказаний в мире.\n"
        "Ты ставишь реальные деньги (USDC) на исходы событий:\n\n"
        "  • 💵 <b>BTC выше $100k до конца месяца?</b>\n"
        "  • 📈 <b>ETH вырастет на 20% за неделю?</b>\n"
        "  • ⚡ <b>SOL установит новый ATH в 2025?</b>\n\n"
        "Если ты угадал — получаешь выплату.\n"
        "Если нет — теряешь ставку.\n\n"
        "Работает на блокчейне Polygon — всё прозрачно."
    ),
    # step 1
    (
        "📝 <b>Шаг 1 — Регистрация</b>\n\n"
        "1. Перейди по ссылке:\n"
        f"👉 <a href=\"{POLY_REGISTER}\">Зарегистрироваться на Polymarket</a>\n\n"
        "2. Нажми <b>Sign Up</b>\n"
        "3. Введи email или подключи кошелёк\n"
        "4. Подтверди email\n\n"
        "✅ Аккаунт создан — переходи к следующему шагу"
    ),
    # step 2
    (
        "💰 <b>Шаг 2 — Пополнение счёта</b>\n\n"
        "Polymarket работает только с <b>USDC</b> на сети <b>Polygon</b>.\n\n"
        "<b>Способы пополнения:</b>\n"
        "  • Банковская карта (Stripe) — прямо на сайте\n"
        "  • Крипто-перевод USDC на Polygon\n"
        "  • Через MetaMask/Coinbase Wallet\n\n"
        "<b>Минимальная ставка:</b> $1 USDC\n"
        "<b>Рекомендуем начать с:</b> $10–50 USDC\n\n"
        "⚠️ Не вкладывай больше, чем готов потерять"
    ),
    # step 3
    (
        "🎯 <b>Шаг 3 — Как сделать ставку</b>\n\n"
        "1. Зайди в раздел <b>Crypto</b>\n"
        "2. Найди рынок по BTC/ETH/SOL\n"
        "3. Выбери <b>YES</b> (вырастет) или <b>NO</b> (нет)\n"
        "4. Введи сумму ставки\n"
        "5. Нажми <b>Buy</b> и подтверди транзакцию\n\n"
        "<b>Пример:</b>\n"
        "  BTC выше $105,000 в июне?\n"
        "  YES @ <b>42%</b> → ставишь $10 → получаешь $23.8 если да\n\n"
        f"👉 <a href=\"{POLY_REF}\">Открыть крипто-рынки</a>"
    ),
    # step 4
    (
        "🤖 <b>Шаг 4 — Как использовать сигналы PredictBot</b>\n\n"
        "Алгоритм работы:\n\n"
        "  1️⃣ Получи сигнал LONG/SHORT в боте\n"
        "  2️⃣ Проверь уверенность сигнала (<b>>70%</b> = надёжный)\n"
        "  3️⃣ Найди соответствующий рынок на Polymarket\n"
        "  4️⃣ Поставь на YES если LONG, NO если SHORT\n\n"
        "<b>💡 Совет:</b> Следи за алертами BTC 5м/15м —\n"
        "они приходят за несколько минут до движения цены.\n\n"
        "✅ <b>Онбординг завершён! Удачных ставок 🎯</b>"
    ),
]


def _onboarding_kb(step: int) -> InlineKeyboardMarkup:
    total = len(_ONBOARDING)
    nav = []
    if step > 0:
        nav.append(Btn(text="← Назад", callback_data=f"poly_step_{step - 1}"))
    if step < total - 1:
        nav.append(Btn(text="Далее →", callback_data=f"poly_step_{step + 1}"))
    rows = []
    if nav:
        rows.append(nav)
    if step == total - 1:
        rows.append([Btn(text="🎯 Открыть Polymarket", url=POLY_REF)])
    rows.append([Btn(text="« Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _alerts_kb(alerts_on: bool) -> InlineKeyboardMarkup:
    toggle_text = "🔔 Алерты: ВКЛ" if alerts_on else "🔕 Алерты: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text=toggle_text, callback_data="toggle_btc_alerts")],
        [Btn(text="📚 Онбординг Polymarket", callback_data="poly_step_0")],
        [Btn(text="🎯 Открыть Polymarket", url=POLY_REF)],
        [Btn(text="« Меню", callback_data="main_menu")],
    ])


def _premium_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="💳 Оформить подписку", callback_data="subscription")],
        [Btn(text="« Меню", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "poly_pro")
async def cb_poly_pro(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    if not user or not user.has_access():
        await call.message.edit_text(
            "🔒 <b>Polymarket Pro</b> — только для подписчиков\n\n"
            "Включено в подписку:\n"
            "  🎯 Полный онбординг в Polymarket\n"
            "  ⚡ 4 вида алертов по BTC 5м/15м\n"
            "  📈 RSI экстремум, MACD кросс, BB пробой, объём\n\n"
            "Оформи подписку чтобы получить доступ 👇",
            reply_markup=_premium_kb(), parse_mode="HTML"
        )
        await call.answer()
        return

    alerts_on = getattr(user, "btc_alerts_enabled", False)
    await call.message.edit_text(
        "🎯 <b>Polymarket Pro</b>\n\n"
        "⚡ <b>4 алерта по BTC 5м/15м:</b>\n"
        "  • RSI экстремум (>75 / <25) на 15м\n"
        "  • MACD кроссовер на 15м\n"
        "  • Пробой Bollinger Bands на 5м\n"
        "  • Спайк объёма 2x+ на 5м\n\n"
        "Алерты приходят раньше, чем большинство трейдеров замечают движение.\n\n"
        "📚 Не знаешь как использовать Polymarket? Пройди онбординг ниже 👇",
        reply_markup=_alerts_kb(alerts_on), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("poly_step_"))
async def cb_poly_step(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    if not user or not user.has_access():
        await call.answer("🔒 Только для подписчиков", show_alert=True)
        return

    try:
        step = int(call.data.split("_")[2])
    except (IndexError, ValueError):
        step = 0

    step = max(0, min(step, len(_ONBOARDING) - 1))
    total = len(_ONBOARDING)
    header = f"📚 <b>Онбординг Polymarket</b>  [{step + 1}/{total}]\n\n"
    await call.message.edit_text(
        header + _ONBOARDING[step],
        reply_markup=_onboarding_kb(step), parse_mode="HTML",
        disable_web_page_preview=True
    )
    await call.answer()


@router.callback_query(F.data == "toggle_btc_alerts")
async def cb_toggle_btc_alerts(call: CallbackQuery):
    from app.models.database import AsyncSessionLocal, User
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.telegram_id == call.from_user.id))
        user = res.scalar_one_or_none()
        if not user:
            await call.answer("Ошибка", show_alert=True)
            return
        if not user.has_access():
            await call.answer("🔒 Только для подписчиков", show_alert=True)
            return
        current = getattr(user, "btc_alerts_enabled", False)
        user.btc_alerts_enabled = not current
        await db.commit()
        await db.refresh(user)
        new_state = user.btc_alerts_enabled

    icon = "🔔" if new_state else "🔕"
    state_text = "включены" if new_state else "выключены"
    await call.answer(f"{icon} BTC алерты {state_text}", show_alert=True)
    await call.message.edit_reply_markup(reply_markup=_alerts_kb(new_state))
