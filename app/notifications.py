from __future__ import annotations

import html
import logging

from aiogram import Bot

from app.config import get_settings
from app.db import Order, SessionLocal, get_order_fulfillment
from app.ui import screen, success

logger = logging.getLogger(__name__)
settings = get_settings()


async def notify_order_paid(
    bot: Bot, order: Order, *, notify_customer: bool = True
) -> None:
    amount = (
        f"{order.amount_stars} ⭐" if order.amount_stars else f"{order.amount_rub} ₽"
    )
    methods = {"telegram_stars": "Telegram Stars", "rollypay": "СБП"}
    async with SessionLocal() as session:
        fulfillment = await get_order_fulfillment(session, order.id)
    fulfillment_text = ""
    if fulfillment and fulfillment.method == "delivery":
        fee = (
            f"{fulfillment.fee_stars} ⭐"
            if fulfillment.fee_stars
            else f"{fulfillment.fee_rub} ₽"
        )
        fulfillment_text = (
            f"\n🚚 <b>Доставка</b> · доплата {fee}\n"
            f"📍 {html.escape(fulfillment.address)}"
        )
    elif fulfillment and fulfillment.method == "pickup":
        fulfillment_text = "\n📍 <b>Самовывоз:</b> ТЦ «Гостиный Двор»"
    if notify_customer:
        try:
            await bot.send_message(
                order.user_id,
                success(
                    "Оплата подтверждена",
                    f"🛍 {html.escape(order.title)}\n"
                    f"💳 {amount}\n"
                    f"🔖 <code>{order.id[:8]}</code>\n\n"
                    f"Заказ принят в работу.{fulfillment_text}",
                ),
            )
        except Exception:
            logger.exception(
                "Could not notify customer %s about order %s", order.user_id, order.id
            )

    for admin_id in settings.admins:
        try:
            await bot.send_message(
                admin_id,
                screen(
                    "💸",
                    "Новый оплаченный заказ",
                    f"🛍 {html.escape(order.title)}\n"
                    f"💳 {amount} · {methods.get(order.payment_method or '', order.payment_method or 'не указано')}\n"
                    f"👤 <code>{order.user_id}</code>\n"
                    f"🔖 <code>{order.id[:8]}</code>"
                    f"{fulfillment_text}",
                    "Платёж подтверждён автоматически",
                ),
            )
        except Exception:
            logger.exception(
                "Could not notify admin %s about order %s", admin_id, order.id
            )
