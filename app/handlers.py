from __future__ import annotations

import html
import logging
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import func, select

from app.config import get_settings
from app.db import (
    Order,
    OrderStatus,
    Product,
    SessionLocal,
    User,
    active_products,
    all_products,
    create_order,
    get_or_create_user,
    get_product,
    mark_order_paid,
    recent_orders,
)
from app.keyboards import (
    admin_keyboard,
    admin_product_keyboard,
    admin_products_keyboard,
    catalog_keyboard,
    main_keyboard,
    payment_url_keyboard,
    product_keyboard,
    product_price,
)
from app.notifications import notify_order_paid
from app.payments.cryptopay import CryptoPayError, create_invoice, get_invoice
from app.payments.rollypay import RollyPayError, create_payment, get_payment

router = Router()
settings = get_settings()
logger = logging.getLogger(__name__)
BOT_COVER = Path(__file__).resolve().parent / "static" / "brand" / "hero-banner.png"


class ProductOrderForm(StatesGroup):
    brief = State()


class AdminAddForm(StatesGroup):
    title = State()
    description = State()
    prices = State()
    kind = State()


class AdminEditForm(StatesGroup):
    value = State()


def money(value: Decimal) -> str:
    return f"{value:.2f}".replace(".00", "")


def is_admin(user_id: int) -> bool:
    return user_id in settings.admins


async def ensure_user(message: Message) -> User:
    async with SessionLocal() as session:
        return await get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )


def menu_text(user: User) -> str:
    return (
        "✨ <b>LIMYZINOV SHOP</b>\n"
        "<i>Музыка и фирменные вещи</i>\n\n"
        f"Рады видеть, <b>{html.escape(user.full_name)}</b>.\n"
        f"У вас покупок: <b>{user.purchases_count}</b>\n\n"
        "Откройте каталог и выберите свой вайб ↓"
    )


@router.message(CommandStart())
@router.message(F.text == "🏠 Главное меню")
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user(message)
    if BOT_COVER.exists():
        await message.answer_photo(FSInputFile(BOT_COVER), caption=menu_text(user), reply_markup=main_keyboard())
    else:
        await message.answer(menu_text(user), reply_markup=main_keyboard())


async def send_catalog(message: Message, *, edit: bool = False) -> None:
    async with SessionLocal() as session:
        products = await active_products(session)
    text = "🛍 <b>Витрина LIMYZINOV</b>\n\nВыберите товар:"
    if not products:
        text = "🛍 Каталог пока пуст. Скоро здесь появятся товары."
    if edit:
        await message.edit_text(text, reply_markup=catalog_keyboard(products))
    else:
        await message.answer(text, reply_markup=catalog_keyboard(products))


@router.message(F.text == "🛍 Каталог")
async def show_catalog(message: Message) -> None:
    await ensure_user(message)
    await send_catalog(message)


@router.callback_query(F.data == "catalog")
async def show_catalog_callback(callback: CallbackQuery) -> None:
    await send_catalog(callback.message, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("product:"))
