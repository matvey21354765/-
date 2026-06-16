from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton as Btn
from app.services.short_forecast import get_short_forecast, format_forecast, format_forecast_free
import logging

router = Router()
logger = logging.getLogger(__name__)

POLY_REF = "https://polymarket.com/markets/crypto?via=max-chron0n"


def _forecast_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="₿ BTC", callback_data="fcast_BTC"),
         Btn(text="Ξ ETH", callback_data="fcast_ETH"),
         Btn(text="◎ SOL", callback_data="fcast_SOL")],
        [Btn(text="🔄 Обновить все", callback_data="fcast_all")],
        [Btn(text="🎯 Polymarket", url=POLY_REF)],
        [Btn(text="« Меню", callback_data="main_menu")],
    ])


def _forecast_kb(coin: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="🔄 Обновить", callback_data=f"fcast_{coin}"),
         Btn(text="🎯 Ставить", url=POLY_REF)],
        [Btn(text="₿ BTC", callback_data="fcast_BTC"),
         Btn(text="Ξ ETH", callback_data="fcast_ETH"),
         Btn(text="◎ SOL", callback_data="fcast_SOL")],
        [Btn(text="« Меню", callback_data="main_menu")],
    ])


def _forecast_kb_free(coin: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [Btn(text="🔓 Открыть полный сигнал", callback_data="subscription")],
        [Btn(text="🔄 Обновить", callback_data=f"fcast_{coin}"),
         Btn(text="🎯 Polymarket", url=POLY_REF)],
        [Btn(text="₿ BTC", callback_data="fcast_BTC"),
         Btn(text="Ξ ETH", callback_data="fcast_ETH"),
         Btn(text="◎ SOL", callback_data="fcast_SOL")],
        [Btn(text="« Меню", callback_data="main_menu")],
    ])


async def _get_user(call: CallbackQuery):
    try:
        from app.services.user_service import get_or_create_user
        user, _ = await get_or_create_user(call.from_user.id, call.from_user.username,
                                           call.from_user.full_name)
        return user
    except Exception:
        return None


@router.callback_query(F.data == "forecast_menu")
async def cb_forecast_menu(call: CallbackQuery):
    await call.message.edit_text(
        "⚡ <b>Прогноз 3–5 минут</b>\n\n"
        "Выбери монету — получи анализ индикаторов прямо сейчас 👇\n"
        "<i>Полный сигнал с входом и SL/TP — по подписке</i>",
        reply_markup=_forecast_menu_kb(), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("fcast_"))
async def cb_forecast(call: CallbackQuery):
    coin_part = call.data.split("_")[1]

    if coin_part == "all":
        await call.answer("⏳ Считаю прогнозы...")
        await call.message.edit_text(
            "⏳ <b>Анализирую BTC, ETH, SOL...</b>\n<i>~5 секунд</i>",
            parse_mode="HTML"
        )
        import asyncio
        user = await _get_user(call)
        has_access = user and user.has_access()
        uid = call.from_user.id
        try:
            results = await asyncio.gather(
                get_short_forecast("BTC", telegram_id=uid),
                get_short_forecast("ETH", telegram_id=uid),
                get_short_forecast("SOL", telegram_id=uid),
            )
            fmt = format_forecast if has_access else format_forecast_free
            parts = [fmt(r) for r in results]
            text = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(parts)
            if len(text) > 4096:
                text = text[:4090] + "…"
            rows = [[Btn(text="🔄 Обновить все", callback_data="fcast_all"),
                     Btn(text="🎯 Polymarket", url=POLY_REF)]]
            if not has_access:
                rows.insert(0, [Btn(text="🔓 Открыть полные сигналы", callback_data="subscription")])
            rows.append([Btn(text="« Меню", callback_data="main_menu")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            await call.message.edit_text(text, reply_markup=kb,
                                          parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Forecast all error: {e}")
            await call.message.edit_text("❌ Ошибка загрузки данных. Попробуй через минуту.",
                                          reply_markup=_forecast_menu_kb(), parse_mode="HTML")
        return

    coin = coin_part.upper()
    if coin not in ("BTC", "ETH", "SOL"):
        await call.answer("❌ Неизвестная монета", show_alert=True)
        return

    await call.answer(f"⏳ Анализирую {coin}...")
    await call.message.edit_text(
        f"⏳ <b>Анализирую {coin}/USDT...</b>\n<i>1м и 5м свечи</i>",
        parse_mode="HTML"
    )
    try:
        user = await _get_user(call)
        has_access = user and user.has_access()
        forecast = await get_short_forecast(coin, telegram_id=call.from_user.id)
        if has_access:
            text = format_forecast(forecast)
            kb = _forecast_kb(coin)
        else:
            text = format_forecast_free(forecast)
            kb = _forecast_kb_free(coin)
        await call.message.edit_text(text, reply_markup=kb,
                                      parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        import html
        logger.error(f"Forecast {coin} error: {type(e).__name__}: {e}")
        await call.message.edit_text(
            f"❌ <b>Ошибка {coin}</b>\n\n"
            f"<code>{html.escape(f'{type(e).__name__}: {str(e)[:300]}')}</code>",
            reply_markup=_forecast_kb(coin), parse_mode="HTML"
        )
