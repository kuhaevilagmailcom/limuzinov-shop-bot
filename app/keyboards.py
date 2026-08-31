from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.config import get_settings
from app.db import Product


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text="📦 Мои покупки")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🆘 Поддержка")],
        ],
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
        rows.append([InlineKeyboardButton(text=f"⚡ СБП / карта · {product.price_rub} ₽", callback_data=f"buy:rolly:{product.id}")])
    if product.price_rub and settings.cryptopay_enabled:
        rows.append([InlineKeyboardButton(text=f"₿ Криптовалюта · {product.price_rub} ₽", callback_data=f"buy:crypto:{product.id}")])
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
