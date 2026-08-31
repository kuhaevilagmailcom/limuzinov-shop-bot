from __future__ import annotations

from decimal import Decimal

BONUS_PERCENT = Decimal("5")
REFERRAL_BONUS = Decimal("100")


def purchase_bonus(amount: Decimal | None) -> Decimal:
    if not amount:
        return Decimal("0")
    return (Decimal(amount) * BONUS_PERCENT / Decimal("100")).quantize(Decimal("0.01"))


async def reward_paid_order(session, user, order):
    """Called only after mark_order_paid returned changed=True."""
    bonus = purchase_bonus(order.amount_rub)
    if bonus <= 0:
        return Decimal("0")
    user.balance_rub += bonus
    await session.commit()
    return bonus


async def reward_referral(session, inviter):
    if inviter is None:
        return Decimal("0")
    inviter.balance_rub += REFERRAL_BONUS
    await session.commit()
    return REFERRAL_BONUS
