from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton as Btn
from app.services.short_forecast import get_short_forecast, format_forecast
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


@router.callback_query(F.data == "forecast_menu")
async def cb_forecast_menu(call: CallbackQuery):
    await call.message.edit_text(
        "⚡ <b>Прогноз 5–10 минут</b>\n\n"
        "Выбери монету — получи прогноз куда пойдёт цена\n"
        "и что ставить на Polymarket прямо сейчас 👇",
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
        from app.services.short_forecast import get_short_forecast, format_forecast
        try:
            results = await asyncio.gather(
                get_short_forecast("BTC"),
                get_short_forecast("ETH"),
                get_short_forecast("SOL"),
            )
            parts = [format_forecast(r) for r in results]
            text = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(parts)
            if len(text) > 4096:
                text = text[:4090] + "…"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [Btn(text="🔄 Обновить все", callback_data="fcast_all"),
                 Btn(text="🎯 Polymarket", url=POLY_REF)],
                [Btn(text="« Меню", callback_data="main_menu")],
            ])
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
        forecast = await get_short_forecast(coin)
        text = format_forecast(forecast)
        await call.message.edit_text(text, reply_markup=_forecast_kb(coin),
                                      parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Forecast {coin} error: {type(e).__name__}: {e}")
        await call.message.edit_text(
            f"❌ <b>Ошибка {coin}</b>\n\n"
            f"<code>{type(e).__name__}: {str(e)[:300]}</code>",
            reply_markup=_forecast_kb(coin), parse_mode="HTML"
        )
