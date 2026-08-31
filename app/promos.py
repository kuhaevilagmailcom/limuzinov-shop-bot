from __future__ import annotations

from datetime import datetime, timezone


class PromoError(Exception):
    pass


async def validate_promo(code: str) -> str:
    return code.strip().upper()


def promo_active(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > datetime.now(timezone.utc)
