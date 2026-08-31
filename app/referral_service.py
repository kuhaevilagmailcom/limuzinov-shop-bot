from __future__ import annotations

REFERRAL_BONUS = 100


def make_referral_code(user_id: int) -> str:
    return f"LMZ{user_id}"


async def reward_referral(session, inviter_id: int | None):
    if not inviter_id:
        return 0
    return REFERRAL_BONUS
