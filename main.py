import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config.settings import settings
from app.models.database import init_db, PromoCode, AsyncSessionLocal
from app.handlers.all import router
from app.middlewares.access import AccessMiddleware
from app.services.scheduler import setup_scheduler
from app.services.user_service import save_promo_codes
from sqlalchemy import select, func

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

    # Generate promo codes on first launch
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(func.count()).select_from(PromoCode))
        count = res.scalar()
    if count == 0:
        total = await save_promo_codes()
        logger.info(f"✅ Seeded {total} promo codes (100x1m + 100x3m + 100x6m)")

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
