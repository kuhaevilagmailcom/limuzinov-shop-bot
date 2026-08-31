from __future__ import annotations

from decimal import Decimal
from secrets import token_urlsafe


BONUS_PER_PURCHASE_PERCENT = Decimal("5")


def generate_referral_code(user_id: int) -> str:
    return f"LMZ{user_id}{token_urlsafe(3).upper()}"


def calculate_purchase_bonus(amount: Decimal) -> Decimal:
    return (amount * BONUS_PER_PURCHASE_PERCENT / Decimal("100")).quantize(Decimal("0.01"))


def build_referral_link(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start={code}"
