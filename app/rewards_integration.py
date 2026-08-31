from __future__ import annotations

from decimal import Decimal

from app.bonuses import calculate_purchase_bonus, build_referral_link


DEFAULT_REFERRAL_REWARD = Decimal("100")


def user_rewards_text(user) -> str:
    return (
        "💎 Бонусный баланс\n\n"
        f"Доступно: {getattr(user, 'balance_rub', Decimal('0'))} ₽\n\n"
        "Получайте бонусы за покупки и приглашение друзей."
    )


def referral_text(bot_username: str, code: str) -> str:
    return (
        "👥 Реферальная программа\n\n"
        "Приглашайте друзей и получайте бонусы.\n\n"
        f"Ваша ссылка:\n{build_referral_link(bot_username, code)}"
    )


def promo_result_text(ok: bool, value: Decimal = Decimal('0')) -> str:
    if not ok:
        return "❌ Промокод недействителен"
    return f"✅ Промокод применён\nБонус: {value} ₽"


def purchase_bonus_text(amount: Decimal) -> str:
    return f"🎁 За покупку начислено {calculate_purchase_bonus(amount)} ₽ бонусов"
