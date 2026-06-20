import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config.settings import settings
from app.models.database import init_db, PromoCode, AsyncSessionLocal
from app.handlers.all import router
from app.handlers.polymarket_pro import router as poly_router
from app.handlers.forecast import router as forecast_router
from app.handlers.leaderboard import router as lb_router
from app.handlers.futures_guide import router as futures_router
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
    logger.info("🚀 Starting PredictBot...")

    await init_db()
    logger.info("✅ Database ready")

    # Seed promo codes — check by known first code
    from app.seeds.promo_list import PROMO_CODES
    first_code = PROMO_CODES[1][0]
    async with AsyncSessionLocal() as db:
        from sqlalchemy import delete
        res = await db.execute(select(PromoCode).where(PromoCode.code == first_code))
        exists = res.scalar_one_or_none()
    if not exists:
        # Clear wrong codes and reload correct ones
        async with AsyncSessionLocal() as db:
            await db.execute(delete(PromoCode).where(PromoCode.is_used == False))
            await db.commit()
        total = await save_promo_codes()
        logger.info(f"✅ Seeded {total} promo codes")

    # One-time: revoke @aeon_8 subscription and reset promo DAO1M-WNNHJ7XV
    try:
        from app.models.database import User
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(User).where(User.username == "aeon_8"))
            bad_user = res.scalar_one_or_none()
            if bad_user and bad_user.is_subscribed:
                bad_user.is_subscribed = False
                bad_user.subscription_ends_at = None
                await db.commit()
                logger.info(f"🚫 Revoked subscription for @aeon_8 (id={bad_user.telegram_id})")
            res2 = await db.execute(select(PromoCode).where(PromoCode.code == "DAO1M-WNNHJ7XV"))
            promo = res2.scalar_one_or_none()
            if promo and promo.is_used:
                promo.is_used = False
                promo.used_by = None
                promo.used_at = None
                await db.commit()
                logger.info("🚫 Reset promo code DAO1M-WNNHJ7XV")
    except Exception as e:
        logger.warning(f"One-time revoke failed: {e}")

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())
    dp.include_router(router)
    dp.include_router(poly_router)
    dp.include_router(forecast_router)
    dp.include_router(lb_router)
    dp.include_router(futures_router)

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
