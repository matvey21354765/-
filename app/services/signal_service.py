from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy import select, and_, desc
from app.models.database import Signal, DepositSnapshot, StrategyStats, AsyncSessionLocal
from app.services.binance import get_full_snapshot, fetch_ticker, SYMBOL_MAP
from app.services.analyzer import analyze_coin
from config.settings import settings

logger = logging.getLogger(__name__)

# In-memory cache: coin -> (signal, timestamp)
_cache: dict[str, tuple[Signal, float]] = {}
CACHE_TTL = 300  # 5 minutes


async def generate_signal(coin: str, use_cache: bool = True) -> Optional[Signal]:
    if use_cache and coin in _cache:
        sig, ts = _cache[coin]
        if time.time() - ts < CACHE_TTL:
            logger.info(f"[{coin}] Cache hit")
            return sig
    return await _generate_signal_fresh(coin)


async def _generate_signal_fresh(coin: str) -> Optional[Signal]:
    logger.info(f"[{coin}] Fetching snapshot...")
    try:
        snap = await get_full_snapshot(coin)
    except Exception as e:
        logger.error(f"[{coin}] Snapshot failed: {e}")
        return None

    logger.info(f"[{coin}] Running analysis...")
    result = analyze_coin(snap)
    if not result:
        return None

    i1 = snap["i1h"]
    async with AsyncSessionLocal() as db:
        sig = Signal(
            coin=coin,
            direction=result["direction"],
            confidence=result["confidence"],
            signal_rating=result["signal_rating"],
            trend_strength=result.get("trend_strength"),
            prob_up=result.get("prob_up"),
            prob_down=result.get("prob_down"),
            timeframe=result.get("timeframe"),
            entry_price=result["entry_price"],
            entry_type=result.get("entry_type", "MARKET"),
            stop_loss=result["stop_loss"],
            take_profit_1=result["take_profit_1"],
            take_profit_2=result["take_profit_2"],
            take_profit_3=result["take_profit_3"],
            risk_reward=result["risk_reward"],
            sl_distance_pct=result.get("sl_distance_pct"),
            reasons="\n".join(result["reasons"]),
            bull_scenario=result.get("bull_scenario"),
            bear_scenario=result.get("bear_scenario"),
            key_trigger=result.get("key_trigger"),
            full_analysis=result["full_analysis"],
            leverage_conservative=result.get("leverage_conservative"),
            leverage_aggressive=result.get("leverage_aggressive"),
            liq_price_conservative=result.get("liq_price_conservative"),
            liq_price_aggressive=result.get("liq_price_aggressive"),
            price_at_signal=snap["price"],
            rsi_1h=i1["rsi"],
            rsi_4h=snap["i4h"]["rsi"],
            macd_1h=i1["macd"]["macd"],
            ema50_1h=i1["ema_50"],
            ema200_1h=i1["ema_200"],
            atr_1h=i1["atr"],
            volume_24h=snap["volume_24h"],
            funding_rate=snap["funding_rate"],
            open_interest=snap["open_interest"],
            fear_greed=snap["fear_greed"],
            status="ACTIVE" if result["direction"] != "NO TRADE" else "EXPIRED",
        )
        db.add(sig)
        await db.commit()
        await db.refresh(sig)
        logger.info(f"[{coin}] Saved #{sig.id} {sig.direction} conf={sig.confidence:.0f}% rating={sig.signal_rating}")
        _cache[coin] = (sig, time.time())
        return sig


async def resolve_signals():
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Signal).where(Signal.status == "ACTIVE"))
        active = res.scalars().all()

    for sig in active:
        try:
            ticker = await fetch_ticker(SYMBOL_MAP.get(sig.coin, sig.coin + "USDT"))
            price = float(ticker["lastPrice"])
        except Exception:
            continue

        now = datetime.now(timezone.utc)
        age = now - sig.created_at.replace(tzinfo=timezone.utc)
        if age > timedelta(days=settings.SIGNAL_EXPIRE_DAYS):
            await _write_outcome(sig, "EXPIRED", price, None, None)
            continue

        hit_sl, hit_tp, tp_level, pnl = _check_levels(sig, price)
        if hit_sl or hit_tp:
            status = "WIN" if hit_tp else "LOSS"
            await _write_outcome(sig, status, price, tp_level, pnl)
            async with AsyncSessionLocal() as db:
                res = await db.execute(
                    select(DepositSnapshot).order_by(desc(DepositSnapshot.recorded_at)).limit(1))
                last = res.scalar_one_or_none()
                prev = last.balance if last else settings.VIRTUAL_DEPOSIT
                risk = prev * (settings.RISK_PER_TRADE_PCT / 100)
                sl_pct = sig.sl_distance_pct or 2.0
                pnl_amount = risk * (pnl / sl_pct) if pnl else 0
                db.add(DepositSnapshot(
                    signal_id=sig.id,
                    balance=round(prev + pnl_amount, 2),
                    pnl_amount=round(pnl_amount, 2),
                    pnl_pct=round(pnl, 3) if pnl else 0,
                ))
                await db.commit()

    await recompute_stats()


