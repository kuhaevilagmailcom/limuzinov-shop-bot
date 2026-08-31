from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

from app.db import SessionLocal, User, Order, OrderStatus

router = Router()


@router.message(Command("stats"))
async def stats(message: Message):
    async with SessionLocal() as session:
        users = await session.scalar(select(func.count(User.telegram_id))) or 0
        paid = await session.scalar(
            select(func.count(Order.id)).where(Order.status == OrderStatus.PAID.value)
        ) or 0
        sales = await session.scalar(
            select(func.sum(Order.amount_rub)).where(Order.status == OrderStatus.PAID.value)
        ) or 0

    await message.answer(
        "📊 <b>Аналитика LIMYZINOV SHOP</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"🧾 Оплаченных заказов: <b>{paid}</b>\n"
        f"💰 Продажи: <b>{sales} ₽</b>"
    )
