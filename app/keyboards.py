from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.catalog import PRODUCTS


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text="🎵 Заказать песню")],
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="📦 Мои покупки")],
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
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 RollyPay", callback_data=f"pay:rolly:{product_key}")],
            [InlineKeyboardButton(text="₿ CryptoBot", callback_data=f"pay:crypto:{product_key}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog")],
        ]
    )


def payment_url_keyboard(url: str, order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💸 Перейти к оплате", url=url)],
            [InlineKeyboardButton(text="🔄 Проверить статус", callback_data=f"status:{order_id}")],
        ]
    )
