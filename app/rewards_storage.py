from decimal import Decimal

from sqlalchemy import select

from app.rewards_models import BonusTransaction, PromoCode, Referral


async def get_promo(session, code: str):
    return await session.scalar(
        select(PromoCode).where(PromoCode.code == code.upper(), PromoCode.active.is_(True))
    )


async def add_bonus(session, user_id: int, amount: Decimal, reason: str):
    tx = BonusTransaction(user_id=user_id, amount=amount, reason=reason)
    session.add(tx)
    await session.commit()
    return tx


async def attach_referral(session, owner_id: int, invited_id: int):
    if owner_id == invited_id:
        return None
    existing = await session.scalar(
        select(Referral).where(Referral.invited_id == invited_id)
    )
    if existing:
        return existing
    item = Referral(owner_id=owner_id, invited_id=invited_id)
    session.add(item)
    await session.commit()
    return item
