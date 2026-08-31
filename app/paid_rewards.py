from decimal import Decimal

from app.db import SessionLocal, User
from app.rewards_storage import add_bonus


BONUS_PERCENT = Decimal("5")
REFERRAL_BONUS = Decimal("100")


async def reward_paid_order(order) -> None:
    """Called only after mark_order_paid returns changed=True."""
    if not order or not order.amount_rub:
        return

    bonus = (Decimal(str(order.amount_rub)) * BONUS_PERCENT / Decimal("100")).quantize(Decimal("0.01"))
    if bonus <= 0:
        return

    async with SessionLocal() as session:
        await add_bonus(
            session,
            order.user_id,
            bonus,
            f"Бонус за заказ {order.id[:8]}",
        )
        await session.commit()
