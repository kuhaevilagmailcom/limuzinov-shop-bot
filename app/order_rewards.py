from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.db import User
from app.rewards_storage import add_bonus
from app.rewards_models import Referral


BONUS_PERCENT = Decimal("5")
REFERRAL_BONUS = Decimal("100")


async def reward_paid_order(session, user_id: int, amount: Decimal | None, order_id: str) -> None:
    if not amount:
        return

    bonus = (Decimal(amount) * BONUS_PERCENT / Decimal("100")).quantize(Decimal("0.01"))
    if bonus > 0:
        await add_bonus(session, user_id, bonus, f"Бонус за заказ {order_id[:8]}")
        user = await session.get(User, user_id)
        if user:
            user.balance_rub += bonus

    referral = await session.scalar(
        select(Referral).where(
            Referral.invited_id == user_id,
            Referral.rewarded.is_(False),
        )
    )
    if referral:
        await add_bonus(session, referral.owner_id, REFERRAL_BONUS, "Бонус за приглашённого пользователя")
        referral.rewarded = True

    await session.commit()
