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
    scheduler.add_job(_run_news, "cron", hour="6,10,14,18,22", minute=0, args=[bot],
                      id="news", replace_existing=True)
    scheduler.add_job(_run_daily_term, "cron", hour=8, minute=0, args=[bot],
                      id="daily_term", replace_existing=True)
    scheduler.add_job(_run_btc_alerts, "interval", minutes=5, args=[bot],
                      id="btc_alerts", replace_existing=True, misfire_grace_time=60)
    scheduler.add_job(_run_resolve_forecasts, "interval", minutes=5,
                      id="resolve_forecasts", replace_existing=True, misfire_grace_time=60)
    scheduler.add_job(_run_auto_forecasts, "interval", minutes=5,
                      id="auto_forecasts", replace_existing=True, misfire_grace_time=60)
    logger.info(f"Scheduler ready: signals/{settings.SIGNAL_INTERVAL_MINUTES}min, BTC alerts 5min, Polymarket, News, Terms")


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


async def _run_news(bot: Bot):
    logger.info("📰 Posting crypto news...")
    try:
        from app.services.news_service import post_news_to_channel
        await post_news_to_channel(bot)
    except Exception as e:
        logger.error(f"News job error: {e}")


async def _run_daily_term(bot: Bot):
    logger.info("📚 Posting daily term...")
    try:
        from app.services.news_service import post_term_to_channel
        await post_term_to_channel(bot)
    except Exception as e:
        logger.error(f"Term job error: {e}")


async def _run_btc_alerts(bot: Bot):
    try:
        from app.services.btc_alerts import fetch_btc_data, check_alerts
        from app.services.user_service import get_users_with_btc_alerts
        from app.services.notifier import POLYMARKET_URL
        df5, df15 = await fetch_btc_data()
        alerts = check_alerts(df5, df15)
        if not alerts:
            return
        users = await get_users_with_btc_alerts()
        if not users:
            return
        for alert in alerts:
            text = (
                f"⚡ <b>{alert['title']}</b>\n\n"
                f"{alert['text']}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔗 <a href=\"{POLYMARKET_URL}\">Ставка на Polymarket</a>"
            )
            for user in users:
                try:
                    await bot.send_message(user.telegram_id, text,
                                           parse_mode="HTML", disable_web_page_preview=True)
                except TelegramForbiddenError:
                    pass
                except Exception as e:
                    logger.warning(f"Alert send error {user.telegram_id}: {e}")
            await asyncio.sleep(0.5)
        logger.info(f"BTC alerts sent: {[a['type'] for a in alerts]}")
    except Exception as e:
        logger.error(f"BTC alerts job error: {e}")


async def _run_resolve_forecasts():
    try:
        from app.services.leaderboard import resolve_forecasts
        await resolve_forecasts()
    except Exception as e:
        logger.error(f"resolve_forecasts job error: {e}")


async def _run_auto_forecasts():
    """Generate short forecasts on a fixed schedule so accuracy stats fill
    up even without users opening the forecast screen themselves."""
    try:
        from app.services.short_forecast import get_short_forecast
        for coin in ("BTC", "ETH", "SOL"):
            try:
                await get_short_forecast(coin)
            except Exception as e:
                logger.warning(f"auto_forecast {coin} error: {e}")
    except Exception as e:
        logger.error(f"auto_forecasts job error: {e}")


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
