from typing import Callable, Awaitable, Any
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from app.services.user_service import get_user
from app.keyboards.inline import subscription_kb

_FREE_CMDS = {"/start", "/help"}
_FREE_CBS = {"subscription", "buy_1", "buy_3", "buy_6", "main_menu"}
_MSG = "⏰ <b>Пробный период закончился</b>\n\nОформите подписку:"


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

        user = await get_user(uid)
        if not user:
            return await handler(event, data)

        data["db_user"] = user

        if not user.has_access():
            if isinstance(event, Message):
                await event.answer(_MSG, reply_markup=subscription_kb(), parse_mode="HTML")
            elif isinstance(event, CallbackQuery):
                await event.message.edit_text(_MSG, reply_markup=subscription_kb(), parse_mode="HTML")
                await event.answer()
            return

        return await handler(event, data)
