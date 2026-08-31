from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


@router.message(Command("bonus"))
async def bonus_command(message: Message):
    await message.answer("💎 Бонусная система скоро доступна. Здесь будет ваш баланс и история начислений.")


@router.message(Command("referral"))
async def referral_command(message: Message):
    await message.answer("👥 Ваша реферальная ссылка будет отображаться здесь.")


@router.message(Command("promo"))
async def promo_command(message: Message):
    await message.answer("🎁 Введите промокод после команды: /promo CODE")
