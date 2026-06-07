from typing import Callable, Awaitable, Any
import asyncio
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
    "check_sub", "poly_pro", "toggle_btc_alerts", "forecast_menu",
}
_MSG = "⏰ <b>Пробный период закончился</b>\n\nОформите подписку или введите промокод:"

REQUIRED_CHANNELS = [
    ("@n000ll", "https://t.me/n000ll"),
    ("@nemiroffcall", "https://t.me/nemiroffcall"),
]

# Cache: user_id -> (is_subscribed, timestamp)
_sub_cache: dict[int, tuple[bool, float]] = {}
_CACHE_TTL = 300  # 5 minutes


def _sub_kb() -> InlineKeyboardMarkup:
    rows = [[Btn(text=f"📢 {ch[0]}", url=ch[1])] for ch in REQUIRED_CHANNELS]
    rows.append([Btn(text="✅ Проверить подписку", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _check_channel(bot, channel: str, user_id: int) -> bool:
    try:
        member = await asyncio.wait_for(
            bot.get_chat_member(channel, user_id), timeout=3.0
        )
        return member.status not in ("left", "kicked", "banned")
    except Exception:
        return False


async def _live_check(bot, user_id: int) -> bool:
    results = await asyncio.gather(*[
        _check_channel(bot, ch, user_id) for ch, _ in REQUIRED_CHANNELS
    ])
    ok = all(results)
    _sub_cache[user_id] = (ok, time.monotonic())
    return ok


# Events that trigger a live Telegram API check
_LIVE_CHECK_CMDS = {"/start"}
_LIVE_CHECK_CBS = {"check_sub"}
_CACHE_TTL_SHORT = 10  # seconds — used after check_sub to force recheck soon


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        uid = None
        is_free = False
        needs_live_check = False

        if isinstance(event, Message):
            uid = event.from_user.id
            cmd = (event.text or "").split()[0] if event.text else ""
            is_free = cmd in _FREE_CMDS
            needs_live_check = cmd in _LIVE_CHECK_CMDS
            bot = event.bot
        elif isinstance(event, CallbackQuery):
            uid = event.from_user.id
            cb = event.data or ""
            is_free = cb in _FREE_CBS
            needs_live_check = False  # never live-check on callbacks — too slow
            if cb == "check_sub":
                _sub_cache.pop(uid, None)  # drop cache so /start will recheck
            bot = event.bot
        else:
            return await handler(event, data)

        if not uid:
            return await handler(event, data)

        # Admins bypass everything
        if uid in settings.ADMIN_IDS:
            return await handler(event, data)

        # Use cache if available and not a live-check event
        now = time.monotonic()
        cached = _sub_cache.get(uid)
        if cached and not needs_live_check:
            ok, ts = cached
            if now - ts < _CACHE_TTL:
                if not ok:
                    await _block(event, bot)
                    return
                # subscribed — continue below
            else:
                # cache expired — do live check
                ok = await _live_check(bot, uid)
                if not ok:
                    await _block(event, bot)
                    return
        else:
            # no cache or needs fresh check
            ok = await _live_check(bot, uid)
            if not ok:
                await _block(event, bot)
                return

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


async def _block(event, bot) -> None:
    sub_text = "Подпишись на наши каналы"
    kb = _sub_kb()
    if isinstance(event, Message):
        await event.answer(sub_text, reply_markup=kb, parse_mode="HTML")
    elif isinstance(event, CallbackQuery):
        if event.data == "check_sub":
            await event.answer("❌ Ты ещё не подписан на все каналы", show_alert=True)
        else:
            await event.answer("❗ Сначала подпишись на каналы", show_alert=True)
            try:
                await event.message.edit_text(sub_text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
