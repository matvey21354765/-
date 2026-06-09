from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func, and_

logger = logging.getLogger(__name__)


async def log_forecast(coin: str, direction: str, price: float) -> int | None:
    """Save a new forecast to forecast_log, deduplicating within 15 minutes."""
    if direction == "FLAT":
        return None
    try:
        from app.models.database import AsyncSessionLocal, ForecastLog
        dedup_window = datetime.now(timezone.utc) - timedelta(minutes=15)
        async with AsyncSessionLocal() as db:
            # Don't log if same coin+direction already logged in last 15 min
            existing = await db.execute(
                select(ForecastLog).where(
                    and_(
                        ForecastLog.coin == coin,
                        ForecastLog.direction == direction,
                        ForecastLog.created_at >= dedup_window,
                        ForecastLog.correct.is_(None),
                    )
                ).limit(1)
            )
            if existing.scalar_one_or_none():
                return None
            row = ForecastLog(coin=coin, direction=direction, price_entry=price)
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row.id
    except Exception as e:
        logger.warning(f"log_forecast error: {e}")
        return None


async def resolve_forecasts():
    """Called by scheduler every 5 min. Resolve forecasts older than 10 min."""
    try:
        from app.models.database import AsyncSessionLocal, ForecastLog
        from app.services.short_forecast import _kraken_df
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(ForecastLog).where(
                    and_(ForecastLog.correct.is_(None),
                         ForecastLog.created_at <= cutoff)
                )
            )
            rows = res.scalars().all()
            if not rows:
                return

            # Fetch current prices per coin
            coins_needed = list({r.coin for r in rows})
            prices: dict[str, float] = {}
            for coin in coins_needed:
                try:
                    df = await _kraken_df(coin, 1, 5)
                    prices[coin] = float(df["close"].iloc[-1])
                except Exception:
                    pass

            now = datetime.now(timezone.utc)
            for row in rows:
                cur = prices.get(row.coin)
                if cur is None:
                    continue
                if row.direction == "UP":
                    row.correct = cur > row.price_entry
                elif row.direction == "DOWN":
                    row.correct = cur < row.price_entry
                row.price_exit = cur
                row.resolved_at = now
            await db.commit()
    except Exception as e:
        logger.warning(f"resolve_forecasts error: {e}")


async def get_stats(days: int = 7) -> dict:
    """Return accuracy stats for last N days — combines short forecasts + regular signals."""
    try:
        from app.models.database import AsyncSessionLocal, ForecastLog, Signal
        from sqlalchemy import case
        since = datetime.now(timezone.utc) - timedelta(days=days)

        coins: dict[str, dict] = {}

        async with AsyncSessionLocal() as db:
            # ── Short forecasts (5-10 min) ───────────────────────────────────
            res = await db.execute(
                select(
                    ForecastLog.coin,
                    func.count().label("total"),
                    func.sum(case((ForecastLog.correct == True, 1), else_=0)).label("wins"),
                ).where(
                    and_(ForecastLog.correct.isnot(None),
                         ForecastLog.created_at >= since)
                ).group_by(ForecastLog.coin)
            )
            for r in res.all():
                coins.setdefault(r.coin, {"total": 0, "wins": 0, "short_total": 0, "short_wins": 0,
                                          "sig_total": 0, "sig_wins": 0})
                coins[r.coin]["total"]       += r.total
                coins[r.coin]["wins"]        += r.wins
                coins[r.coin]["short_total"] += r.total
                coins[r.coin]["short_wins"]  += r.wins

            # ── Regular signals (WIN/LOSS) ────────────────────────────────────
            res2 = await db.execute(
                select(
                    Signal.coin,
                    func.count().label("total"),
                    func.sum(case((Signal.status == "WIN", 1), else_=0)).label("wins"),
                ).where(
                    and_(Signal.status.in_(["WIN", "LOSS"]),
                         Signal.created_at >= since)
                ).group_by(Signal.coin)
            )
            for r in res2.all():
                coins.setdefault(r.coin, {"total": 0, "wins": 0, "short_total": 0, "short_wins": 0,
                                          "sig_total": 0, "sig_wins": 0})
                coins[r.coin]["total"]     += r.total
                coins[r.coin]["wins"]      += r.wins
                coins[r.coin]["sig_total"] += r.total
                coins[r.coin]["sig_wins"]  += r.wins

        for coin in coins:
            d = coins[coin]
            d["acc"] = round(d["wins"] / d["total"] * 100) if d["total"] else 0

        overall_total = sum(d["total"] for d in coins.values())
        overall_wins  = sum(d["wins"]  for d in coins.values())
        overall_acc   = round(overall_wins / overall_total * 100) if overall_total else 0

        return {
            "days": days, "total": overall_total, "wins": overall_wins,
            "acc": overall_acc, "coins": coins,
        }
    except Exception as e:
        logger.warning(f"get_stats error: {e}")
        return {"days": days, "total": 0, "wins": 0, "acc": 0, "coins": {}}


def format_leaderboard(stats: dict) -> str:
    days = stats["days"]
    total = stats["total"]
    wins  = stats["wins"]
    acc   = stats["acc"]
    coins = stats["coins"]

    bar_len = max(0, min(10, round(acc / 10)))
    bar = "█" * bar_len + "░" * (10 - bar_len)

    coin_lines = []
    order = ["BTC", "ETH", "SOL"]
    medal = ["🥇", "🥈", "🥉"]
    sorted_coins = sorted(
        [(c, d) for c, d in coins.items() if c in order],
        key=lambda x: x[1]["acc"], reverse=True
    )
    for i, (coin, d) in enumerate(sorted_coins):
        m = medal[i] if i < 3 else "  "
        parts = []
        if d.get("short_total", 0) > 0:
            sa = round(d["short_wins"] / d["short_total"] * 100) if d["short_total"] else 0
            parts.append(f"⚡{sa}% ({d['short_wins']}/{d['short_total']})")
        if d.get("sig_total", 0) > 0:
            sg = round(d["sig_wins"] / d["sig_total"] * 100) if d["sig_total"] else 0
            parts.append(f"📊{sg}% ({d['sig_wins']}/{d['sig_total']})")
        detail = "  ".join(parts) if parts else f"{d['wins']}/{d['total']}"
        coin_lines.append(f"{m} <b>{coin}</b>: <b>{d['acc']}%</b>  <i>{detail}</i>")

    coins_text = "\n".join(coin_lines) if coin_lines else "  Нет данных"

    return (
        f"🏆 <b>Лидерборд точности</b>\n"
        f"📅 За последние {days} дней\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Общая точность: <b>{acc}%</b>  <code>{bar}</code>\n"
        f"✅ Верных: <b>{wins}</b> из <b>{total}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>По монетам:</b>\n{coins_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>⚡ — прогноз 5-10м  📊 — основной сигнал</i>"
    )
