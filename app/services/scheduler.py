import logging
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.services.signal_service import generate_signal, resolve_signals
from app.services.notifier import broadcast_signal
from config.settings import settings

logger = logging.getLogger(__name__)


def setup_scheduler(scheduler: AsyncIOScheduler, bot: Bot):
    scheduler.add_job(_run_signals, "interval", minutes=settings.SIGNAL_INTERVAL_MINUTES,
                      args=[bot], id="signals", replace_existing=True, misfire_grace_time=300)
    scheduler.add_job(resolve_signals, "interval", minutes=5,
                      id="resolve", replace_existing=True, misfire_grace_time=60)
    logger.info(f"Scheduler: signals every {settings.SIGNAL_INTERVAL_MINUTES}min")


async def _run_signals(bot: Bot):
    import asyncio
    logger.info("⚙️ Generating scheduled signals...")
    for coin in settings.COINS:
        try:
            sig = await generate_signal(coin, use_cache=False)
            if sig:
                sent, _ = await broadcast_signal(bot, sig)
                logger.info(f"[{coin}] Broadcast {sig.direction} → {sent} users")
            await asyncio.sleep(3)  # avoid Groq rate limits between coins
        except Exception as e:
            logger.error(f"[{coin}] Scheduled error: {e}")
