from __future__ import annotations

from decimal import Decimal

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from app.db import SessionLocal, User
from app.rewards_storage import add_bonus, attach_referral, get_promo

router = Router()


@router.message(Command("bonus"))
async def bonus_command(message: Message):
    async with SessionLocal() as session:
        user = await session.get(User, message.from_user.id)
        balance = user.balance_rub if user else Decimal("0")
    await message.answer(
        f"💎 <b>Ваши бонусы</b>\n\n"
        f"Баланс: <b>{balance} ₽</b>\n\n"
        "Бонусы можно использовать для будущих покупок."
    )


@router.message(Command("referral"))
async def referral_command(message: Message):
    code = f"LMZ{message.from_user.id}"
    await message.answer(
        "👥 <b>Реферальная программа</b>\n\n"
        f"Ваш код: <code>{code}</code>\n\n"
        "Приглашайте друзей и получайте бонусы за их покупки."
    )


@router.message(Command("promo"))
async def promo_command(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("🎁 Использование: /promo CODE")
        return

    code = parts[1].strip()
    async with SessionLocal() as session:
        promo = await get_promo(session, code)
        if not promo:
            await message.answer("❌ Промокод не найден или уже отключён")
            return

        await add_bonus(
            session,
            message.from_user.id,
            Decimal(str(promo.reward)),
            f"Промокод {promo.code}",
        )
        promo.uses += 1
        await session.commit()

    await message.answer(
        f"🎉 Промокод активирован!\n"
        f"Начислено: <b>{promo.reward} ₽</b>"
    )
