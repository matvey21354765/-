from typing import Callable, Awaitable, Any
import time
from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import TelegramObject, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton as Btn
from app.services.user_service import get_user
from app.keyboards.inline import subscription_kb
from config.settings import settings

_FREE_CMDS = {"/start", "/help"}
_FREE_CBS = {
    "subscription", "enter_promo", "main_menu", "open_menu",
    "buy_stars_1", "buy_stars_3", "buy_stars_6",
    "buy_1", "buy_3", "buy_6",
    "check_sub",
}
_MSG = "⏰ <b>Пробный период закончился</b>\n\nОформите подписку или введите промокод:"

REQUIRED_CHANNELS = [
    ("@n000ll", "https://t.me/n000ll"),
    ("@nemiroffcall", "https://t.me/nemiroffcall"),
]

# Cache: user_id -> (is_subscribed, timestamp)
_sub_cache: dict[int, tuple[bool, float]] = {}
_CACHE_TTL = 60  # seconds


def _sub_kb() -> InlineKeyboardMarkup:
    rows = [[Btn(text=f"📢 {ch[0]}", url=ch[1])] for ch in REQUIRED_CHANNELS]
    rows.append([Btn(text="✅ Проверить подписку", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _is_subscribed(bot, user_id: int, force: bool = False) -> bool:
    now = time.monotonic()
    if not force and user_id in _sub_cache:
        cached, ts = _sub_cache[user_id]
        if now - ts < _CACHE_TTL:
            return cached

    for channel, _ in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ("left", "kicked", "banned"):
                _sub_cache[user_id] = (False, now)
                return False
        except Exception:
            # Bot not admin in channel — fail closed (block)
            _sub_cache[user_id] = (False, now)
            return False

    _sub_cache[user_id] = (True, now)
    return True


def invalidate_sub_cache(user_id: int) -> None:
    _sub_cache.pop(user_id, None)


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        uid = None
        is_free = False

        if isinstance(event, Message):
            uid = event.from_user.id
            is_free = any((event.text or "").startswith(c) for c in _FREE_CMDS)
            bot = event.bot
        elif isinstance(event, CallbackQuery):
            uid = event.from_user.id
            is_free = (event.data or "") in _FREE_CBS
            bot = event.bot
        else:
            return await handler(event, data)

        if not uid:
            return await handler(event, data)

        # Admins bypass everything
        if uid in settings.ADMIN_IDS:
            return await handler(event, data)

        # Channel subscription gate — runs for ALL events including /start
        force_check = isinstance(event, CallbackQuery) and event.data == "check_sub"
        if not await _is_subscribed(bot, uid, force=force_check):
            sub_text = (
                "📢 <b>Для использования PredictBot</b>\n"
                "подпишись на наши каналы:\n\n"
                + "\n".join(f"• {ch[0]}" for ch in REQUIRED_CHANNELS)
                + "\n\nПосле подписки нажми кнопку ниже 👇"
            )
            if isinstance(event, Message):
                await event.answer(sub_text, reply_markup=_sub_kb(), parse_mode="HTML")
            elif isinstance(event, CallbackQuery):
                if event.data == "check_sub":
                    await event.answer("❌ Ты ещё не подписан на все каналы", show_alert=True)
                else:
                    await event.answer("❗ Сначала подпишись на каналы", show_alert=True)
                    try:
                        await event.message.edit_text(sub_text, reply_markup=_sub_kb(), parse_mode="HTML")
                    except Exception:
                        pass
            return

        # check_sub succeeded — show main menu (handled in handler)
        if is_free:
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
