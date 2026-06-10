from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton as Btn
from app.services.user_service import get_user
from app.keyboards.inline import back_kb, main_menu, subscription_kb
import logging

router = Router()
logger = logging.getLogger(__name__)

POLY_REF     = "https://polymarket.com/markets/crypto?via=max-chron0n"
POLY_REG     = "https://polymarket.com/?via=max-chron0n"
POLY_BTC     = "https://polymarket.com/markets/crypto/bitcoin?via=max-chron0n"
POLY_ETH     = "https://polymarket.com/markets/crypto/ethereum?via=max-chron0n"
POLY_SOL     = "https://polymarket.com/markets/crypto/solana?via=max-chron0n"
POLY_WC      = "https://polymarket.com/markets/sports/soccer?via=max-chron0n"

# World Cup 2026 match predictions based on FIFA rankings & form
_WC_MATCHES = [
    ("🇧🇷 Бразилия", "🇨🇷 Коста-Рика",  "Бразилия",    "Явный фаворит, топ-5 FIFA"),
    ("🇫🇷 Франция",  "🇲🇽 Мексика",     "Франция",     "Действующий чемпион"),
    ("🇦🇷 Аргентина","🇸🇦 Саудовская Аравия", "Аргентина", "Мировой чемпион, Месси"),
    ("🏴󠁧󠁢󠁥󠁮󠁧󠁿 Англия",   "🇨🇴 Колумбия",   "Англия",      "Сильная сборная, FIFA топ-10"),
    ("🇩🇪 Германия", "🇯🇵 Япония",      "Ничья/Япония","Япония опрокидывала Германию в 2022"),
    ("🇪🇸 Испания",  "🇭🇷 Хорватия",    "Испания",     "Доминирующий стиль, молодёжь"),
    ("🇵🇹 Португалия","🇺🇸 США",         "Португалия",  "Роналду, опыт на ЧМ"),
    ("🇳🇱 Нидерланды","🇸🇳 Сенегал",    "Нидерланды",  "Ван Дейк, ди Йонг — стабильны"),
    ("🇺🇾 Уругвай",  "🇰🇷 Южная Корея", "Уругвай",     "Нуньес в форме, атака топ"),
    ("🇲🇦 Марокко",  "🇨🇦 Канада",      "Марокко",     "Полуфиналисты 2022, дома мотивация"),
]


def _wc_text() -> str:
    lines = []
    for home, away, pick, reason in _WC_MATCHES:
        lines.append(f"{home} vs {away}\n  → <b>{pick}</b> <i>({reason})</i>")
    return (
        "⚽ <b>ЧМ 2026 — ставки Polymarket</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Прогнозы на основе FIFA-рейтинга и формы команд:\n\n"
        + "\n\n".join(lines) +
        "\n\n━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Нажми кнопку ниже чтобы найти матч на Polymarket</i>"
    )


