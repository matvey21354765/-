from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton as Btn

router = Router()

BYBIT_REF = "https://www.bybit.com/invite?ref=EGB5O0&medium=referral&utm_campaign=evergreen"

_PAGES = [
    (
        "📈 <b>Гайд по фьючерсам</b>  [1/6]\n\n"
        "<b>Что такое фьючерс?</b>\n\n"
        "Фьючерс — контракт на покупку/продажу актива в будущем по заранее оговорённой цене.\n\n"
        "На крипто-биржах (Bybit) это означает:\n"
        "  • Торгуешь не реальной монетой, а <b>контрактом</b>\n"
        "  • Можешь зарабатывать и на росте (<b>LONG</b>), и на падении (<b>SHORT</b>)\n"
        "  • Используешь <b>плечо</b> — торгуешь суммой в 5–20× больше депозита\n\n"
        "Пример: депозит $100 + плечо 10× = позиция $1000\n"
        "Цена выросла на 1% → ты заработал 10% = $10\n"
        "Цена упала на 1% → ты потерял 10% = $10\n\n"
        "<i>Плечо усиливает и прибыль, и убытки одинаково</i>"
    ),
    (
        "📈 <b>Гайд по фьючерсам</b>  [2/6]\n\n"
        "<b>LONG и SHORT — базовые понятия</b>\n\n"
        "🟢 <b>LONG (лонг)</b> — ставка на рост:\n"
        "  Открываешь LONG → цена растёт → ты в плюсе\n"
        "  Открываешь LONG → цена падает → ты в минусе\n\n"
        "🔴 <b>SHORT (шорт)</b> — ставка на падение:\n"
        "  Открываешь SHORT → цена падает → ты в плюсе\n"
        "  Открываешь SHORT → цена растёт → ты в минусе\n\n"
        "<b>Как использовать с сигналами бота:</b>\n"
        "  ⚡ Бот дал сигнал <b>LONG</b> → открывай BUY на Bybit\n"
        "  ⚡ Бот дал сигнал <b>SHORT</b> → открывай SELL на Bybit\n\n"
        "<i>Всегда смотри направление сигнала перед входом</i>"
    ),
    (
        "📈 <b>Гайд по фьючерсам</b>  [3/6]\n\n"
        "<b>Stop Loss и Take Profit — защита капитала</b>\n\n"
        "🛡 <b>Stop Loss (SL)</b> — автоматически закрывает сделку при убытке:\n"
        "  Открыл BTC LONG по $60 000\n"
        "  Поставил SL на $59 400 (−1%)\n"
        "  Цена упала до $59 400 → сделка закрылась, потерял только 1%\n\n"
        "🎯 <b>Take Profit (TP)</b> — фиксирует прибыль:\n"
        "  Поставил TP на $60 900 (+1.5%)\n"
        "  Цена выросла до $60 900 → сделка закрылась автоматически в плюсе\n\n"
        "<b>Правило бота:</b>\n"
        "  SL = 2× ATR от цены входа\n"
        "  TP1 = 2× ATR, TP2 = 4× ATR\n\n"
        "<i>Никогда не торгуй без стоп-лосса!</i>"
    ),
    (
        "📈 <b>Гайд по фьючерсам</b>  [4/6]\n\n"
        "<b>Плечо — как выбрать безопасное</b>\n\n"
        "Плечо (leverage) умножает позицию, но и риск ликвидации растёт.\n\n"
        "<b>Ликвидация</b> — биржа принудительно закрывает позицию при большом убытке:\n"
        "  Плечо 10× → ликвидация при движении −10% против тебя\n"
        "  Плечо 20× → ликвидация при движении −5% против тебя\n\n"
        "<b>Рекомендации по плечу:</b>\n"
        "  🟢 Новичок: <b>2–3×</b> — безопасно, учишься\n"
        "  🟡 Опытный: <b>5–10×</b> — нормально при жёстком SL\n"
        "  🔴 Агрессивный: <b>10–20×</b> — только для профи\n\n"
        "<b>Правило:</b> рискуй не более 1–2% депозита на одну сделку\n\n"
        "<i>Не гонись за большим плечом — гонись за стабильностью</i>"
    ),
    (
        "📈 <b>Гайд по фьючерсам</b>  [5/6]\n\n"
        "<b>Как открыть сделку на Bybit</b>\n\n"
        "1. Зарегистрируйся на Bybit (ссылка ниже)\n"
        "2. Пополни счёт USDT\n"
        "3. Перейди в <b>Derivatives → USDT Perpetual</b>\n"
        "4. Выбери пару: BTCUSDT / ETHUSDT / SOLUSDT\n"
        "5. Установи плечо (рекомендуем 3–5×)\n"
        "6. Нажми <b>Buy/Long</b> или <b>Sell/Short</b>\n"
        "7. Укажи сумму позиции\n"
        "8. Выставь <b>Stop Loss</b> и <b>Take Profit</b>\n"
        "9. Подтверди сделку\n\n"
        "<b>Бот даёт:</b>\n"
        "  • Направление (LONG/SHORT)\n"
        "  • Цену входа\n"
        "  • Уровни SL и TP1/TP2\n\n"
        "<i>Просто копируй параметры из сигнала → вставляй на Bybit</i>"
    ),
    (
        "📈 <b>Гайд по фьючерсам</b>  [6/6]\n\n"
        "<b>Золотые правила трейдера</b>\n\n"
        "✅ Всегда ставь Stop Loss\n"
        "✅ Рискуй не более 1–2% депозита за сделку\n"
        "✅ Не торгуй на эмоциях — следуй сигналу\n"
        "✅ Начинай с малого плеча (2–5×)\n"
        "✅ Не усредняй убыточные позиции\n"
        "✅ Фиксируй прибыль частями (TP1 → TP2)\n\n"
        "❌ Не торгуй против тренда\n"
        "❌ Не ставь всё на одну сделку\n"
        "❌ Не игнорируй SL «в надежде что отобьётся»\n"
        "❌ Не используй плечо 20× пока не научился\n\n"
        "🎯 <b>Зарегистрируйся на Bybit</b> и начни торговать\n"
        "со скидкой на комиссию по нашей ссылке 👇"
    ),
]


def _guide_kb(step: int) -> InlineKeyboardMarkup:
    total = len(_PAGES)
    nav = []
    if step > 0:
        nav.append(Btn(text="← Назад", callback_data=f"fg_step_{step - 1}"))
    if step < total - 1:
        nav.append(Btn(text="Далее →", callback_data=f"fg_step_{step + 1}"))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([Btn(text="🚀 Открыть Bybit", url=BYBIT_REF)])
    rows.append([Btn(text="« Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "futures_guide")
async def cb_futures_guide(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        _PAGES[0],
        reply_markup=_guide_kb(0),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("fg_step_"))
async def cb_fg_step(call: CallbackQuery):
    try:
        step = int(call.data.split("_")[2])
    except (IndexError, ValueError):
        step = 0
    step = max(0, min(step, len(_PAGES) - 1))
    await call.answer()
    await call.message.edit_text(
        _PAGES[step],
        reply_markup=_guide_kb(step),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
