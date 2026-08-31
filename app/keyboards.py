from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.config import get_settings
from app.db import Product, SupportStatus, SupportTicket


def main_keyboard(admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text="📦 Мои покупки")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🆘 Поддержка")],
    ]
    if admin:
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…",
    )


def product_price(product: Product) -> str:
    prices: list[str] = []
    if product.price_rub:
        prices.append(f"{product.price_rub} ₽")
    if product.price_stars:
        prices.append(f"{product.price_stars} ⭐")
    return " / ".join(prices) or "по запросу"


def catalog_keyboard(products: list[Product]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{product.emoji} {product.title} · {product_price(product)}",
                    callback_data=f"product:{product.id}",
                )
            ]
            for product in products
        ]
    )


def product_keyboard(product: Product) -> InlineKeyboardMarkup:
    settings = get_settings()
    rows: list[list[InlineKeyboardButton]] = []
    if product.price_stars:
        rows.append([InlineKeyboardButton(text=f"⭐ Telegram Stars · {product.price_stars}", callback_data=f"buy:stars:{product.id}")])
    if product.price_rub and settings.rollypay_enabled:
        rows.append([InlineKeyboardButton(text=f"⚡ СБП · {product.price_rub} ₽", callback_data=f"buy:rolly:{product.id}")])
    rows.append([InlineKeyboardButton(text="← Вернуться в каталог", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_url_keyboard(url: str, order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💸 Перейти к оплате", url=url)],
            [InlineKeyboardButton(text="🔄 Проверить статус", callback_data=f"status:{order_id}")],
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Управление товарами", callback_data="admin:products")],
            [InlineKeyboardButton(text="➕ Создать товар", callback_data="admin:add")],
            [InlineKeyboardButton(text="🆘 Новые обращения", callback_data="admin:support:new")],
            [
                InlineKeyboardButton(text="📂 Все обращения", callback_data="admin:support:all"),
                InlineKeyboardButton(text="✅ Закрытые", callback_data="admin:support:closed"),
            ],
        ]
    )


def admin_products_keyboard(products: list[Product]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{p.emoji} {p.title} · {product_price(p)}", callback_data=f"admin:product:{p.id}")]
        for p in products
    ]
    rows.append([InlineKeyboardButton(text="➕ Создать товар", callback_data="admin:add")])
    rows.append([InlineKeyboardButton(text="← Админ-панель", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_product_keyboard(product: Product) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💵 Цена ₽", callback_data=f"admin:edit:rub:{product.id}"),
                InlineKeyboardButton(text="⭐ Цена Stars", callback_data=f"admin:edit:stars:{product.id}"),
            ],
            [InlineKeyboardButton(text="📝 Название и описание", callback_data=f"admin:edit:text:{product.id}")],
            [InlineKeyboardButton(text="🙈 Скрыть" if product.is_active else "👁 Показать", callback_data=f"admin:toggle:{product.id}")],
            [InlineKeyboardButton(text="← К товарам", callback_data="admin:products")],
        ]
    )


def support_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отменить обращение", callback_data="support:cancel")]]
    )


def support_ticket_keyboard(ticket: SupportTicket) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if ticket.status != SupportStatus.CLOSED.value:
        rows.append([InlineKeyboardButton(text="✉️ Ответить", callback_data=f"support:reply:{ticket.id}")])
        rows.append([InlineKeyboardButton(text="✅ Закрыть обращение", callback_data=f"support:close:{ticket.id}")])
    else:
        rows.append([InlineKeyboardButton(text="↩️ Открыть снова", callback_data=f"support:reopen:{ticket.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def support_tickets_keyboard(tickets: list[SupportTicket], scope: str) -> InlineKeyboardMarkup:
    status_icons = {
        SupportStatus.NEW.value: "🆘",
        SupportStatus.ANSWERED.value: "💬",
        SupportStatus.CLOSED.value: "✅",
    }
    rows = [
        [
            InlineKeyboardButton(
                text=f"{status_icons.get(ticket.status, '•')} #{ticket.id} · {(ticket.full_name or str(ticket.user_id))[:24]}",
                callback_data=f"support:ticket:{ticket.id}:{scope}",
            )
        ]
        for ticket in tickets
    ]
    rows.append([InlineKeyboardButton(text="← Админ-панель", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
