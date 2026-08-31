from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from decimal import Decimal

from app.config import get_settings
from app.db import SessionLocal
from app.rewards_models import PromoCode

router = Router()
settings = get_settings()


def is_admin(user_id: int) -> bool:
    return user_id in settings.admins


@router.message(Command("createpromo"))
async def create_promo(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Использование: /createpromo CODE BONUS")
        return

    code = parts[1].upper()
    reward = Decimal(parts[2])

    async with SessionLocal() as session:
        existing = await session.get(PromoCode, code)
        if existing:
            await message.answer("Такой промокод уже есть")
            return
        session.add(PromoCode(code=code, reward=reward))
        await session.commit()

    await message.answer(f"🎁 Промокод создан: {code}\nБонус: {reward} ₽")