async def product_card(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        product = await get_product(session, int(callback.data.rsplit(":", 1)[1]))
        if not product or not product.is_active:
            await callback.answer("Товар не найден", show_alert=True)
            return
        await callback.message.edit_text(
            f"{product.emoji} <b>{html.escape(product.title)}</b>\n\n"
            f"{html.escape(product.description)}\n\n"
            f"Цена: <b>{product_price(product)}</b>\n\n"
            "Выберите удобный способ оплаты:",
            reply_markup=product_keyboard(product),
        )
    await callback.answer()


async def send_payment(
    message: Message,
    bot: Bot,
    user_id: int,
    username: str | None,
    full_name: str,
    product_id: int,
    provider: str,
    brief: str = "",
) -> None:
    async with SessionLocal() as session:
        product = await get_product(session, product_id)
        if not product or not product.is_active:
            await message.answer("Товар больше недоступен.")
            return
        await get_or_create_user(session, user_id, username, full_name)
        description = brief.strip() if brief else product.description
        order = await create_order(
            session,
            user_id=user_id,
            kind=product.kind,
            product_key=product.key,
            title=product.title,
            description=description,
            amount_rub=Decimal(product.price_rub) if product.price_rub and provider != "stars" else None,
            amount_stars=product.price_stars if provider == "stars" else None,
        )

        if provider == "stars":
            if not product.price_stars:
                await message.answer("Оплата Stars для этого товара не настроена.")
                return
            order.payment_method = "telegram_stars"
            await session.commit()
            await bot.send_invoice(
                chat_id=message.chat.id,
                title=product.title[:32],
                description=(product.description.strip() or "Заказ в LIMYZINOV SHOP")[:255],
                payload=f"order:{order.id}",
                currency="XTR",
                prices=[LabeledPrice(label=product.title[:32], amount=product.price_stars)],
                provider_token="",
            )
            return

        if not product.price_rub:
            await message.answer("Рублёвая цена для этого товара не настроена.")
            return
        try:
            if provider == "rolly":
                payment = await create_payment(order.id, Decimal(product.price_rub), f"{product.title} / заказ {order.id[:8]}", user_id)
                order.payment_method = "rollypay"
                order.provider_payment_id = str(payment.get("payment_id", ""))
                pay_url = payment["pay_url"]
            else:
                payment = await create_invoice(order.id, Decimal(product.price_rub), f"{product.title} / заказ {order.id[:8]}")
                order.payment_method = "cryptopay"
                order.provider_payment_id = str(payment.get("invoice_id", ""))
                pay_url = payment["bot_invoice_url"]
            await session.commit()
        except (RollyPayError, CryptoPayError, KeyError) as exc:
            logger.exception("Payment creation failed for order %s: %s", order.id, exc)
            await message.answer("⚠️ Платёжная страница сейчас не ответила. Попробуйте ещё раз через минуту.")
            return

    await message.answer(
        "🧾 <b>Заказ создан</b>\n\n"
        f"Товар: {html.escape(product.title)}\n"
        f"Сумма: <b>{product.price_rub} ₽</b>\n"
        f"Заказ: <code>{order.id}</code>\n\n"
        "После оплаты статус обновится автоматически.",
        reply_markup=payment_url_keyboard(pay_url, order.id),
    )


@router.callback_query(F.data.startswith("buy:"))
async def buy_product(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    _, provider, raw_id = callback.data.split(":", 2)
    if provider not in {"rolly", "stars"}:
        await callback.answer("Этот способ оплаты недоступен", show_alert=True)
        return
    product_id = int(raw_id)
    async with SessionLocal() as session:
        product = await get_product(session, product_id)
    if not product or not product.is_active:
        await callback.answer("Товар не найден", show_alert=True)
        return
    if provider == "rolly" and not settings.rollypay_enabled:
        await callback.answer("Этот способ оплаты пока недоступен", show_alert=True)
        return
    if product.requires_brief:
        await state.set_state(ProductOrderForm.brief)
        await state.update_data(product_id=product.id, provider=provider)
        await callback.message.answer(
            f"{product.emoji} <b>{html.escape(product.title)}</b>\n\n"
            "Одним сообщением напишите тему, стиль, настроение, нужные имена/слова и пожелания."
        )
        await callback.answer()
        return
    await send_payment(callback.message, bot, callback.from_user.id, callback.from_user.username, callback.from_user.full_name, product.id, provider)
    await callback.answer()


@router.message(ProductOrderForm.brief)
async def product_brief(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.text or len(message.text.strip()) < 5:
        await message.answer("Напишите чуть подробнее — хотя бы 5 символов.")
        return
    data = await state.get_data()
    await state.clear()
    await send_payment(message, bot, message.from_user.id, message.from_user.username, message.from_user.full_name, int(data["product_id"]), str(data["provider"]), message.text)


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
        try:
            if order.payment_method == "rollypay" and order.provider_payment_id:
                payment = await get_payment(order.provider_payment_id)
                matches = str(payment.get("order_id", "")) == order.id and str(payment.get("payment_id", "")) == order.provider_payment_id and str(payment.get("payment_currency", payment.get("currency", ""))).upper() == "RUB" and Decimal(str(payment.get("amount", "0"))) == order.amount_rub
                if payment.get("status") == "paid" and matches:
                    order, changed = await mark_order_paid(session, order.id, payment_method="rollypay", provider_payment_id=order.provider_payment_id)
                else:
                    changed = False
            elif order.payment_method == "cryptopay" and order.provider_payment_id:
                invoice = await get_invoice(order.provider_payment_id)
                matches = invoice and str(invoice.get("payload", "")) == order.id and str(invoice.get("invoice_id", "")) == order.provider_payment_id and str(invoice.get("fiat", "")).upper() == "RUB" and Decimal(str(invoice.get("amount", "0"))) == order.amount_rub
                if invoice and invoice.get("status") == "paid" and matches:
                    order, changed = await mark_order_paid(session, order.id, payment_method="cryptopay", provider_payment_id=order.provider_payment_id)
                else:
                    changed = False
            else:
                changed = False
            if changed:
                await callback.message.answer("✅ Платёж подтверждён. Заказ оплачен!")
                await notify_order_paid(callback.bot, order, notify_customer=False)
                await callback.answer("Оплачено", show_alert=True)
                return
        except (RollyPayError, CryptoPayError, ValueError, ArithmeticError):
            logger.exception("Could not verify order %s", order.id)
    await callback.answer("Платёж пока не подтверждён", show_alert=True)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    if query.currency != "XTR" or not query.invoice_payload.startswith("order:"):
        await query.answer(ok=False, error_message="Неизвестный заказ")
        return
    order_id = query.invoice_payload.split(":", 1)[1]
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        valid = order and order.user_id == query.from_user.id and order.status == OrderStatus.CREATED.value and order.payment_method == "telegram_stars" and order.amount_stars == query.total_amount
    await query.answer(ok=bool(valid), error_message=None if valid else "Заказ уже обработан или не найден")


@router.message(F.successful_payment)
async def stars_success(message: Message, bot: Bot) -> None:
    payment = message.successful_payment
    if payment.currency != "XTR" or not payment.invoice_payload.startswith("order:"):
        return
    order_id = payment.invoice_payload.split(":", 1)[1]
    async with SessionLocal() as session:
        expected = await session.get(Order, order_id)
        if not expected or expected.user_id != message.from_user.id or expected.amount_stars != payment.total_amount or expected.payment_method != "telegram_stars":
            logger.error("Rejected mismatched Stars payment for order %s", order_id)
            return
        order, changed = await mark_order_paid(session, order_id, payment_method="telegram_stars", provider_payment_id=payment.telegram_payment_charge_id)
    if order:
        await message.answer("✅ <b>Оплата получена!</b>\n\n" f"Заказ: <code>{order.id}</code>\n" "Мы получили заказ и скоро свяжемся с вами.")
        if changed:
            await notify_order_paid(bot, order, notify_customer=False)


@router.message(F.text.in_({"👤 Профиль", "💰 Баланс"}))
async def profile(message: Message) -> None:
    user = await ensure_user(message)
    await message.answer("👤 <b>Ваш профиль</b>\n\n" f"Имя: <b>{html.escape(user.full_name)}</b>\n" f"Всего покупок: <b>{user.purchases_count}</b>")


@router.message(F.text == "📦 Мои покупки")
async def my_orders(message: Message) -> None:
    await ensure_user(message)
    async with SessionLocal() as session:
        orders = await recent_orders(session, message.from_user.id, 10)
    if not orders:
        await message.answer("📦 У вас пока нет заказов.")
        return
    labels = {"created": "🕓 ожидает оплаты", "processing": "⚡ обрабатывается", "paid": "✅ оплачен", "canceled": "❌ отменён", "expired": "⌛ истёк", "refunded": "↩️ возврат", "chargeback": "↩️ отмена платежа"}
    rows = ["📦 <b>Последние заказы</b>\n"]
    for order in orders:
        amount = f"{order.amount_stars} ⭐" if order.amount_stars else f"{money(order.amount_rub or Decimal(0))} ₽"
        rows.append(f"• {html.escape(order.title)} — {amount}\n  {labels.get(order.status, order.status)} · <code>{order.id[:8]}</code>")
    await message.answer("\n".join(rows))


@router.message(F.text == "🆘 Поддержка")
@router.message(Command("paysupport"))
async def support(message: Message) -> None:
    await message.answer("🆘 <b>Поддержка</b>\n\nОтправьте администратору номер заказа из раздела «Мои покупки» и коротко опишите проблему.")


async def admin_home(target: Message) -> None:
    async with SessionLocal() as session:
        users_count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        orders_count = (await session.execute(select(func.count()).select_from(Order))).scalar_one()
        paid_count = (await session.execute(select(func.count()).select_from(Order).where(Order.status == "paid"))).scalar_one()
        products_count = (await session.execute(select(func.count()).select_from(Product))).scalar_one()
    text = "🛠 <b>Админ-панель</b>\n\n" f"Товаров: <b>{products_count}</b>\nПользователей: <b>{users_count}</b>\nЗаказов: <b>{orders_count}</b>\nОплачено: <b>{paid_count}</b>"
    await target.answer(text, reply_markup=admin_keyboard())


@router.message(Command("id"))
async def show_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к админ-панели.")
        return
    await admin_home(message)


@router.callback_query(F.data.startswith("admin:"))
async def admin_callbacks(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    action = callback.data.split(":")
    if action[1] == "home":
        await admin_home(callback.message)
    elif action[1] == "products":
        async with SessionLocal() as session:
            products = await all_products(session)
        await callback.message.answer("📦 <b>Товары</b>\n\nВыберите позицию:", reply_markup=admin_products_keyboard(products))
    elif action[1] == "product":
        async with SessionLocal() as session:
            product = await get_product(session, int(action[2]))
            if not product:
                await callback.answer("Товар не найден", show_alert=True)
                return
            await callback.message.answer(
                f"{product.emoji} <b>{html.escape(product.title)}</b>\n\n{html.escape(product.description)}\n\nЦена: <b>{product_price(product)}</b>\nСтатус: {'показывается' if product.is_active else 'скрыт'}",
                reply_markup=admin_product_keyboard(product),
            )
    elif action[1] == "toggle":
        async with SessionLocal() as session:
            product = await get_product(session, int(action[2]))
            if product:
                product.is_active = not product.is_active
                await session.commit()
        await callback.answer("Статус изменён", show_alert=True)
        return
    elif action[1] == "add":
        await state.set_state(AdminAddForm.title)
        await callback.message.answer("➕ <b>Новый товар</b>\n\nВведите название товара:")
    elif action[1] == "edit":
        field, product_id = action[2], int(action[3])
        await state.set_state(AdminEditForm.value)
        await state.update_data(field=field, product_id=product_id)
        prompts = {"rub": "Введите новую цену для СБП в рублях (больше 0):", "stars": "Введите новую цену в Stars (больше 0):", "text": "Введите <code>Название | Описание</code>:"}
        await callback.message.answer(prompts[field])
    await callback.answer()


@router.message(AdminAddForm.title)
async def admin_add_title(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id) or not message.text:
        return
    await state.update_data(title=message.text.strip())
    await state.set_state(AdminAddForm.description)
    await message.answer("Введите описание товара:")


@router.message(AdminAddForm.description)
async def admin_add_description(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id) or not message.text:
        return
    await state.update_data(description=message.text.strip())
    await state.set_state(AdminAddForm.prices)
    await message.answer("Введите обе цены в формате <code>СБП в рублях / Stars</code>.\nНапример: <code>990 / 350</code>.")


@router.message(AdminAddForm.prices)
async def admin_add_prices(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id) or not message.text:
        return
    try:
        rub, stars = [int(v.strip()) for v in message.text.split("/", 1)]
        if rub <= 0 or stars <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Нужен формат <code>990 / 350</code>. Обе цены должны быть больше нуля.")
        return
    await state.update_data(price_rub=rub, price_stars=stars)
    await state.set_state(AdminAddForm.kind)
    await message.answer("Введите тип товара: <code>physical</code> или <code>digital</code>.")


@router.message(AdminAddForm.kind)
async def admin_add_kind(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id) or not message.text:
        return
    kind = message.text.strip().lower()
    if kind not in {"physical", "digital"}:
        await message.answer("Введите physical или digital.")
        return
    data = await state.get_data()
    async with SessionLocal() as session:
        product = Product(
            key=f"item-{uuid4().hex[:10]}",
            title=data["title"][:255],
            description=data["description"],
            price_rub=data["price_rub"],
            price_stars=data["price_stars"],
            emoji="🛍",
            kind=kind,
            requires_brief=False,
        )
        session.add(product)
        await session.commit()
        await session.refresh(product)
    await state.clear()
    await message.answer(f"✅ Товар <b>{html.escape(product.title)}</b> создан.", reply_markup=admin_product_keyboard(product))


@router.message(AdminEditForm.value)
async def admin_edit_value(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id) or not message.text:
        return
    data = await state.get_data()
    field, product_id = data["field"], int(data["product_id"])
    async with SessionLocal() as session:
        product = await get_product(session, product_id)
        if not product:
            await state.clear()
            await message.answer("Товар не найден.")
            return
        try:
            if field == "rub":
                value = int(message.text.strip())
                product.price_rub = value or None
            elif field == "stars":
                value = int(message.text.strip())
                product.price_stars = value or None
            else:
                title, description = [v.strip() for v in message.text.split("|", 1)]
                product.title, product.description = title[:255], description
                value = 1
            if value <= 0 or not product.price_rub or not product.price_stars:
                raise ValueError
        except (ValueError, TypeError):
            await message.answer("Неверное значение. Для товара обязательны обе цены: СБП и Stars, обе больше нуля.")
            return
        await session.commit()
        await session.refresh(product)
    await state.clear()
    await message.answer("✅ Товар обновлён.", reply_markup=admin_product_keyboard(product))
