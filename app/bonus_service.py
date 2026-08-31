from __future__ import annotations

from decimal import Decimal

BONUS_PERCENT = Decimal("5")
REFERRAL_BONUS = Decimal("100")


def purchase_bonus(amount: Decimal | None) -> Decimal:
    if not amount:
        return Decimal("0")
    return (Decimal(amount) * BONUS_PERCENT / Decimal("100")).quantize(Decimal("0.01"))


async def reward_paid_order(session, order):
    """Single entry point called after changed=True from mark_order_paid."""
    bonus = purchase_bonus(order.amount_rub)
    if bonus <= 0:
        return Decimal("0")
    user = await session.get(type(order).__mro__[0], order.user_id)
    return bonus