_GUIDE = [
    (
        "🎯 <b>Polymarket — рынок предсказаний</b>\n\n"
        "Ты ставишь USDC на исход события:\n"
        "  <b>YES</b> — цена вырастет / событие произойдёт\n"
        "  <b>NO</b>  — цена не вырастет / не произойдёт\n\n"
        "Примеры рынков:\n"
        "  • BTC выше $70 000 до конца июня?\n"
        "  • ETH выше $4 000 в июле?\n"
        "  • SOL установит новый ATH в 2025?\n\n"
        "Если угадал → получаешь $1 за каждую долю\n"
        "Если нет → теряешь вложенное\n\n"
        "<i>Работает на блокчейне Polygon. Всё прозрачно.</i>"
    ),
    (
        "📝 <b>Как зарегистрироваться</b>\n\n"
        f"1. Перейди: <a href=\"{POLY_REG}\">polymarket.com</a>\n"
        "2. Нажми <b>Sign Up</b>\n"
        "3. Войди через Google или email\n"
        "4. Подтверди возраст (18+)\n\n"
        "✅ Всё — аккаунт готов за 30 секунд\n\n"
        "<b>Пополнение счёта:</b>\n"
        "  • Банковская карта (Stripe) — прямо на сайте\n"
        "  • USDC на сети Polygon\n"
        "  • Минимум: $1\n\n"
        "<i>Рекомендуем начать с $10–50 USDC</i>"
    ),
    (
        "💡 <b>Как делать ставки</b>\n\n"
        "1. Зайди в раздел <b>Crypto</b>\n"
        "2. Найди рынок (например, BTC &gt; $100k)\n"
        "3. Посмотри текущую цену YES/NO\n"
        "   Пример: YES @ <b>35%</b> = за $3.50 получишь $10 при победе\n"
        "4. Нажми <b>Buy</b> → введи сумму → подтверди\n\n"
        "<b>Стратегия с нашими сигналами:</b>\n"
        "  🟢 Сигнал LONG → ставь YES на рост\n"
        "  🔴 Сигнал SHORT → ставь NO\n"
        "  ⚡ Прогноз 3–5м → для краткосрочных рынков\n\n"
        "<i>Никогда не ставь больше 5–10% депозита на одну ставку</i>"
    ),
    (
        "📊 <b>Текущие крипто-рынки Polymarket</b>\n\n"
        "Нажми на монету чтобы увидеть\n"
        "актуальные рынки и рекомендации 👇"
    ),
]


def _guide_kb(step: int) -> InlineKeyboardMarkup:
    total = len(_GUIDE)
    nav = []
    if step > 0:
        nav.append(Btn(text="← Назад", callback_data=f"pg_step_{step - 1}"))
    if step < total - 1:
        nav.append(Btn(text="Далее →", callback_data=f"pg_step_{step + 1}"))
    rows = []
    if nav:
        rows.append(nav)
    if step == total - 1:
        rows.append([
            Btn(text="₿ BTC рынки", callback_data="pg_markets_BTC"),
            Btn(text="Ξ ETH рынки", callback_data="pg_markets_ETH"),
        ])
        rows.append([Btn(text="◎ SOL рынки", callback_data="pg_markets_SOL")])
    rows.append([Btn(text="« Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _main_kb(alerts_on: bool) -> InlineKeyboardMarkup:
    toggle = "🔔 Алерты: ВКЛ" if alerts_on else "🔕 Алерты: ВЫКЛ"
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="📚 Гайд по Polymarket", callback_data="pg_step_0")],
        [Btn(text="₿ BTC ставки",  callback_data="pg_markets_BTC"),
         Btn(text="Ξ ETH ставки",  callback_data="pg_markets_ETH")],
        [Btn(text="◎ SOL ставки",  callback_data="pg_markets_SOL")],
        [Btn(text="⚽ ЧМ 2026 — прогнозы", callback_data="pg_wc2026")],
        [Btn(text=toggle, callback_data="toggle_btc_alerts")],
        [Btn(text="🎯 Открыть Polymarket", url=POLY_REF)],
        [Btn(text="« Меню", callback_data="main_menu")],
    ])


def _markets_kb(coin: str) -> InlineKeyboardMarkup:
    urls = {"BTC": POLY_BTC, "ETH": POLY_ETH, "SOL": POLY_SOL}
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text=f"🎯 Открыть {coin} рынки", url=urls.get(coin, POLY_REF))],
        [Btn(text="« Назад", callback_data="poly_pro")],
    ])


def _wc_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="🎯 Ставить на ЧМ 2026", url=POLY_WC)],
        [Btn(text="« Назад", callback_data="poly_pro")],
    ])


