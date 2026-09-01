from __future__ import annotations

import hashlib
import html
import logging
import re
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy import func, select

from app.config import OWNER_ADMIN_ID, get_settings
from app.db import (
    BonusAccount,
    Order,
    OrderStatus,
    Product,
    PromoCode,
    SessionLocal,
    SupportMessage,
    SupportStatus,
    SupportTicket,
    User,
    active_products,
    add_support_message,
    all_products,
    apply_referral,
    create_order,
    create_promo_code,
    create_support_ticket,
    get_active_support_ticket,
    get_bonus_account,
    get_or_create_user,
    get_product,
    get_shop_analytics,
    list_promo_codes,
    list_support_tickets,
    mark_order_paid,
    recent_bonus_transactions,
    recent_orders,
    recent_payment_events,
    record_payment_event,
    redeem_promo_code,
    register_user,
    set_support_ticket_status,
    support_rate_limited,
    support_ticket_messages,
)
from app.keyboards import (
    admin_cancel_keyboard,
    admin_keyboard,
    admin_product_keyboard,
    admin_products_keyboard,
    admin_promos_keyboard,
    bonus_cancel_keyboard,
    bonus_keyboard,
    catalog_keyboard,
    main_keyboard,
    payment_url_keyboard,
    product_keyboard,
    product_kind_keyboard,
    product_price,
    support_cancel_keyboard,
    support_ticket_keyboard,
    support_tickets_keyboard,
)
from app.notifications import notify_order_paid
from app.payments.rollypay import RollyPayError, create_payment, get_payment
from app.ui import ORDER_STATUS_LABELS, screen, success, warning

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


class SupportUserForm(StatesGroup):
    content = State()


class SupportReplyForm(StatesGroup):
    content = State()


class PromoUserForm(StatesGroup):
    code = State()


class AdminPromoForm(StatesGroup):
    value = State()


SUPPORT_CONTENT_TYPES = {
    ContentType.TEXT,
    ContentType.PHOTO,
    ContentType.VIDEO,
    ContentType.DOCUMENT,
    ContentType.VOICE,
}


def money(value: Decimal) -> str:
    return f"{value:.2f}".replace(".00", "")


def is_admin(user_id: int) -> bool:
    return user_id in settings.admins


def is_support_admin(user_id: int) -> bool:
    return user_id == OWNER_ADMIN_ID


def support_message_body(message: Message) -> str:
    return (message.text or message.caption or "").strip()[:4000]


def support_content_type(message: Message) -> str:
    return message.content_type.value if hasattr(message.content_type, "value") else str(message.content_type)


def support_ticket_text(ticket: SupportTicket) -> str:
    username = f"@{html.escape(ticket.username)}" if ticket.username else "не указан"
    labels = {
        SupportStatus.NEW.value: "🆘 новое",
        SupportStatus.ANSWERED.value: "💬 дан ответ",
        SupportStatus.CLOSED.value: "✅ закрыто",
    }
    return screen(
        "💬",
        f"Обращение #{ticket.id}",
        f"{labels.get(ticket.status, ticket.status)}\n\n"
        f"👤 <b>{html.escape(ticket.full_name)}</b>\n"
        f"🔗 {username}\n"
        f"🆔 <code>{ticket.user_id}</code>\n"
        f"🕒 <code>{ticket.created_at:%d.%m.%Y %H:%M}</code>",
    )


def support_history_text(messages: list[SupportMessage]) -> str:
    if not messages:
        return "\n\nИстория пока пуста."
    content_labels = {
        "photo": "[фотография]",
        "video": "[видео]",
        "document": "[документ]",
        "voice": "[голосовое сообщение]",
    }
    rows = ["\n\n<b>Последние сообщения:</b>"]
    for item in messages:
        sender = "👤 Покупатель" if item.sender == "user" else "👑 Вы"
        body = html.escape(item.body[:350]) if item.body else content_labels.get(item.content_type, f"[{html.escape(item.content_type)}]")
        rows.append(f"\n{sender}: {body}")
        if sum(len(row) for row in rows) > 3000:
            rows.append("\n…")
            break
    return "".join(rows)


async def ensure_user(message: Message) -> User:
    async with SessionLocal() as session:
        return await get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )


def menu_text(user: User) -> str:
    return screen(
        "✨",
        "LIMYZINOV SHOP",
        f"Привет, <b>{html.escape(user.full_name)}</b>!\n\n"
        "🛍 Выбирайте товары\n"
        "⚡ Оплачивайте по СБП\n"
        "⭐ Или используйте Telegram Stars",
        "Всё просто — нужный раздел уже в меню",
    )


