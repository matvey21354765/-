from typing import Callable, Awaitable, Any
from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import TelegramObject, Message, CallbackQuery
from app.services.user_service import get_user
from app.keyboards.inline import subscription_kb
from config.settings import settings

_FREE_CMDS = {"/start", "/help"}
_FREE_CBS = {
    "subscription", "enter_promo", "main_menu", "open_menu",
    "buy_stars_1", "buy_stars_3", "buy_stars_6",
    "buy_1", "buy_3", "buy_6",
    "check_sub", "poly_pro", "toggle_btc_alerts", "forecast_menu",
}
_MSG = "🔒 <b>Нет активной подписки</b>\n\nОформите подписку или введите промокод:"


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        uid = None
        is_free = False

        if isinstance(event, Message):
            uid = event.from_user.id
            cmd = (event.text or "").split()[0] if event.text else ""
            is_free = cmd in _FREE_CMDS
        elif isinstance(event, CallbackQuery):
            uid = event.from_user.id
            cb = event.data or ""
            is_free = cb in _FREE_CBS
        else:
            return await handler(event, data)

        if not uid:
            return await handler(event, data)

        # Admins bypass everything
        if uid in settings.ADMIN_IDS:
            return await handler(event, data)

        if is_free:
            return await handler(event, data)

        user = await get_user(uid)
        if not user:
            return await handler(event, data)

        data["db_user"] = user

        fsm: FSMContext = data.get("state")
        if fsm:
            state_name = await fsm.get_state()
            if state_name is not None:
                return await handler(event, data)

        if not user.has_access():
            if isinstance(event, Message):
                await event.answer(_MSG, reply_markup=subscription_kb(), parse_mode="HTML")
            elif isinstance(event, CallbackQuery):
                await event.message.edit_text(_MSG, reply_markup=subscription_kb(), parse_mode="HTML")
                await event.answer()
            return

        return await handler(event, data)
