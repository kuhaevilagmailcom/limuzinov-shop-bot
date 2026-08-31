from __future__ import annotations

from decimal import Decimal

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.db import SessionLocal, User
from app.rewards_storage import add_bonus, get_promo, attach_referral

router = Router()

REFERRAL_BONUS = Decimal("100")


@router.message(Command("bonus"))
async def bonus_command(message: Message):
    async with SessionLocal() as session:
        user = await session.get(User, message.from_user.id)
        balance = user.balance_rub if user else Decimal("0")

    await message.answer(
        f"💎 <b>Ваши бонусы</b>\n\n"
        f"Баланс: <b>{balance} ₽</b>\n\n"
        "Используйте бонусы для будущих покупок."
    )


@router.message(Command("start"))
async def referral_start(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return

    code = args[1].strip()
    if not code.startswith("LMZ"):
        return

    try:
        owner_id = int(code.replace("LMZ", ""))
    except ValueError:
        return

    if owner_id == message.from_user.id:
        return

    async with SessionLocal() as session:
        await attach_referral(session, owner_id, message.from_user.id)


@router.message(Command("referral"))
async def referral_command(message: Message):
    code = f"LMZ{message.from_user.id}"
    await message.answer(
        "👥 <b>Реферальная программа</b>\n\n"
        f"Ваша ссылка:\n"
        f"<code>https://t.me/LIMYZINOV_BOT?start={code}</code>\n\n"
        "Приглашайте друзей и получайте бонусы."
    )


@router.message(Command("promo"))
async def promo_command(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("🎁 Использование: /promo CODE")
        return

    async with SessionLocal() as session:
        promo = await get_promo(session, parts[1].strip())
        if not promo:
            await message.answer("❌ Промокод не найден")
            return

        await add_bonus(
            session,
            message.from_user.id,
            Decimal(str(promo.reward)),
            f"Промокод {promo.code}",
        )
        promo.uses += 1
        await session.commit()

    await message.answer(f"🎉 Начислено <b>{promo.reward} ₽</b>")