async def send_home(message: Message, state: FSMContext, user: User) -> None:
    await state.clear()
    if BOT_COVER.exists():
        await message.answer_photo(FSInputFile(BOT_COVER), caption=menu_text(user), reply_markup=main_keyboard(is_admin(message.from_user.id)))
    else:
        await message.answer(menu_text(user), reply_markup=main_keyboard(is_admin(message.from_user.id)))


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, command: CommandObject) -> None:
    async with SessionLocal() as session:
        user, created = await register_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )
        if created and command.args and command.args.startswith("ref_"):
            raw_referrer = command.args.removeprefix("ref_")
            if raw_referrer.isdigit():
                rewarded = await apply_referral(
                    session,
                    new_user_id=message.from_user.id,
                    referrer_id=int(raw_referrer),
                )
                if rewarded:
                    await message.answer(success("Подарок за приглашение", "На ваш бонусный баланс начислено <b>50 бонусов</b> 🎁"))
    await send_home(message, state, user)


@router.message(F.text == "🏠 Главное меню")
async def home_menu(message: Message, state: FSMContext) -> None:
    await send_home(message, state, await ensure_user(message))


async def send_catalog(message: Message, *, edit: bool = False) -> None:
    async with SessionLocal() as session:
        products = await active_products(session)
    text = screen(
        "🛍",
        "Каталог",
        "Выберите товар — покажем описание и способы оплаты.",
        "Оплата: СБП • Telegram Stars",
    )
    if not products:
        text = screen(
            "🛍",
            "Каталог пока пуст",
            "Новые товары уже готовятся к появлению.",
            "Загляните немного позже",
        )
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
            screen(
                product.emoji,
                html.escape(product.title),
                f"{html.escape(product.description)}\n\n"
                f"💳 Стоимость: <b>{product_price(product)}</b>",
                "Выберите удобный способ оплаты",
            ),
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
    if provider not in {"rolly", "stars"}:
        await message.answer(warning("Оплата недоступна", "Выберите СБП или Telegram Stars."))
        return
    async with SessionLocal() as session:
        product = await get_product(session, product_id)
        if not product or not product.is_active:
            await message.answer(warning("Товар недоступен", "Вернитесь в каталог и выберите другую позицию."))
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
                await message.answer(warning("Stars недоступны", "Для этого товара цена в Stars ещё не настроена."))
                return
            order.payment_method = "telegram_stars"
            await session.commit()
            await record_payment_event(
                session,
                event_key=hashlib.sha256(f"created:stars:{order.id}".encode()).hexdigest(),
                provider="telegram_stars",
                order_id=order.id,
                event_status="created",
                result="accepted",
                amount=Decimal(product.price_stars),
                currency="XTR",
            )
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
            await message.answer(warning("СБП недоступна", "Для этого товара цена в рублях ещё не настроена."))
            return
        try:
            payment = await create_payment(order.id, Decimal(product.price_rub), f"{product.title} / заказ {order.id[:8]}", user_id)
            order.payment_method = "rollypay"
            order.provider_payment_id = str(payment.get("payment_id", ""))
            pay_url = payment["pay_url"]
            await session.commit()
            await record_payment_event(
                session,
                event_key=hashlib.sha256(f"created:rollypay:{order.id}".encode()).hexdigest(),
                provider="rollypay",
                order_id=order.id,
                provider_payment_id=order.provider_payment_id,
                event_status="created",
                result="accepted",
                amount=Decimal(product.price_rub),
                currency="RUB",
            )
        except (RollyPayError, KeyError):
            logger.exception("Payment creation failed for order %s", order.id)
            await message.answer(warning("Не удалось создать платёж", "Попробуйте ещё раз через минуту."))
            return

    await message.answer(
        screen(
            "🧾",
            "Заказ создан",
            f"🛍 {html.escape(product.title)}\n"
            f"💳 К оплате: <b>{product.price_rub} ₽</b>\n"
            f"🔖 Номер: <code>{order.id[:8]}</code>",
            "После оплаты нажмите «Проверить платёж»",
        ),
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
        await message.answer(warning("Нужно больше деталей", "Напишите хотя бы 5 символов."))
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
            else:
                changed = False
            if changed:
                await callback.message.answer(success("Платёж подтверждён", "Заказ оплачен и принят в работу."))
                await notify_order_paid(callback.bot, order, notify_customer=False)
                await callback.answer("Оплачено", show_alert=True)
                return
        except (RollyPayError, ValueError, ArithmeticError):
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
    charge_id = payment.telegram_payment_charge_id
    event_key = hashlib.sha256(f"stars:{charge_id}".encode()).hexdigest()
    async with SessionLocal() as session:
        expected = await session.get(Order, order_id)
        if not expected or expected.user_id != message.from_user.id or expected.amount_stars != payment.total_amount or expected.payment_method != "telegram_stars":
            logger.error("Rejected mismatched Stars payment for order %s", order_id)
            await record_payment_event(
                session,
                event_key=event_key,
                provider="telegram_stars",
                order_id=order_id,
                provider_payment_id=charge_id,
                event_status="paid",
                result="rejected",
                reason="Payment does not match order",
                amount=Decimal(payment.total_amount),
                currency="XTR",
            )
            return
        order, changed = await mark_order_paid(session, order_id, payment_method="telegram_stars", provider_payment_id=charge_id)
        await record_payment_event(
            session,
            event_key=event_key,
            provider="telegram_stars",
            order_id=order_id,
            provider_payment_id=charge_id,
            event_status="paid",
            result="accepted" if changed else "duplicate",
            amount=Decimal(payment.total_amount),
            currency="XTR",
        )
    if order:
        await message.answer(
            success(
                "Оплата получена",
                f"Заказ: <code>{order.id[:8]}</code>\n\nМы уже начали обработку покупки.",
            )
        )
        if changed:
            await notify_order_paid(bot, order, notify_customer=False)


@router.message(F.text.in_({"👤 Профиль", "💰 Баланс"}))
async def profile(message: Message) -> None:
    user = await ensure_user(message)
    async with SessionLocal() as session:
        bonus = await get_bonus_account(session, user.telegram_id)
    username = f"@{html.escape(user.username)}" if user.username else "не указан"
    await message.answer(
        screen(
            "👤",
            "Профиль LIMYZINOV",
            f"💎 <b>{html.escape(user.full_name)}</b>\n"
            f"🔗 {username}\n"
            f"🆔 <code>{user.telegram_id}</code>\n\n"
            f"🛍 Покупок: <b>{user.purchases_count}</b>\n"
            f"🎁 Бонусов: <b>{bonus.balance}</b>\n"
            f"📅 С нами с: <b>{user.created_at:%d.%m.%Y}</b>",
            "История покупок доступна в разделе «Заказы»",
        )
    )


async def send_bonus_screen(message: Message, user_id: int) -> None:
    async with SessionLocal() as session:
        account = await get_bonus_account(session, user_id)
        invited = (
            await session.execute(select(func.count()).select_from(BonusAccount).where(BonusAccount.referred_by == user_id))
        ).scalar_one()
    await message.answer(
        screen(
            "🎁",
            "Бонусный клуб",
            f"💰 Баланс: <b>{account.balance} бонусов</b>\n"
            f"👥 Приглашено друзей: <b>{invited}</b>\n\n"
            "За каждого нового друга вы получите <b>100 бонусов</b>, "
            "а друг — <b>50 бонусов</b>.",
            "Промокод можно активировать только один раз",
        ),
        reply_markup=bonus_keyboard(),
    )


@router.message(F.text == "🎁 Бонусы")
async def bonuses(message: Message) -> None:
    await ensure_user(message)
    await send_bonus_screen(message, message.from_user.id)


@router.callback_query(F.data.startswith("bonus:"))
async def bonus_callbacks(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    action = callback.data.split(":", 1)[1]
    if action == "promo":
        await state.set_state(PromoUserForm.code)
        await callback.message.answer(
            screen("🎟", "Активация промокода", "Отправьте промокод одним сообщением."),
            reply_markup=bonus_cancel_keyboard(),
        )
    elif action == "referral":
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{callback.from_user.id}"
        await callback.message.answer(
            screen(
                "👥",
                "Пригласить друга",
                f"Ваша персональная ссылка:\n<code>{html.escape(link)}</code>\n\n"
                "Вы получите <b>100 бонусов</b>, друг — <b>50 бонусов</b>.",
                "Награда начисляется за нового пользователя",
            )
        )
    elif action == "history":
        async with SessionLocal() as session:
            items = await recent_bonus_transactions(session, callback.from_user.id)
        labels = {"promo": "🎟 Промокод", "referral_join": "🎁 Вход по приглашению", "referral_invite": "👥 Приглашённый друг"}
        body = "\n".join(
            f"{labels.get(item.reason, '🎁 Бонусы')}: <b>{item.amount:+d}</b> · {item.created_at:%d.%m.%Y}"
            for item in items
        ) or "Операций пока нет."
        await callback.message.answer(screen("📜", "История бонусов", body))
    elif action == "cancel":
        await state.clear()
        await send_bonus_screen(callback.message, callback.from_user.id)
    await callback.answer()


@router.message(PromoUserForm.code)
async def redeem_promo(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer(warning("Нужен текстовый код", "Введите промокод буквами и цифрами."), reply_markup=bonus_cancel_keyboard())
        return
    async with SessionLocal() as session:
        status, amount = await redeem_promo_code(session, user_id=message.from_user.id, code=message.text)
    messages = {
        "not_found": warning("Промокод не найден", "Проверьте написание и попробуйте ещё раз."),
        "inactive": warning("Промокод выключен", "Этот промокод больше не действует."),
        "already_used": warning("Уже использован", "Один промокод можно активировать только один раз."),
        "limit_reached": warning("Активации закончились", "Лимит этого промокода исчерпан."),
    }
    if status != "ok":
        await message.answer(messages[status], reply_markup=bonus_cancel_keyboard())
        return
    await state.clear()
    await message.answer(success("Промокод активирован", f"Начислено <b>{amount} бонусов</b> 🎁"))
    await send_bonus_screen(message, message.from_user.id)


@router.message(F.text.in_({"📦 Заказы", "📦 Мои покупки"}))
async def my_orders(message: Message) -> None:
    await ensure_user(message)
    async with SessionLocal() as session:
        orders = await recent_orders(session, message.from_user.id, 10)
    if not orders:
        await message.answer(
            screen("📦", "Заказов пока нет", "Выберите первый товар в каталоге.", "Ваши покупки появятся здесь")
        )
        return
    rows = []
    for index, order in enumerate(orders, 1):
        amount = f"{order.amount_stars} ⭐" if order.amount_stars else f"{money(order.amount_rub or Decimal(0))} ₽"
        rows.append(
            f"<b>{index}. {html.escape(order.title)}</b>\n"
            f"{ORDER_STATUS_LABELS.get(order.status, order.status)} · {amount}\n"
            f"🔖 <code>{order.id[:8]}</code>"
        )
    await message.answer(screen("📦", "Ваши заказы", "\n\n".join(rows), "Показываем последние 10 заказов"))


@router.message(F.text.in_({"💬 Поддержка", "🆘 Поддержка"}))
@router.message(Command("paysupport"))
async def support(message: Message, state: FSMContext) -> None:
    await ensure_user(message)
    await state.set_state(SupportUserForm.content)
    await message.answer(
        screen(
            "💬",
            "Поддержка",
            "Опишите вопрос одним сообщением. Можно прикрепить:\n\n"
            "📝 текст   🖼 фото   🎬 видео\n"
            "📎 документ   🎙 голосовое",
            "Сообщение сразу получит владелец магазина",
        ),
        reply_markup=support_cancel_keyboard(),
    )


@router.callback_query(F.data == "support:cancel")
async def support_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer(
        success("Готово", "Обращение отменено."),
        reply_markup=main_keyboard(is_admin(callback.from_user.id)),
    )
    await callback.answer()


@router.message(SupportUserForm.content)
async def support_receive(message: Message, state: FSMContext) -> None:
    if message.content_type not in SUPPORT_CONTENT_TYPES:
        await message.answer(
            warning("Формат не поддерживается", "Отправьте текст, фото, видео, документ или голосовое."),
            reply_markup=support_cancel_keyboard(),
        )
        return
    if message.content_type == ContentType.TEXT and not support_message_body(message):
        await message.answer(warning("Пустое сообщение", "Напишите вопрос или прикрепите файл."), reply_markup=support_cancel_keyboard())
        return

    async with SessionLocal() as session:
        user = await get_or_create_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )
        if await support_rate_limited(session, user.telegram_id):
            await message.answer(warning("Слишком быстро", "Подождите 10 секунд и повторите отправку."))
            return
        ticket = await get_active_support_ticket(session, user.telegram_id)
        if ticket is None:
            ticket = await create_support_ticket(
                session,
                user_id=user.telegram_id,
                username=user.username,
                full_name=user.full_name,
            )
        saved = await add_support_message(
            session,
            ticket=ticket,
            sender="user",
            content_type=support_content_type(message),
            body=support_message_body(message),
            source_message_id=message.message_id,
        )

    try:
        await message.bot.send_message(OWNER_ADMIN_ID, support_ticket_text(ticket))
        delivered = await message.copy_to(
            OWNER_ADMIN_ID,
            reply_markup=support_ticket_keyboard(ticket),
        )
        async with SessionLocal() as session:
            stored = await session.get(SupportMessage, saved.id)
            if stored:
                stored.delivered_message_id = delivered.message_id
                await session.commit()
    except TelegramAPIError:
        logger.exception("Could not deliver support ticket %s to owner", ticket.id)

    await state.clear()
    await message.answer(
        success(
            "Сообщение отправлено",
            f"Номер обращения: <code>#{ticket.id}</code>\n\nПоддержка скоро ответит прямо в этом чате.",
        ),
        reply_markup=main_keyboard(is_admin(message.from_user.id)),
    )


async def admin_home(target: Message) -> None:
    async with SessionLocal() as session:
        users_count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        orders_count = (await session.execute(select(func.count()).select_from(Order))).scalar_one()
        paid_count = (await session.execute(select(func.count()).select_from(Order).where(Order.status == "paid"))).scalar_one()
        products_count = (await session.execute(select(func.count()).select_from(Product))).scalar_one()
        support_count = (
            await session.execute(
                select(func.count()).select_from(SupportTicket).where(SupportTicket.status == SupportStatus.NEW.value)
            )
        ).scalar_one()
    text = screen(
        "⚙️",
        "Панель управления",
        f"📦 Товаров: <b>{products_count}</b>\n"
        f"👥 Клиентов: <b>{users_count}</b>\n"
        f"🧾 Заказов: <b>{orders_count}</b>\n"
        f"💳 Оплачено: <b>{paid_count}</b>\n"
        f"💬 Новых обращений: <b>{support_count}</b>",
        "Выберите раздел",
    )
    await target.answer(text, reply_markup=admin_keyboard())


def sales_line(label: str, stats: dict[str, object]) -> str:
    return (
        f"{label}: <b>{stats['orders']}</b> заказов · "
        f"<b>{money(stats['rub'])} ₽</b> · <b>{stats['stars']} ⭐</b>"
    )


async def send_admin_analytics(target: Message) -> None:
    async with SessionLocal() as session:
        data = await get_shop_analytics(session)
    popular = data["popular"]
    popular_text = "\n".join(
        f"{index}. {html.escape(title)} — <b>{sales}</b>"
        for index, (title, sales) in enumerate(popular, 1)
    ) or "Продаж пока нет."
    await target.answer(
        screen(
            "📊",
            "Аналитика магазина",
            f"👥 Пользователей: <b>{data['users']}</b>\n"
            f"🛍 Покупателей: <b>{data['paid_buyers']}</b>\n"
            f"🎯 Конверсия в покупку: <b>{data['conversion']:.1f}%</b>\n\n"
            f"{sales_line('За 24 часа', data['day'])}\n"
            f"{sales_line('За 7 дней', data['week'])}\n"
            f"{sales_line('За 30 дней', data['month'])}\n\n"
            f"<b>🔥 Популярные товары</b>\n{popular_text}",
            "Рубли и Telegram Stars считаются отдельно",
        )
    )


async def send_payment_logs(target: Message) -> None:
    async with SessionLocal() as session:
        events = await recent_payment_events(session)
    result_icons = {"accepted": "✅", "duplicate": "🔁", "rejected": "⛔"}
    rows = []
    for event in events:
        order = event.order_id[:8] if event.order_id else "—"
        rows.append(
            f"{result_icons.get(event.result, '•')} <b>{html.escape(event.provider)}</b> · {html.escape(event.event_status)}\n"
            f"Заказ <code>{order}</code> · доставок: <b>{event.delivery_count}</b> · {event.last_seen_at:%d.%m %H:%M}"
        )
    await target.answer(
        screen("🧾", "Журнал платежей", "\n\n".join(rows) if rows else "Событий пока нет.", "Последние 20 событий")
    )


@router.message(Command("id"))
async def show_id(message: Message) -> None:
    await message.answer(screen("🪪", "Ваш Telegram ID", f"<code>{message.from_user.id}</code>"))


@router.message(Command("admin"))
@router.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not is_admin(message.from_user.id):
        await message.answer(warning("Доступ закрыт", "Админ-панель доступна только владельцу магазина."))
        return
    await admin_home(message)


async def finish_admin_product(target: Message, state: FSMContext, kind: str) -> Product | None:
    data = await state.get_data()
    required = {"title", "description", "price_rub", "price_stars"}
    if kind not in {"physical", "digital"} or not required.issubset(data):
        await state.clear()
        await target.answer(warning("Создание прервано", "Данные устарели. Начните создание товара заново."))
        return None
    async with SessionLocal() as session:
        product = Product(
            key=f"item-{uuid4().hex[:10]}",
            title=data["title"][:255],
            description=data["description"],
            price_rub=data["price_rub"],
            price_stars=data["price_stars"],
            emoji="📦" if kind == "physical" else "💾",
            kind=kind,
            requires_brief=False,
        )
        session.add(product)
        await session.commit()
        await session.refresh(product)
    await state.clear()
    await target.answer(
        success(
            "Товар создан",
            f"{product.emoji} <b>{html.escape(product.title)}</b>\n"
            f"💳 {product_price(product)}",
        ),
        reply_markup=admin_product_keyboard(product),
    )
    return product


@router.callback_query(F.data.startswith("admin:"))
async def admin_callbacks(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    action = callback.data.split(":")
    if action[1] == "home":
        await admin_home(callback.message)
    elif action[1] == "cancel":
        await state.clear()
        await admin_home(callback.message)
        await callback.answer("Действие отменено")
        return
    elif action[1] == "products":
        async with SessionLocal() as session:
            products = await all_products(session)
        body = "Выберите товар для редактирования." if products else "Товаров пока нет — создайте первый."
        await callback.message.answer(
            screen("📦", "Управление товарами", body),
            reply_markup=admin_products_keyboard(products),
        )
    elif action[1] == "analytics":
        await send_admin_analytics(callback.message)
    elif action[1] == "payments":
        await send_payment_logs(callback.message)
    elif action[1] == "promos":
        async with SessionLocal() as session:
            promos = await list_promo_codes(session)
        await callback.message.answer(
            screen("🎟", "Промокоды", "Нажмите на промокод, чтобы включить или выключить его." if promos else "Промокодов пока нет."),
            reply_markup=admin_promos_keyboard(promos),
        )
    elif action[1] == "promo":
        operation = action[2] if len(action) > 2 else ""
        if operation == "add":
            await state.set_state(AdminPromoForm.value)
            await callback.message.answer(
                screen(
                    "➕",
                    "Новый промокод",
                    "Отправьте данные через слеш:\n\n<code>WELCOME / 100 / 50</code>\n"
                    "где 100 — бонусы, 50 — число активаций.",
                ),
                reply_markup=admin_cancel_keyboard(),
            )
        elif operation == "toggle" and len(action) > 3:
            async with SessionLocal() as session:
                promo = await session.get(PromoCode, int(action[3]))
                if promo:
                    promo.is_active = not promo.is_active
                    await session.commit()
                promos = await list_promo_codes(session)
            if promo is None:
                await callback.answer("Промокод не найден", show_alert=True)
                return
            await callback.message.answer(
                success("Статус изменён", f"Промокод <code>{promo.code}</code> {'включён' if promo.is_active else 'выключен'}."),
                reply_markup=admin_promos_keyboard(promos),
            )
    elif action[1] == "support":
        if not is_support_admin(callback.from_user.id):
            await callback.answer("Доступ только владельцу", show_alert=True)
            return
        scope = action[2] if len(action) > 2 else "new"
        status = {
            "new": SupportStatus.NEW.value,
            "closed": SupportStatus.CLOSED.value,
        }.get(scope)
        async with SessionLocal() as session:
            tickets = await list_support_tickets(session, status=status)
        titles = {
            "new": ("💬", "Новые обращения"),
            "all": ("🗂", "Все обращения"),
            "closed": ("✅", "Закрытые обращения"),
        }
        icon, title = titles.get(scope, titles["all"])
        text = screen(icon, title, f"Найдено: <b>{len(tickets)}</b>" if tickets else "Здесь пока пусто.")
        await callback.message.answer(text, reply_markup=support_tickets_keyboard(tickets, scope))
    elif action[1] == "product":
        async with SessionLocal() as session:
            product = await get_product(session, int(action[2]))
            if not product:
                await callback.answer("Товар не найден", show_alert=True)
                return
            await callback.message.answer(
                screen(
                    product.emoji,
                    html.escape(product.title),
                    f"{html.escape(product.description)}\n\n"
                    f"💳 Цена: <b>{product_price(product)}</b>\n"
                    f"👁 Статус: <b>{'показывается' if product.is_active else 'скрыт'}</b>",
                ),
                reply_markup=admin_product_keyboard(product),
            )
    elif action[1] == "toggle":
        async with SessionLocal() as session:
            product = await get_product(session, int(action[2]))
            if product:
                product.is_active = not product.is_active
                await session.commit()
                await callback.message.edit_reply_markup(reply_markup=admin_product_keyboard(product))
        await callback.answer("Статус изменён", show_alert=True)
        return
    elif action[1] == "add":
        await state.set_state(AdminAddForm.title)
        await callback.message.answer(
            screen("✨", "Новый товар · 1/4", "Введите короткое и понятное название."),
            reply_markup=admin_cancel_keyboard(),
        )
    elif action[1] == "kind":
        kind = action[2] if len(action) > 2 else ""
        await finish_admin_product(callback.message, state, kind)
    elif action[1] == "edit":
        field, product_id = action[2], int(action[3])
        await state.set_state(AdminEditForm.value)
        await state.update_data(field=field, product_id=product_id)
        prompts = {
            "rub": screen("⚡", "Цена СБП", "Введите сумму в рублях, например: <code>990</code>"),
            "stars": screen("⭐", "Цена Stars", "Введите количество звёзд, например: <code>350</code>"),
            "text": screen("✏️", "Название и описание", "Формат: <code>Название | Описание</code>"),
        }
        await callback.message.answer(prompts[field], reply_markup=admin_cancel_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("support:"))
async def support_admin_callbacks(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_support_admin(callback.from_user.id):
        await callback.answer("Доступ только владельцу", show_alert=True)
        return
    action = callback.data.split(":")
    if len(action) < 3:
        await callback.answer("Некорректная команда", show_alert=True)
        return
    operation = action[1]
    try:
        ticket_id = int(action[2])
    except ValueError:
        await callback.answer("Некорректный номер обращения", show_alert=True)
        return
    messages: list[SupportMessage] = []
    async with SessionLocal() as session:
        ticket = await session.get(SupportTicket, ticket_id)
        if ticket is None:
            await callback.answer("Обращение не найдено", show_alert=True)
            return
        if operation == "close":
            ticket = await set_support_ticket_status(session, ticket_id, SupportStatus.CLOSED)
        elif operation == "reopen":
            ticket = await set_support_ticket_status(session, ticket_id, SupportStatus.NEW)
        elif operation == "ticket":
            messages = await support_ticket_messages(session, ticket_id)

    if operation == "ticket":
        await callback.message.answer(
            support_ticket_text(ticket) + support_history_text(messages),
            reply_markup=support_ticket_keyboard(ticket),
        )
    elif operation == "reply":
        if ticket.status == SupportStatus.CLOSED.value:
            await callback.answer("Сначала откройте обращение", show_alert=True)
            return
        await state.set_state(SupportReplyForm.content)
        await state.update_data(ticket_id=ticket.id)
        await callback.message.answer(
            screen(
                "✉️",
                f"Ответ · обращение #{ticket.id}",
                "Отправьте текст или вложение. Покупателю уйдёт только следующее сообщение.",
                "Можно отменить действие кнопкой ниже",
            ),
            reply_markup=support_cancel_keyboard(),
        )
    elif operation in {"close", "reopen"}:
        await callback.message.answer(support_ticket_text(ticket), reply_markup=support_ticket_keyboard(ticket))
    else:
        await callback.answer("Неизвестное действие", show_alert=True)
        return
    await callback.answer()


@router.message(SupportReplyForm.content)
async def support_admin_reply(message: Message, state: FSMContext) -> None:
    if not is_support_admin(message.from_user.id):
        await state.clear()
        return
    if message.content_type not in SUPPORT_CONTENT_TYPES:
        await message.answer(
            warning("Формат не поддерживается", "Отправьте текст, фото, видео, документ или голосовое."),
            reply_markup=support_cancel_keyboard(),
        )
        return
    if message.content_type == ContentType.TEXT and not support_message_body(message):
        await message.answer(warning("Пустой ответ", "Напишите сообщение или прикрепите файл."), reply_markup=support_cancel_keyboard())
        return

    data = await state.get_data()
    ticket_id = int(data["ticket_id"])
    async with SessionLocal() as session:
        ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None:
        await state.clear()
        await message.answer(warning("Обращение не найдено", "Возможно, оно было удалено."))
        return
    if ticket.status == SupportStatus.CLOSED.value:
        await state.clear()
        await message.answer(warning("Обращение закрыто", "Сначала откройте его снова в админ-панели."))
        return

    try:
        await message.bot.send_message(
            ticket.user_id,
            screen(
                "💬",
                "Ответ поддержки",
                f"Обращение: <code>#{ticket.id}</code>",
                "LIMYZINOV SHOP",
            ),
            reply_markup=main_keyboard(False),
        )
        delivered = await message.copy_to(ticket.user_id)
    except TelegramAPIError as exc:
        await state.clear()
        logger.warning("Could not deliver support reply for ticket %s: %s", ticket.id, exc)
        await message.answer(
            warning("Ответ не доставлен", "Возможно, пользователь заблокировал бота."),
            reply_markup=support_ticket_keyboard(ticket),
        )
        return

    async with SessionLocal() as session:
        stored_ticket = await session.get(SupportTicket, ticket.id)
        if stored_ticket:
            await add_support_message(
                session,
                ticket=stored_ticket,
                sender="admin",
                content_type=support_content_type(message),
                body=support_message_body(message),
                source_message_id=message.message_id,
                delivered_message_id=delivered.message_id,
            )
            ticket = stored_ticket

    await state.clear()
    await message.answer(
        success("Ответ доставлен", f"Обращение: <code>#{ticket.id}</code>"),
        reply_markup=support_ticket_keyboard(ticket),
    )


@router.message(AdminAddForm.title)
async def admin_add_title(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id) or not message.text:
        return
    title = message.text.strip()
    if not title:
        await message.answer(warning("Нужно название", "Введите хотя бы один символ."), reply_markup=admin_cancel_keyboard())
        return
    await state.update_data(title=title[:255])
    await state.set_state(AdminAddForm.description)
    await message.answer(
        screen("✨", "Новый товар · 2/4", "Расскажите коротко, что получает покупатель."),
        reply_markup=admin_cancel_keyboard(),
    )


@router.message(AdminPromoForm.value)
async def admin_add_promo(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id) or not message.text:
        return
    try:
        code, raw_bonus, raw_limit = [part.strip() for part in message.text.split("/", 2)]
        bonus_amount, max_uses = int(raw_bonus), int(raw_limit)
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", code) or bonus_amount <= 0 or max_uses <= 0:
            raise ValueError
    except ValueError:
        await message.answer(
            warning("Проверьте формат", "Пример: <code>WELCOME / 100 / 50</code>. Код от 3 символов, числа больше нуля."),
            reply_markup=admin_cancel_keyboard(),
        )
        return
    async with SessionLocal() as session:
        promo = await create_promo_code(session, code=code, bonus_amount=bonus_amount, max_uses=max_uses)
        promos = await list_promo_codes(session)
    if promo is None:
        await message.answer(warning("Код уже существует", "Придумайте другой промокод."), reply_markup=admin_cancel_keyboard())
        return
    await state.clear()
    await message.answer(
        success(
            "Промокод создан",
            f"Код: <code>{promo.code}</code>\n🎁 Бонусов: <b>{promo.bonus_amount}</b>\n👥 Активаций: <b>{promo.max_uses}</b>",
        ),
        reply_markup=admin_promos_keyboard(promos),
    )


@router.message(AdminAddForm.description)
async def admin_add_description(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id) or not message.text:
        return
    description = message.text.strip()
    if not description:
        await message.answer(warning("Нужно описание", "Коротко опишите товар."), reply_markup=admin_cancel_keyboard())
        return
    await state.update_data(description=description[:4000])
    await state.set_state(AdminAddForm.prices)
    await message.answer(
        screen(
            "✨",
            "Новый товар · 3/4",
            "Введите обе цены через слеш:\n\n<code>990 / 350</code>\n"
            "⚡ сначала рубли   ⭐ затем Stars",
        ),
        reply_markup=admin_cancel_keyboard(),
    )


@router.message(AdminAddForm.prices)
async def admin_add_prices(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id) or not message.text:
        return
    try:
        rub, stars = [int(v.strip()) for v in message.text.split("/", 1)]
        if rub <= 0 or stars <= 0:
            raise ValueError
    except ValueError:
        await message.answer(
            warning("Проверьте цены", "Используйте формат <code>990 / 350</code>. Обе цены больше нуля."),
            reply_markup=admin_cancel_keyboard(),
        )
        return
    await state.update_data(price_rub=rub, price_stars=stars)
    await state.set_state(AdminAddForm.kind)
    await message.answer(
        screen("✨", "Новый товар · 4/4", "Выберите тип товара."),
        reply_markup=product_kind_keyboard(),
    )


@router.message(AdminAddForm.kind)
async def admin_add_kind(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id) or not message.text:
        return
    kind = message.text.strip().lower()
    if kind not in {"physical", "digital"}:
        await message.answer(warning("Выберите тип кнопкой", "Физический или цифровой товар."), reply_markup=product_kind_keyboard())
        return
    await finish_admin_product(message, state, kind)


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
            await message.answer(warning("Товар не найден", "Вернитесь в список товаров."))
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
            await message.answer(
                warning("Проверьте значение", "Для товара обязательны обе цены: СБП и Stars, обе больше нуля."),
                reply_markup=admin_cancel_keyboard(),
            )
            return
        await session.commit()
        await session.refresh(product)
    await state.clear()
    await message.answer(
        success("Товар обновлён", f"{product.emoji} <b>{html.escape(product.title)}</b>\n💳 {product_price(product)}"),
        reply_markup=admin_product_keyboard(product),
    )
