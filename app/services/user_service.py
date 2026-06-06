from __future__ import annotations
import random
import string
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy import select
from app.models.database import User, PromoCode, AsyncSessionLocal
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


async def toggle_notifications(telegram_id: int) -> bool:
    """Toggle notifications, return new state."""
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.telegram_id == telegram_id))
        user = res.scalar_one_or_none()
        if not user:
            return False
        user.notifications_enabled = not user.notifications_enabled
        await db.commit()
        return user.notifications_enabled


async def get_users_with_notifications() -> list[User]:
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(User).where(User.notifications_enabled == True, User.is_active == True))
        return res.scalars().all()


async def get_all_active_users() -> list[User]:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.is_active == True))
        return res.scalars().all()


async def activate_promo_code(telegram_id: int, code: str) -> tuple[bool, str]:
    """Try to activate promo code. Returns (success, message)."""
    code = code.strip().upper()
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(PromoCode).where(PromoCode.code == code))
        promo = res.scalar_one_or_none()
        if not promo:
            return False, "❌ Промокод не найден"
        if promo.is_used:
            return False, "❌ Промокод уже использован"
        promo.is_used = True
        promo.used_by = telegram_id
        promo.used_at = datetime.now(timezone.utc)
        await db.commit()
    ok = await activate_subscription(telegram_id, promo.months)
    if ok:
        labels = {1: "1 месяц", 3: "3 месяца", 6: "6 месяцев"}
        return True, f"✅ Промокод активирован! Подписка на {labels.get(promo.months, f'{promo.months} мес.')}"
    return False, "❌ Ошибка активации"


def generate_promo_codes() -> dict[str, list[str]]:
    """Generate 300 unique promo codes: 100x1m, 100x3m, 100x6m."""
    def make_code(prefix: str) -> str:
        chars = string.ascii_uppercase + string.digits
        return prefix + "-" + "".join(random.choices(chars, k=8))

    codes = {"1M": [], "3M": [], "6M": []}
    used = set()
    for prefix, key in [("DAO1M", "1M"), ("DAO3M", "3M"), ("DAO6M", "6M")]:
        while len(codes[key]) < 100:
            c = make_code(prefix)
            if c not in used:
                used.add(c)
                codes[key].append(c)
    return codes


async def save_promo_codes(codes: dict[str, list[str]]) -> int:
    months_map = {"1M": 1, "3M": 3, "6M": 6}
    count = 0
    async with AsyncSessionLocal() as db:
        for key, code_list in codes.items():
            for code in code_list:
                db.add(PromoCode(code=code, months=months_map[key]))
                count += 1
        await db.commit()
    return count


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