def _check_levels(sig: Signal, price: float):
    if sig.direction == "LONG":
        if price <= sig.stop_loss:
            return True, False, None, round((sig.stop_loss / sig.entry_price - 1) * 100, 3)
        for tp_num, tp in [(3, sig.take_profit_3), (2, sig.take_profit_2), (1, sig.take_profit_1)]:
            if price >= tp:
                return False, True, tp_num, round((tp / sig.entry_price - 1) * 100, 3)
    elif sig.direction == "SHORT":
        if price >= sig.stop_loss:
            return True, False, None, round(-abs((1 - sig.stop_loss / sig.entry_price) * 100), 3)
        for tp_num, tp in [(3, sig.take_profit_3), (2, sig.take_profit_2), (1, sig.take_profit_1)]:
            if price <= tp:
                return False, True, tp_num, round((1 - tp / sig.entry_price) * 100, 3)
    return False, False, None, 0.0


async def _write_outcome(sig, status, price, tp_hit, pnl):
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Signal).where(Signal.id == sig.id))
        s = res.scalar_one_or_none()
        if s and s.status == "ACTIVE":
            s.status = status
            s.outcome_price = price
            s.outcome_tp_hit = tp_hit
            s.outcome_pnl_pct = pnl
            s.resolved_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info(f"Signal #{s.id} {s.coin} → {status} PnL={pnl}")


async def recompute_stats():
    periods = {"7d": timedelta(days=7), "30d": timedelta(days=30),
               "90d": timedelta(days=90), "all": timedelta(days=36500)}
    async with AsyncSessionLocal() as db:
        for period, delta in periods.items():
            cutoff = datetime.now(timezone.utc) - delta
            res = await db.execute(
                select(Signal).where(and_(Signal.status.in_(["WIN", "LOSS"]), Signal.created_at >= cutoff)))
            signals = res.scalars().all()
            if not signals:
                continue
            wins = [s for s in signals if s.status == "WIN"]
            losses = [s for s in signals if s.status == "LOSS"]
            total = len(signals)
            win_rate = len(wins) / total * 100
            pnl_vals = [s.outcome_pnl_pct for s in signals if s.outcome_pnl_pct]
            avg_win = sum(s.outcome_pnl_pct for s in wins if s.outcome_pnl_pct) / max(len(wins), 1)
            avg_loss = abs(sum(s.outcome_pnl_pct for s in losses if s.outcome_pnl_pct)) / max(len(losses), 1)
            pf = (avg_win * len(wins)) / (avg_loss * len(losses)) if losses and avg_loss > 0 else float(len(wins))
            exp = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * avg_loss)
            max_dd = 0.0
            peak = cumulative = 0.0
            for p in pnl_vals:
                cumulative += p
                peak = max(peak, cumulative)
                max_dd = max(max_dd, peak - cumulative)
            coin_pnl = {}
            for s in signals:
                if s.outcome_pnl_pct:
                    coin_pnl[s.coin] = coin_pnl.get(s.coin, 0.0) + s.outcome_pnl_pct
            existing = await db.execute(select(StrategyStats).where(StrategyStats.period == period))
            stats = existing.scalar_one_or_none()
            if not stats:
                stats = StrategyStats(period=period)
                db.add(stats)
            stats.total_signals = total
            stats.wins = len(wins)
            stats.losses = len(losses)
            stats.win_rate = round(win_rate, 1)
            stats.avg_rr = round(sum(s.risk_reward for s in signals) / total, 2)
            stats.total_pnl_pct = round(sum(pnl_vals), 2)
            stats.avg_win_pct = round(avg_win, 2)
            stats.avg_loss_pct = round(avg_loss, 2)
            stats.max_drawdown = round(max_dd, 2)
            stats.profit_factor = round(pf, 2)
            stats.expectancy = round(exp, 2)
            stats.best_coin = max(coin_pnl, key=coin_pnl.get) if coin_pnl else None
            stats.updated_at = datetime.now(timezone.utc)
        await db.commit()


async def get_stats(period: str = "30d") -> Optional[StrategyStats]:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(StrategyStats).where(StrategyStats.period == period))
        return res.scalar_one_or_none()


async def get_recent_signals(coin: Optional[str] = None, limit: int = 15) -> list[Signal]:
    async with AsyncSessionLocal() as db:
        q = select(Signal).order_by(desc(Signal.created_at)).limit(limit)
        if coin:
            q = q.where(Signal.coin == coin)
        res = await db.execute(q)
        return res.scalars().all()


async def get_deposit_history(limit: int = 60) -> list[DepositSnapshot]:
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(DepositSnapshot).order_by(desc(DepositSnapshot.recorded_at)).limit(limit))
        return list(reversed(res.scalars().all()))


async def get_current_balance() -> float:
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(DepositSnapshot).order_by(desc(DepositSnapshot.recorded_at)).limit(1))
        last = res.scalar_one_or_none()
        return last.balance if last else settings.VIRTUAL_DEPOSIT


async def get_signal_by_id(sig_id: int) -> Optional[Signal]:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Signal).where(Signal.id == sig_id))
        return res.scalar_one_or_none()


async def get_latest_signals_all_coins() -> list[Optional[Signal]]:
    """Последний сигнал по каждой из монет BTC/ETH/SOL."""
    result = []
    async with AsyncSessionLocal() as db:
        for coin in settings.COINS:
            res = await db.execute(
                select(Signal)
                .where(Signal.coin == coin)
                .order_by(desc(Signal.created_at))
                .limit(1)
            )
            result.append(res.scalar_one_or_none())
    return result
