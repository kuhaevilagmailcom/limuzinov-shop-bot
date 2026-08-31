from __future__ import annotations


def referral_start_value(start_arg: str | None) -> str | None:
    if not start_arg:
        return None
    value = start_arg.strip()
    if value.startswith("LMZ"):
        return value
    return None


def referral_reward(first_purchase: bool = True) -> int:
    return 100 if first_purchase else 0
