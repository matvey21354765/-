from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy import select
from app.models.database import User, AsyncSessionLocal
from config.settings import settings


async def get_or_create_user(telegram_id: int, username: str = None, first_name: str = None) -> tuple[User, bool]:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = res.scalar_one_or_none()
        if user:
            if username and user.username != username:
                user.username = username
                await db.commit()
            return user, False
        now = datetime.now(timezone.utc)
        user = User(
            telegram_id=telegram_id, username=username, first_name=first_name,
            trial_started_at=now,
            trial_ends_at=now + timedelta(days=settings.TRIAL_DAYS),
            virtual_deposit=settings.VIRTUAL_DEPOSIT,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user, True


async def get_user(telegram_id: int) -> Optional[User]:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.telegram_id == telegram_id))
        return res.scalar_one_or_none()


async def activate_subscription(telegram_id: int, months: int) -> bool:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = res.scalar_one_or_none()
        if not user:
            return False
        now = datetime.now(timezone.utc)
        base = user.subscription_ends_at
        if base and base.replace(tzinfo=timezone.utc) > now:
            base = base.replace(tzinfo=timezone.utc)
        else:
            base = now
        user.subscription_ends_at = base + timedelta(days=30 * months)
        user.is_subscribed = True
        await db.commit()
        return True


async def get_all_active_users() -> list[User]:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.is_active == True))
        return res.scalars().all()


async def get_user_count() -> dict:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User))
        all_users = res.scalars().all()
        return {
            "total": len(all_users),
            "trial": sum(1 for u in all_users if u.trial_active()),
            "subscribed": sum(1 for u in all_users if u.is_subscribed),
            "expired": sum(1 for u in all_users if not u.has_access()),
        }
