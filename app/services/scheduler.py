import asyncio
import logging
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.services.signal_service import generate_signal, resolve_signals
from app.services.notifier import broadcast_signal
from app.services.polymarket import get_high_confidence_markets, format_polymarket_alert
from app.services.user_service import get_users_with_notifications
from config.settings import settings

logger = logging.getLogger(__name__)


def setup_scheduler(scheduler: AsyncIOScheduler, bot: Bot):
    scheduler.add_job(_run_signals, "interval", minutes=settings.SIGNAL_INTERVAL_MINUTES,
                      args=[bot], id="signals", replace_existing=True, misfire_grace_time=300)
    scheduler.add_job(resolve_signals, "interval", minutes=5,
                      id="resolve", replace_existing=True, misfire_grace_time=60)
    # Polymarket high-confidence alerts at 09:00 and 18:00 UTC
    scheduler.add_job(_run_polymarket, "cron", hour=9, minute=0,
                      args=[bot], id="poly_morning", replace_existing=True)
    scheduler.add_job(_run_polymarket, "cron", hour=18, minute=0,
                      args=[bot], id="poly_evening", replace_existing=True)
    logger.info(f"Scheduler: signals every {settings.SIGNAL_INTERVAL_MINUTES}min, Polymarket at 09:00 & 18:00 UTC")


async def _run_signals(bot: Bot):
    logger.info("⚙️ Generating scheduled signals...")
    for coin in settings.COINS:
        try:
            sig = await generate_signal(coin, use_cache=False)
            if sig:
                sent, _ = await broadcast_signal(bot, sig)
                logger.info(f"[{coin}] Broadcast {sig.direction} → {sent} users")
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"[{coin}] Scheduled error: {e}")


async def _run_polymarket(bot: Bot):
    logger.info("🎯 Checking Polymarket high-confidence markets...")
    try:
        markets = await get_high_confidence_markets(min_conf=85.0)
        if not markets:
            logger.info("No high-confidence Polymarket markets found")
            return
        text = format_polymarket_alert(markets)
        if not text:
            return
        users = await get_users_with_notifications()
        sent = 0
        for user in users:
            if not user.has_access():
                continue
            try:
                await bot.send_message(user.telegram_id, text,
                                       parse_mode="HTML", disable_web_page_preview=False)
                sent += 1
            except TelegramForbiddenError:
                pass
            except Exception as e:
                logger.warning(f"Polymarket send error {user.telegram_id}: {e}")
        logger.info(f"Polymarket alert sent to {sent} users")
    except Exception as e:
        logger.error(f"Polymarket job error: {e}")
