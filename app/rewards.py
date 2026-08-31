from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime


@dataclass
class PromoResult:
    valid: bool
    discount: Decimal = Decimal("0")
    message: str = ""


@dataclass
class ReferralReward:
    inviter_id: int
    invited_id: int
    bonus: Decimal


class RewardService:
    """Business rules for bonuses. Database handlers can use this service."""

    BONUS_PERCENT = Decimal("5")
    REFERRAL_BONUS = Decimal("100")

    @classmethod
    def purchase_bonus(cls, amount: Decimal) -> Decimal:
        return (amount * cls.BONUS_PERCENT / Decimal("100")).quantize(Decimal("0.01"))

    @classmethod
    def referral_bonus(cls) -> Decimal:
        return cls.REFERRAL_BONUS

    @staticmethod
    def normalize_promo(code: str) -> str:
        return code.strip().upper()

    @staticmethod
    def is_expired(expires_at: datetime | None) -> bool:
        return bool(expires_at and expires_at < datetime.now(expires_at.tzinfo))
