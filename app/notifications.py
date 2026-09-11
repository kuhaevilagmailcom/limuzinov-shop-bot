from __future__ import annotations

import html
import logging

from aiogram import Bot

from app.config import get_settings
from app.db import (
    Order,
    Review,
    SessionLocal,
    get_order_fulfillment,
    get_user,
    has_review,
)
from app.keyboards import admin_order_open_keyboard, review_ask_keyboard
from app.ui import screen, success, warning

logger = logging.getLogger(__name__)
settings = get_settings()

PAYMENT_METHOD_LABELS = {
    "telegram_stars": "Telegram Stars",
    "rollypay": "СБП",
    "cash": "Наличные при получении",
}


def buyer_text(username: str | None, full_name: str, user_id: int) -> str:
    """One-line buyer identity: @username, name and numeric id."""
    parts = []
    if username:
        parts.append(f"@{html.escape(username)}")
    if full_name:
        parts.append(html.escape(full_name))
    parts.append(f"<code>{user_id}</code>")
    return " · ".join(parts)


async def fulfillment_text_for(order: Order) -> str:
    async with SessionLocal() as session:
        fulfillment = await get_order_fulfillment(session, order.id)
    if not fulfillment:
        return ""
    if fulfillment.method == "delivery":
        fee = (
            f"{fulfillment.fee_stars} ⭐"
            if fulfillment.fee_stars
            else f"{fulfillment.fee_rub} ₽"
        )
        text = (
            f"\n🚚 <b>Доставка</b> · доплата {fee}\n"
            f"📍 {html.escape(fulfillment.address)}"
        )
    elif fulfillment.method == "pickup":
        text = "\n📍 <b>Самовывоз:</b> ТЦ «Гостиный Двор»"
    else:
        text = ""
    if getattr(fulfillment, "scheduled_date", None):
        text += (
            f"\n📅 <b>Дата получения:</b> "
            f"{fulfillment.scheduled_date:%d.%m.%Y}"
        )
    if getattr(fulfillment, "scheduled_time", None):
        text += f"\n🕒 <b>Время получения:</b> {fulfillment.scheduled_time}"
    return text


def order_amount(order: Order) -> str:
    return f"{order.amount_stars} ⭐" if order.amount_stars else f"{order.amount_rub} ₽"


async def notify_order_paid(
    bot: Bot, order: Order, *, notify_customer: bool = True, schedule_note: str = ""
) -> None:
    amount = order_amount(order)
    async with SessionLocal() as session:
        buyer = await get_user(session, order.user_id)
    fulfillment_text = await fulfillment_text_for(order)
    if schedule_note and not fulfillment_text:
        fulfillment_text += f"\n📅 <b>Получение:</b> {schedule_note}"
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

    paid_in_cash = order.payment_method == "cash"
    method = order.payment_method or ""
    buyer_line = buyer_text(
        buyer.username if buyer else None,
        buyer.full_name if buyer else "",
        order.user_id,
    )
    for admin_id in settings.admins:
        try:
            await bot.send_message(
                admin_id,
                screen(
                    "💸",
                    "Новый заказ · наличные"
                    if paid_in_cash
                    else "Новый оплаченный заказ",
                    f"🛍 {html.escape(order.title)}\n"
                    f"💳 {amount} · "
                    f"{PAYMENT_METHOD_LABELS.get(method, method or 'не указано')}\n"
                    f"👤 {buyer_line}\n"
                    f"🔖 <code>{order.id[:8]}</code>"
                    f"{fulfillment_text}",
                    (
                        "Оплата наличными при получении — подтвердите выдачу"
                        if paid_in_cash
                        else "Платёж подтверждён автоматически"
                    ),
                ),
                reply_markup=admin_order_open_keyboard(order.id),
            )
        except Exception:
            logger.exception(
                "Could not notify admin %s about order %s", admin_id, order.id
            )


async def notify_order_issued(bot: Bot, order: Order) -> bool:
    """Thanks the customer for a handed-over order and offers the review button."""
    async with SessionLocal() as session:
        reviewed = await has_review(session, order.id)
    try:
        if reviewed:
            await bot.send_message(
                order.user_id,
                success(
                    "Заказ выполнен",
                    f"🛍 {html.escape(order.title)}\n"
                    f"🔖 <code>{order.id[:8]}</code>\n\n"
                    "Спасибо за заказ! Обращайтесь ещё 🙂",
                ),
            )
        else:
            await bot.send_message(
                order.user_id,
                success(
                    "Заказ выполнен",
                    f"🛍 {html.escape(order.title)}\n"
                    f"🔖 <code>{order.id[:8]}</code>\n\n"
                    "Спасибо за заказ! Будем благодарны за пару слов о покупке — "
                    "это займёт 10 секунд.",
                ),
                reply_markup=review_ask_keyboard(order.id),
            )
    except Exception:
        logger.exception(
            "Could not notify customer %s about issued order %s",
            order.user_id,
            order.id,
        )
        return False
    return True


async def notify_order_canceled(bot: Bot, order: Order) -> None:
    try:
        await bot.send_message(
            order.user_id,
            warning(
                "Заказ отменён",
                f"🛍 {html.escape(order.title)}\n"
                f"🔖 <code>{order.id[:8]}</code>\n\n"
                "Выдача не состоялась. Напишите в поддержку, если нужны детали.",
            ),
        )
    except Exception:
        logger.exception(
            "Could not notify customer %s about canceled order %s",
            order.user_id,
            order.id,
        )


def stars_line(rating: int) -> str:
    return "⭐️" * rating + "▫️" * (5 - rating)


async def notify_review(bot: Bot, review: Review) -> None:
    """Delivers a fresh review to every admin."""
    async with SessionLocal() as session:
        buyer = await get_user(session, review.user_id)
    buyer_line = buyer_text(
        buyer.username if buyer else None,
        buyer.full_name if buyer else "",
        review.user_id,
    )
    body = (
        f"🛍 {html.escape(review.title)}\n"
        f"{stars_line(review.rating)} · <b>{review.rating}/5</b>\n"
        f"👤 {buyer_line}\n"
        f"🔖 <code>{review.order_id[:8]}</code>"
    )
    if review.comment:
        body += f"\n\n💬 {html.escape(review.comment)}"
    else:
        body += "\n\n💬 Комментарий не оставлен"
    for admin_id in settings.admins:
        try:
            await bot.send_message(
                admin_id,
                screen("⭐️", "Новый отзыв", body, "Спасибо за обратную связь"),
                reply_markup=admin_order_open_keyboard(review.order_id),
            )
        except Exception:
            logger.exception(
                "Could not notify admin %s about review %s", admin_id, review.id
            )
