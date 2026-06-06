from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, func, Index, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from config.settings import settings

engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass



class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64))
    first_name: Mapped[Optional[str]] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_subscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    trial_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    subscription_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    virtual_deposit: Mapped[float] = mapped_column(Float, default=10000.0)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    def has_access(self) -> bool:
        now = datetime.now(timezone.utc)
        if self.is_subscribed and self.subscription_ends_at:
            if self.subscription_ends_at.replace(tzinfo=timezone.utc) > now:
                return True
        if self.trial_ends_at:
            if self.trial_ends_at.replace(tzinfo=timezone.utc) > now:
                return True
        return False

    def trial_days_left(self) -> int:
        if not self.trial_ends_at:
            return 0
        delta = self.trial_ends_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
        return max(0, delta.days)

    def trial_active(self) -> bool:
        return not self.is_subscribed and self.trial_days_left() > 0


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_coin_created", "coin", "created_at"),
        Index("ix_signals_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin: Mapped[str] = mapped_column(String(10), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    signal_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    trend_strength: Mapped[Optional[str]] = mapped_column(String(20))
    prob_up: Mapped[Optional[float]] = mapped_column(Float)
    prob_down: Mapped[Optional[float]] = mapped_column(Float)
    timeframe: Mapped[Optional[str]] = mapped_column(String(32))
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(10), default="MARKET")
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_1: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_2: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_3: Mapped[float] = mapped_column(Float, nullable=False)
    risk_reward: Mapped[float] = mapped_column(Float, nullable=False)
    sl_distance_pct: Mapped[Optional[float]] = mapped_column(Float)
    reasons: Mapped[str] = mapped_column(Text, nullable=False)
    bull_scenario: Mapped[Optional[str]] = mapped_column(Text)
    bear_scenario: Mapped[Optional[str]] = mapped_column(Text)
    key_trigger: Mapped[Optional[str]] = mapped_column(Text)
    full_analysis: Mapped[str] = mapped_column(Text, nullable=False)
    price_at_signal: Mapped[float] = mapped_column(Float, nullable=False)
    rsi_1h: Mapped[Optional[float]] = mapped_column(Float)
    rsi_4h: Mapped[Optional[float]] = mapped_column(Float)
    macd_1h: Mapped[Optional[float]] = mapped_column(Float)
    ema50_1h: Mapped[Optional[float]] = mapped_column(Float)
    ema200_1h: Mapped[Optional[float]] = mapped_column(Float)
    atr_1h: Mapped[Optional[float]] = mapped_column(Float)
    volume_24h: Mapped[Optional[float]] = mapped_column(Float)
    funding_rate: Mapped[Optional[float]] = mapped_column(Float)
    open_interest: Mapped[Optional[float]] = mapped_column(Float)
    fear_greed: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    outcome_price: Mapped[Optional[float]] = mapped_column(Float)
    outcome_tp_hit: Mapped[Optional[int]] = mapped_column(Integer)
    outcome_pnl_pct: Mapped[Optional[float]] = mapped_column(Float)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DepositSnapshot(Base):
    __tablename__ = "deposit_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[Optional[int]] = mapped_column(Integer)
    balance: Mapped[float] = mapped_column(Float, nullable=False)
    pnl_amount: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StrategyStats(Base):
    __tablename__ = "strategy_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period: Mapped[str] = mapped_column(String(8), nullable=False, unique=True)
    total_signals: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    avg_rr: Mapped[float] = mapped_column(Float, default=0.0)
    total_pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    avg_win_pct: Mapped[float] = mapped_column(Float, default=0.0)
    avg_loss_pct: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, default=0.0)
    expectancy: Mapped[float] = mapped_column(Float, default=0.0)
    best_coin: Mapped[Optional[str]] = mapped_column(String(10))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    months: Mapped[int] = mapped_column(Integer, nullable=False)  # 1, 3, or 6
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)
    used_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS notifications_enabled BOOLEAN DEFAULT FALSE"
        ))


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
