from __future__ import annotations

import html
import logging
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import func, select

from app.catalog import PRODUCTS, SONG_ORDER_STARS
from app.config import get_settings
from app.db import Order, OrderStatus, SessionLocal, User, create_order, get_or_create_user, mark_order_paid, recent_orders
from app.keyboards import catalog_keyboard, main_keyboard, payment_url_keyboard, product_keyboard
from app.notifications import notify_order_paid
from app.payments.cryptopay import CryptoPayError, create_invoice, get_invoice
from app.payments.rollypay import RollyPayError, create_payment, get_payment

router = Router()
settings = get_settings()
logger = logging.getLogger(__name__)


class SongOrderForm(StatesGroup):
    brief = State()


def money(value: Decimal) -> str:
    return f"{value:.2f}".replace(".00", "")


async def ensure_user(message: Message) -> User:
    async with SessionLocal() as session:
        return await get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )


async def menu_text(user: User) -> str:
    return (
        "✨ <b>LIMUZINOV SHOP</b>\n"
        "<i>Музыка и фирменные вещи</i>\n\n"
        f"Рады видеть, <b>{html.escape(user.full_name)}</b>.\n"
        f"У вас покупок: <b>{user.purchases_count}</b>\n\n"
        "Выберите, чем порадовать себя сегодня ↓"
    )


@router.message(CommandStart())
@router.message(F.text == "🏠 Главное меню")
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user(message)
    await message.answer(await menu_text(user), reply_markup=main_keyboard())


@router.message(F.text == "🛍 Каталог")
async def show_catalog(message: Message) -> None:
    await ensure_user(message)
    await message.answer(
        "🛍 <b>Витрина LIMUZINOV</b>\n\nВыберите товар — оплатить можно за пару касаний:",
        reply_markup=catalog_keyboard(),
    )


@router.callback_query(F.data == "catalog")
async def show_catalog_callback(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "🛍 <b>Витрина LIMUZINOV</b>\n\nВыберите товар — оплатить можно за пару касаний:",
        reply_markup=catalog_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("product:"))
async def product_card(callback: CallbackQuery) -> None:
    key = callback.data.split(":", 1)[1]
    product = PRODUCTS.get(key)
    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return
    await callback.message.edit_text(
        f"{product.emoji} <b>{product.title}</b>\n\n"
        f"{product.description}\n\n"
        f"Цена: <b>{product.price_rub} ₽</b>\n\n"
        "Выберите удобный способ оплаты:",
        reply_markup=product_keyboard(product.key),
    )
    if not (settings.rollypay_enabled or settings.cryptopay_enabled):
        await callback.message.answer("🚧 Оплата временно настраивается. Загляните чуть позже.")
    await callback.answer()


