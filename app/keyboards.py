from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.catalog import PRODUCTS
from app.config import get_settings


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text="🎵 Заказать песню")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="📦 Мои покупки")],
            [KeyboardButton(text="🆘 Поддержка"), KeyboardButton(text="🏠 Главное меню")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…",
    )


def catalog_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for product in PRODUCTS.values():
        rows.append([
            InlineKeyboardButton(
                text=f"{product.emoji} {product.title} — {product.price_rub} ₽",
                callback_data=f"product:{product.key}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_keyboard(product_key: str) -> InlineKeyboardMarkup:
    settings = get_settings()
    rows: list[list[InlineKeyboardButton]] = []
    if settings.rollypay_enabled:
        rows.append([InlineKeyboardButton(text="⚡ СБП / карта", callback_data=f"pay:rolly:{product_key}")])
    if settings.cryptopay_enabled:
        rows.append([InlineKeyboardButton(text="₿ Криптовалюта", callback_data=f"pay:crypto:{product_key}")])
    rows.append([InlineKeyboardButton(text="← Вернуться в каталог", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_url_keyboard(url: str, order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💸 Перейти к оплате", url=url)],
            [InlineKeyboardButton(text="🔄 Проверить статус", callback_data=f"status:{order_id}")],
        ]
    )
