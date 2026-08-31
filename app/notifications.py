from __future__ import annotations

import logging
import html

from aiogram import Bot

from app.config import get_settings
from app.db import Order
from app.ui import screen, success


logger = logging.getLogger(__name__)
settings = get_settings()


async def notify_order_paid(bot: Bot, order: Order, *, notify_customer: bool = True) -> None:
    amount = f"{order.amount_stars} ⭐" if order.amount_stars else f"{order.amount_rub} ₽"
    methods = {"telegram_stars": "Telegram Stars", "rollypay": "СБП"}
    if notify_customer:
        try:
            await bot.send_message(
                order.user_id,
                success(
                    "Оплата подтверждена",
                    f"🛍 {html.escape(order.title)}\n"
                    f"💳 {amount}\n"
                    f"🔖 <code>{order.id[:8]}</code>\n\n"
                    "Заказ принят в работу.",
                ),
            )
        except Exception:
            logger.exception("Could not notify customer %s about order %s", order.user_id, order.id)

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
                    f"🔖 <code>{order.id[:8]}</code>",
                    "Платёж подтверждён автоматически",
                ),
            )
        except Exception:
            logger.exception("Could not notify admin %s about order %s", admin_id, order.id)
