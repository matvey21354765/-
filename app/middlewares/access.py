from typing import Callable, Awaitable, Any
from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import TelegramObject, Message, CallbackQuery
from app.services.user_service import get_user
from app.keyboards.inline import subscription_kb
from config.settings import settings

_FREE_CMDS = {"/start", "/help"}
_FREE_CBS = {
    "subscription", "enter_promo", "main_menu",
    "buy_stars_1", "buy_stars_3", "buy_stars_6",
    "buy_1", "buy_3", "buy_6",
}
_MSG = "⏰ <b>Пробный период закончился</b>\n\nОформите подписку или введите промокод:"


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        uid = None
        is_free = False

        if isinstance(event, Message):
            uid = event.from_user.id
            is_free = any((event.text or "").startswith(c) for c in _FREE_CMDS)
        elif isinstance(event, CallbackQuery):
            uid = event.from_user.id
            is_free = (event.data or "") in _FREE_CBS

        if not uid or is_free:
            return await handler(event, data)

        # Admins always pass through
        if uid in settings.ADMIN_IDS:
            return await handler(event, data)

        user = await get_user(uid)
        if not user:
            return await handler(event, data)

        data["db_user"] = user

        # Allow through if user is in FSM state (e.g. entering promo code)
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
