from __future__ import annotations

import logging
import html

from aiogram import Bot

from app.config import get_settings
from app.db import Order


logger = logging.getLogger(__name__)
settings = get_settings()


async def notify_order_paid(bot: Bot, order: Order, *, notify_customer: bool = True) -> None:
    if notify_customer:
        try:
            await bot.send_message(
                order.user_id,
                "✅ <b>Оплата подтверждена</b>\n\n"
                f"{html.escape(order.title)}\n"
                f"Заказ: <code>{order.id}</code>\n\n"
                "Мы уже получили заказ и скоро свяжемся с вами.",
            )
        except Exception:
            logger.exception("Could not notify customer %s about order %s", order.user_id, order.id)

    for admin_id in settings.admins:
        try:
            await bot.send_message(
                admin_id,
                "💸 <b>Новый оплаченный заказ</b>\n\n"
                f"Товар: {html.escape(order.title)}\n"
                f"Пользователь: <code>{order.user_id}</code>\n"
                f"Заказ: <code>{order.id}</code>",
            )
        except Exception:
            logger.exception("Could not notify admin %s about order %s", admin_id, order.id)
