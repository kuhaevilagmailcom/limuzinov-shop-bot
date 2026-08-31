from __future__ import annotations

from decimal import Decimal

BONUS_PERCENT = Decimal("5")
REFERRAL_BONUS = Decimal("100")


async def calculate_order_bonus(order) -> Decimal:
    if not order.amount_rub:
        return Decimal("0")
    return (Decimal(order.amount_rub) * BONUS_PERCENT / Decimal("100")).quantize(Decimal("0.01"))


async def reward_paid_order_once(session, order) -> Decimal:
    """Reward hook. Should be called only after mark_order_paid returns changed=True."""
    from app.db import User

    user = await session.get(User, order.user_id)
    if user is None:
        return Decimal("0")

    bonus = await calculate_order_bonus(order)
    if bonus:
        user.balance_rub += bonus
        await session.commit()
    return bonus
