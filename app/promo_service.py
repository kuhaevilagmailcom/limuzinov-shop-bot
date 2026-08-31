from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PromoCode:
    code: str
    bonus: int
    active: bool = True
    uses: int = 0


PROMOS: dict[str, PromoCode] = {}


def create_promo(code: str, bonus: int) -> PromoCode:
    promo = PromoCode(code=code.upper(), bonus=bonus)
    PROMOS[promo.code] = promo
    return promo


def disable_promo(code: str) -> bool:
    promo = PROMOS.get(code.upper())
    if not promo:
        return False
    promo.active = False
    return True


def use_promo(code: str) -> int | None:
    promo = PROMOS.get(code.upper())
    if not promo or not promo.active:
        return None
    promo.uses += 1
    return promo.bonus
