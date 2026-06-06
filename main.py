import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config.settings import settings
from app.models.database import init_db
from app.handlers.all import router
from app.middlewares.access import AccessMiddleware
from app.services.scheduler import setup_scheduler

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main():
    logger.info("🚀 Starting DAO Signals Bot (Groq)...")

    await init_db()
    logger.info("✅ Database ready")

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="UTC")
    setup_scheduler(scheduler, bot)
    scheduler.start()
    logger.info("✅ Scheduler started")

    logger.info("✅ Bot is running!")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
