import asyncio
import logging
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.services.signal_service import generate_signal, resolve_signals
from app.services.notifier import broadcast_signal
from app.services.polymarket import get_crypto_predictions
from app.services.user_service import get_users_with_notifications
from config.settings import settings

logger = logging.getLogger(__name__)


def setup_scheduler(scheduler: AsyncIOScheduler, bot: Bot):
    scheduler.add_job(_run_signals, "interval", minutes=settings.SIGNAL_INTERVAL_MINUTES,
                      args=[bot], id="signals", replace_existing=True, misfire_grace_time=300)
    scheduler.add_job(resolve_signals, "interval", minutes=5,
                      id="resolve", replace_existing=True, misfire_grace_time=60)
    scheduler.add_job(_run_polymarket, "cron", hour=9,  minute=0, args=[bot],
                      id="poly_morning", replace_existing=True)
    scheduler.add_job(_run_polymarket, "cron", hour=18, minute=0, args=[bot],
                      id="poly_evening", replace_existing=True)
    # News: every 4 hours
    scheduler.add_job(_run_news, "cron", hour="6,10,14,18,22", minute=0, args=[bot],
                      id="news", replace_existing=True)
    # Daily crypto term: 08:00 UTC
    scheduler.add_job(_run_daily_term, "cron", hour=8, minute=0, args=[bot],
                      id="daily_term", replace_existing=True)
    # TikTok: news video 3x per day
    scheduler.add_job(_run_tiktok_news, "cron", hour="9,15,21", minute=30,
                      id="tiktok_news", replace_existing=True)
    # TikTok: term video daily at 08:15 UTC (15 min after Telegram term post)
    scheduler.add_job(_run_tiktok_term, "cron", hour=8, minute=15,
                      id="tiktok_term", replace_existing=True)
    # TikTok: signal video every signal cycle
    scheduler.add_job(_run_tiktok_signals, "interval",
                      minutes=settings.SIGNAL_INTERVAL_MINUTES,
                      start_date="2024-01-01 00:10:00",
                      id="tiktok_signals", replace_existing=True, misfire_grace_time=300)
    logger.info("Scheduler ready: signals, Polymarket, news, daily term, TikTok (news 3x + term daily + signals)")


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
    logger.info("🎯 Running Polymarket daily signals...")
    try:
        from app.services.binance import get_full_snapshot
        snaps = {}
        for coin in settings.COINS:
            try:
                snaps[coin] = await get_full_snapshot(coin)
                await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"[{coin}] Snapshot failed: {e}")

        if not snaps:
            return

        texts = await get_crypto_predictions(snaps)
        if not texts:
            logger.info("No Polymarket daily markets found today")
            return

        users = await get_users_with_notifications()
        for text in texts:
            sent = 0
            for user in users:
                if not user.has_access():
                    continue
                try:
                    await bot.send_message(user.telegram_id, text,
                                           parse_mode="HTML",
                                           disable_web_page_preview=False)
                    sent += 1
                except TelegramForbiddenError:
                    pass
                except Exception as e:
                    logger.warning(f"Poly send error {user.telegram_id}: {e}")
            logger.info(f"Polymarket signal sent to {sent} users")
            await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"Polymarket job error: {e}")


async def _run_news(bot: Bot):
    logger.info("📰 Posting news to channel...")
    try:
        from app.services.news_service import post_news_to_channel
        count = await post_news_to_channel(bot)
        logger.info(f"News job done: {count} items posted")
    except Exception as e:
        logger.error(f"News job error: {e}")


async def _run_daily_term(bot: Bot):
    logger.info("📖 Posting daily crypto term...")
    try:
        from app.services.news_service import post_term_to_channel
        await post_term_to_channel(bot)
    except Exception as e:
        logger.error(f"Daily term job error: {e}")


async def _run_tiktok_news():
    logger.info("🎬 TikTok news video job started...")
    try:
        from app.services.news_service import fetch_news
        from app.services.tiktok_service import post_news_video
        items = await fetch_news()
        if not items:
            logger.info("No news for TikTok video")
            return
        ok = await post_news_video(items)
        logger.info(f"TikTok news video: {'uploaded' if ok else 'skipped/failed'}")
    except Exception as e:
        logger.error(f"TikTok news job error: {e}")


async def _run_tiktok_signals():
    logger.info("🎬 TikTok signal video job started...")
    try:
        from app.services.signal_service import generate_signal
        from app.services.tiktok_service import post_signal_video
        for coin in settings.COINS:
            try:
                sig = await generate_signal(coin, use_cache=True)
                if sig and sig.direction != "NO TRADE":
                    ok = await post_signal_video(sig)
                    logger.info(f"[{coin}] TikTok signal video: {'uploaded' if ok else 'skipped/failed'}")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"[{coin}] TikTok signal error: {e}")
    except Exception as e:
        logger.error(f"TikTok signal job error: {e}")


async def _run_tiktok_term():
    logger.info("🎬 TikTok daily term video job started...")
    try:
        from app.services.news_service import get_daily_term
        from app.services.tiktok_service import post_term_video
        title, body = get_daily_term()
        ok = await post_term_video(title, body)
        logger.info(f"TikTok term video: {'uploaded' if ok else 'skipped/failed'}")
    except Exception as e:
        logger.error(f"TikTok term job error: {e}")
