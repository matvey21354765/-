from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton as Btn
import logging

router = Router()
logger = logging.getLogger(__name__)


def _kb(days: int) -> InlineKeyboardMarkup:
    rows = [[
        Btn(text="📅 24ч" + (" ✓" if days == 1 else ""),   callback_data="lb_1"),
        Btn(text="📅 7д"  + (" ✓" if days == 7 else ""),   callback_data="lb_7"),
        Btn(text="📅 30д" + (" ✓" if days == 30 else ""),  callback_data="lb_30"),
    ]]
    rows.append([Btn(text="« Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "leaderboard")
async def cb_leaderboard(call: CallbackQuery):
    await _show(call, 7)


@router.callback_query(F.data.startswith("lb_"))
async def cb_lb_period(call: CallbackQuery):
    try:
        days = int(call.data.split("_")[1])
    except (IndexError, ValueError):
        days = 7
    await _show(call, days)


async def _show(call: CallbackQuery, days: int):
    await call.answer()
    try:
        from app.services.leaderboard import get_stats, format_leaderboard
        stats = await get_stats(days)
        text = format_leaderboard(stats)
    except Exception as e:
        logger.error(f"Leaderboard error: {e}")
        text = "❌ Ошибка загрузки статистики"
    await call.message.edit_text(text, reply_markup=_kb(days), parse_mode="HTML")