async def make_physical_order(callback: CallbackQuery, provider: str, product_key: str) -> None:
    product = PRODUCTS.get(product_key)
    if not product:
        await callback.answer("Товар не найден", show_alert=True)
        return
    if provider == "rolly" and not settings.rollypay_enabled:
        await callback.answer("Этот способ оплаты пока недоступен", show_alert=True)
        return
    if provider == "crypto" and not settings.cryptopay_enabled:
        await callback.answer("Этот способ оплаты пока недоступен", show_alert=True)
        return

    async with SessionLocal() as session:
        await get_or_create_user(
            session,
            callback.from_user.id,
            callback.from_user.username,
            callback.from_user.full_name,
        )
        order = await create_order(
            session,
            user_id=callback.from_user.id,
            kind="physical",
            product_key=product.key,
            title=product.title,
            description=product.description,
            amount_rub=Decimal(product.price_rub),
        )

        try:
            if provider == "rolly":
                payment = await create_payment(
                    order.id,
                    Decimal(product.price_rub),
                    f"{product.title} / заказ {order.id[:8]}",
                    callback.from_user.id,
                )
                order.payment_method = "rollypay"
                order.provider_payment_id = str(payment.get("payment_id", ""))
                pay_url = payment["pay_url"]
            else:
                payment = await create_invoice(
                    order.id,
                    Decimal(product.price_rub),
                    f"{product.title} / заказ {order.id[:8]}",
                )
                order.payment_method = "cryptopay"
                order.provider_payment_id = str(payment.get("invoice_id", ""))
                pay_url = payment["bot_invoice_url"]
            await session.commit()
        except (RollyPayError, CryptoPayError, KeyError) as exc:
            logger.exception("Payment creation failed for order %s: %s", order.id, exc)
            await callback.message.answer(
                "⚠️ Платёжная страница сейчас не ответила. Заказ сохранён — попробуйте ещё раз через минуту."
            )
            await callback.answer()
            return

    await callback.message.answer(
        f"🧾 <b>Заказ создан</b>\n\n"
        f"Товар: {product.title}\n"
        f"Сумма: <b>{product.price_rub} ₽</b>\n"
        f"Заказ: <code>{order.id}</code>\n\n"
        "Ссылка действует ограниченное время. После оплаты статус обновится автоматически.",
        reply_markup=payment_url_keyboard(pay_url, order.id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay:rolly:"))
async def pay_rolly(callback: CallbackQuery) -> None:
    await make_physical_order(callback, "rolly", callback.data.split(":", 2)[2])


@router.callback_query(F.data.startswith("pay:crypto:"))
async def pay_crypto(callback: CallbackQuery) -> None:
    await make_physical_order(callback, "crypto", callback.data.split(":", 2)[2])


@router.callback_query(F.data.startswith("status:"))
async def check_status(callback: CallbackQuery) -> None:
    order_id = callback.data.split(":", 1)[1]
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if not order or order.user_id != callback.from_user.id:
            await callback.answer("Заказ не найден", show_alert=True)
            return
        if order.status == OrderStatus.PAID.value:
            await callback.answer("✅ Уже оплачен", show_alert=True)
            return
        if order.payment_method == "rollypay" and order.provider_payment_id:
            try:
                payment = await get_payment(order.provider_payment_id)
                matches = (
                    str(payment.get("order_id", "")) == order.id
                    and str(payment.get("payment_id", "")) == order.provider_payment_id
                    and str(payment.get("payment_currency", payment.get("currency", ""))).upper() == "RUB"
                    and Decimal(str(payment.get("amount", "0"))) == order.amount_rub
                )
                if payment.get("status") == "paid" and matches:
                    order, changed = await mark_order_paid(
                        session,
                        order.id,
                        payment_method="rollypay",
                        provider_payment_id=str(payment.get("payment_id", "")),
                    )
                    if changed:
                        await callback.message.answer("✅ Платёж подтверждён. Заказ оплачен!")
                        await notify_order_paid(callback.bot, order, notify_customer=False)
                    await callback.answer("Оплачено", show_alert=True)
                    return
            except (RollyPayError, ValueError, ArithmeticError):
                logger.exception("Could not verify RollyPay order %s", order.id)
        elif order.payment_method == "cryptopay" and order.provider_payment_id:
            try:
                invoice = await get_invoice(order.provider_payment_id)
                matches = invoice and (
                    str(invoice.get("payload", "")) == order.id
                    and str(invoice.get("invoice_id", "")) == order.provider_payment_id
                    and str(invoice.get("fiat", "")).upper() == "RUB"
                    and Decimal(str(invoice.get("amount", "0"))) == order.amount_rub
                )
                if invoice and invoice.get("status") == "paid" and matches:
                    order, changed = await mark_order_paid(
                        session,
                        order.id,
                        payment_method="cryptopay",
                        provider_payment_id=str(invoice.get("invoice_id", "")),
                    )
                    if changed:
                        await callback.message.answer("✅ Платёж подтверждён. Заказ оплачен!")
                        await notify_order_paid(callback.bot, order, notify_customer=False)
                    await callback.answer("Оплачено", show_alert=True)
                    return
            except (CryptoPayError, ValueError, ArithmeticError):
                logger.exception("Could not verify Crypto Pay order %s", order.id)
        await callback.answer("Платёж пока не подтверждён", show_alert=True)


@router.message(F.text == "🎵 Заказать песню")
async def song_start(message: Message, state: FSMContext) -> None:
    await ensure_user(message)
    await state.set_state(SongOrderForm.brief)
    await message.answer(
        "🎵 <b>Заказ песни</b>\n\n"
        "Одним сообщением напишите:\n"
        "• тему/сюжет;\n"
        "• стиль и настроение;\n"
        "• имя/слова, которые обязательно использовать;\n"
        "• дополнительные пожелания.\n\n"
        "После этого бот выставит счёт в Telegram Stars."
    )


@router.message(SongOrderForm.brief)
async def song_brief(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.text or len(message.text.strip()) < 5:
        await message.answer("Напишите чуть подробнее, что должно быть в песне.")
        return

    async with SessionLocal() as session:
        await get_or_create_user(session, message.from_user.id, message.from_user.username, message.from_user.full_name)
        order = await create_order(
            session,
            user_id=message.from_user.id,
            kind="digital_song",
            product_key="song",
            title="Заказ песни",
            description=message.text.strip(),
            amount_stars=SONG_ORDER_STARS,
        )

    await state.clear()
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Заказ песни",
        description="Индивидуальный заказ песни по вашему ТЗ.",
        payload=f"song:{order.id}",
        currency="XTR",
        prices=[LabeledPrice(label="Заказ песни", amount=SONG_ORDER_STARS)],
        provider_token="",
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    if query.currency != "XTR" or not query.invoice_payload.startswith("song:"):
        await query.answer(ok=False, error_message="Неизвестный заказ")
        return
    order_id = query.invoice_payload.split(":", 1)[1]
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if (
            not order
            or order.user_id != query.from_user.id
            or order.status != OrderStatus.CREATED.value
            or order.amount_stars != query.total_amount
        ):
            await query.answer(ok=False, error_message="Заказ уже обработан или не найден")
            return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def stars_success(message: Message, bot: Bot) -> None:
    payment = message.successful_payment
    if payment.currency != "XTR" or not payment.invoice_payload.startswith("song:"):
        return
    order_id = payment.invoice_payload.split(":", 1)[1]
    async with SessionLocal() as session:
        expected = await session.get(Order, order_id)
        if (
            not expected
            or expected.user_id != message.from_user.id
            or expected.amount_stars != payment.total_amount
        ):
            logger.error("Rejected mismatched Stars payment for order %s", order_id)
            return
        order, changed = await mark_order_paid(
            session,
            order_id,
            payment_method="telegram_stars",
            provider_payment_id=payment.telegram_payment_charge_id,
        )
    if not order:
        return

    await message.answer(
        "✅ <b>Оплата получена!</b>\n\n"
        f"Заказ: <code>{order.id}</code>\n"
        "Техническое задание сохранено. Администратор получил уведомление."
    )
    if changed:
        for admin_id in settings.admins:
            try:
                await bot.send_message(
                    admin_id,
                    "🎵 <b>Новый оплаченный заказ песни</b>\n\n"
                    f"Пользователь: <code>{order.user_id}</code>\n"
                    f"Заказ: <code>{order.id}</code>\n"
                    f"ТЗ:\n{html.escape(order.description[:3000])}",
                )
            except Exception:
                pass


@router.message(F.text.in_({"👤 Профиль", "💰 Баланс"}))
async def balance(message: Message) -> None:
    user = await ensure_user(message)
    await message.answer(
        "👤 <b>Ваш профиль</b>\n\n"
        f"Имя: <b>{html.escape(user.full_name)}</b>\n"
        f"Всего покупок: <b>{user.purchases_count}</b>"
    )


@router.message(F.text == "📦 Мои покупки")
async def my_orders(message: Message) -> None:
    await ensure_user(message)
    async with SessionLocal() as session:
        orders = await recent_orders(session, message.from_user.id, 10)
    if not orders:
        await message.answer("📦 У вас пока нет заказов.")
        return
    labels = {
        "created": "🕓 ожидает оплаты",
        "paid": "✅ оплачен",
        "canceled": "❌ отменён",
        "expired": "⌛ истёк",
        "refunded": "↩️ возврат",
        "processing": "⚡ обрабатывается",
        "chargeback": "↩️ отмена платежа",
    }
    text = ["📦 <b>Последние заказы</b>\n"]
    for order in orders:
        amount = f"{order.amount_stars} ⭐" if order.amount_stars else f"{money(order.amount_rub or Decimal(0))} ₽"
        text.append(f"• {order.title} — {amount}\n  {labels.get(order.status, order.status)} · <code>{order.id[:8]}</code>")
    await message.answer("\n".join(text))


@router.message(F.text == "🆘 Поддержка")
@router.message(Command("paysupport"))
async def support(message: Message) -> None:
    await message.answer(
        "🆘 <b>Поддержка</b>\n\n"
        "По вопросу заказа отправьте администратору номер заказа из раздела «Мои покупки».\n"
        "Укажите, что именно произошло с оплатой или заказом."
    )


@router.message(Command("admin"))
async def admin_panel(message: Message) -> None:
    if message.from_user.id not in settings.admins:
        return
    async with SessionLocal() as session:
        users_count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        orders_count = (await session.execute(select(func.count()).select_from(Order))).scalar_one()
        paid_count = (await session.execute(select(func.count()).select_from(Order).where(Order.status == "paid"))).scalar_one()
    await message.answer(
        "🛠 <b>Админ-панель</b>\n\n"
        f"Пользователей: <b>{users_count}</b>\n"
        f"Заказов: <b>{orders_count}</b>\n"
        f"Оплачено: <b>{paid_count}</b>"
    )
