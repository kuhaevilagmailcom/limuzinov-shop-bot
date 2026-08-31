from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.config import get_settings
from app.db import Product, SupportStatus, SupportTicket


def main_keyboard(admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text="📦 Заказы")],
        [KeyboardButton(text="💎 Бонусы"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="💬 Поддержка")],
    ]
    if admin:
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, input_field_placeholder="Что хотите открыть?")


def product_price(product: Product) -> str:
    prices = []
    if product.price_rub:
        prices.append(f"{product.price_rub} ₽")
    if product.price_stars:
        prices.append(f"{product.price_stars} ⭐")
    return " / ".join(prices) or "по запросу"


def catalog_keyboard(products: list[Product]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{p.emoji} {p.title} · {product_price(p)}", callback_data=f"product:{p.id}")] for p in products])


def product_keyboard(product: Product) -> InlineKeyboardMarkup:
    settings = get_settings()
    rows = []
    if product.price_rub and settings.rollypay_enabled:
        rows.append([InlineKeyboardButton(text=f"⚡ Оплатить по СБП · {product.price_rub} ₽", callback_data=f"buy:rolly:{product.id}")])
    if product.price_stars:
        rows.append([InlineKeyboardButton(text=f"⭐ Оплатить Stars · {product.price_stars}", callback_data=f"buy:stars:{product.id}")])
    rows.append([InlineKeyboardButton(text="‹ Назад в каталог", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_url_keyboard(url: str, order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Открыть страницу оплаты", url=url)], [InlineKeyboardButton(text="🔄 Проверить платёж", callback_data=f"status:{order_id}")]])


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Товары", callback_data="admin:products")],
        [InlineKeyboardButton(text="✨ Создать товар", callback_data="admin:add")],
        [InlineKeyboardButton(text="🎁 Промокоды", callback_data="admin:promos")],
        [InlineKeyboardButton(text="💬 Новые обращения", callback_data="admin:support:new")],
    ])