@router.callback_query(F.data == "poly_pro")
async def cb_poly_pro(call: CallbackQuery):
    user = await get_user(call.from_user.id)
    if not user or not user.has_access():
        await call.answer("🔒 Только для подписчиков", show_alert=True)
        await call.message.edit_text(
            "🔒 <b>Polymarket Pro — по подписке</b>\n\n"
            "Полный гайд, ставки по BTC/ETH/SOL и прогнозы на ЧМ 2026.",
            reply_markup=subscription_kb(), parse_mode="HTML"
        )
        return
    alerts_on = getattr(user, "btc_alerts_enabled", False)
    await call.message.edit_text(
        "🎯 <b>Polymarket Pro</b>\n\n"
        "Здесь ты найдёшь:\n"
        "  📚 Полный гайд как зарабатывать на Polymarket\n"
        "  💰 Актуальные ставки по BTC / ETH / SOL\n"
        "  ⚽ Прогнозы на матчи ЧМ 2026\n"
        "  ⚡ Алерты когда рынок даёт точку входа\n\n"
        "Используй прогнозы бота чтобы знать куда ставить 👇",
        reply_markup=_main_kb(alerts_on), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "pg_wc2026")
async def cb_wc2026(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        _wc_text(),
        reply_markup=_wc_kb(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("pg_step_"))
async def cb_guide_step(call: CallbackQuery):
    try:
        step = int(call.data.split("_")[2])
    except (IndexError, ValueError):
        step = 0
    step = max(0, min(step, len(_GUIDE) - 1))
    header = f"📚 <b>Гайд Polymarket</b>  [{step + 1}/{len(_GUIDE)}]\n\n"
    await call.message.edit_text(
        header + _GUIDE[step],
        reply_markup=_guide_kb(step),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await call.answer()


@router.callback_query(F.data.startswith("pg_markets_"))
async def cb_markets(call: CallbackQuery):
    coin = call.data.split("_")[2]
    await call.answer(f"⏳ Загружаю {coin} рынки...")

    try:
        from app.services.short_forecast import get_short_forecast
        forecast = await get_short_forecast(coin)
        direction = forecast["direction"]
        conf = forecast["confidence"]
        price = forecast["price"]
        price_str = f"${price:,.2f}" if price >= 1000 else f"${price:.4f}"

        if direction == "UP":
            rec = f"✅ <b>Рекомендация: YES</b> — ожидается рост\nИщи рынки вида «{coin} выше X$»"
        elif direction == "DOWN":
            rec = f"❌ <b>Рекомендация: NO</b> — ожидается падение\nИщи рынки вида «{coin} выше X$» → ставь NO"
        else:
            rec = f"⏸ <b>Рекомендация: подожди</b> — нет чёткого сигнала"

        text = (
            f"💰 <b>{coin}/USDT — ставки Polymarket</b>\n\n"
            f"📍 Цена сейчас: <b>{price_str}</b>\n"
            f"📊 Прогноз 3–5м: <b>{'Рост ↑' if direction == 'UP' else 'Падение ↓' if direction == 'DOWN' else 'Боковик ↔'}</b>  ({conf}%)\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{rec}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Как найти нужный рынок:</b>\n"
            f"  1. Нажми кнопку ниже → откроется Polymarket\n"
            f"  2. Найди рынок с ценой близкой к текущей\n"
            f"  3. Ставь YES или NO по рекомендации выше\n"
        )
    except Exception as e:
        logger.error(f"pg_markets {coin}: {e}")
        text = f"💰 <b>{coin} — рынки Polymarket</b>\n\nНажми кнопку ниже чтобы открыть актуальные рынки 👇"

    await call.message.edit_text(text, reply_markup=_markets_kb(coin),
                                  parse_mode="HTML", disable_web_page_preview=True)


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
        user.btc_alerts_enabled = not getattr(user, "btc_alerts_enabled", False)
        await db.commit()
        new_state = user.btc_alerts_enabled

    icon = "🔔" if new_state else "🔕"
    await call.answer(f"{icon} Алерты {'включены' if new_state else 'выключены'}", show_alert=True)
    await call.message.edit_reply_markup(reply_markup=_main_kb(new_state))
