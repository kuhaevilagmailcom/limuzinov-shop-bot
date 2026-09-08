from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.config import get_settings
from app.db import Product, PromoCode, SupportStatus, SupportTicket


def main_keyboard(admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="Каталог"), KeyboardButton(text="Мои заказы")],
        [KeyboardButton(text="Профиль"), KeyboardButton(text="Бонусный клуб")],
        [KeyboardButton(text="Поддержка")],
    ]
    if admin:
        rows.append([KeyboardButton(text="Управление магазином")])
    rows.append([KeyboardButton(text="← Назад")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Что хотите открыть?",
    )


def product_price(product: Product) -> str:
    prices: list[str] = []
    if product.price_rub:
        prices.append(f"{product.price_rub} ₽")
    if product.price_stars:
        prices.append(f"{product.price_stars} ⭐")
    return " / ".join(prices) or "по запросу"


def catalog_keyboard(products: list[Product]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{product.title} · {product_price(product)}",
                callback_data=f"product:{product.id}",
            )
        ]
        for product in products
    ]
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_keyboard(product: Product) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if product.kind == "physical":
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="Доставка · +50 ₽ / +25 ⭐",
                        callback_data=f"fulfill:delivery:{product.id}:0",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Самовывоз · Гостиный Двор",
                        callback_data=f"fulfill:pickup:{product.id}:0",
                    )
                ],
            ]
        )
    else:
        settings = get_settings()
        if product.price_rub and settings.rollypay_enabled:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Оплатить по СБП · {product.price_rub} ₽",
                        callback_data=f"buy:rolly:{product.id}",
                    )
                ]
            )
        if product.price_stars:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Оплатить звёздами · {product.price_stars} ⭐",
                        callback_data=f"buy:stars:{product.id}",
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="← В каталог", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_url_keyboard(url: str, order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Перейти к оплате", url=url)],
            [
                InlineKeyboardButton(
                    text="Проверить платёж", callback_data=f"status:{order_id}"
                )
            ],
            [InlineKeyboardButton(text="← В каталог", callback_data="catalog")],
            [InlineKeyboardButton(text="Главное меню", callback_data="home")],
        ]
    )


def stars_invoice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить звёздами", pay=True)],
            [InlineKeyboardButton(text="Главное меню", callback_data="home")],
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Товары", callback_data="admin:products")],
            [InlineKeyboardButton(text="Создать товар", callback_data="admin:add")],
            [
                InlineKeyboardButton(text="Аналитика", callback_data="admin:analytics"),
                InlineKeyboardButton(text="Промокоды", callback_data="admin:promos"),
            ],
            [
                InlineKeyboardButton(
                    text="Журнал платежей", callback_data="admin:payments"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Новые обращения", callback_data="admin:support:new"
                )
            ],
            [
                InlineKeyboardButton(text="Все", callback_data="admin:support:all"),
                InlineKeyboardButton(
                    text="Закрытые", callback_data="admin:support:closed"
                ),
            ],
            [InlineKeyboardButton(text="Главное меню", callback_data="home")],
        ]
    )


def bonus_keyboard(
    *, daily_claimed: bool = False, next_reward: int = 10
) -> InlineKeyboardMarkup:
    daily_label = (
        f"Сегодня получено · завтра {next_reward}"
        if daily_claimed
        else f"Получить сегодня · {next_reward} бонусов"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть секретное предложение",
                    callback_data="bonus:secret",
                    icon_custom_emoji_id="5309958691854754293",
                    style="primary",
                )
            ],
            [InlineKeyboardButton(text=daily_label, callback_data="bonus:daily")],
            [
                InlineKeyboardButton(
                    text="Активировать промокод", callback_data="bonus:promo"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Пригласить друга", callback_data="bonus:referral"
                ),
                InlineKeyboardButton(text="История", callback_data="bonus:history"),
            ],
            [InlineKeyboardButton(text="Главное меню", callback_data="home")],
        ]
    )


def secret_offer_keyboard(
    offer_id: str,
    *,
    product_id: int,
    product_kind: str,
    price_rub: int,
    price_stars: int,
    available: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if available:
        if product_kind == "physical":
            rows.extend(
                [
                    [
                        InlineKeyboardButton(
                            text="Доставка · +50 ₽ / +25 ⭐",
                            callback_data=f"fulfill:delivery:{product_id}:{offer_id}",
                            style="primary",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Самовывоз · Гостиный Двор",
                            callback_data=f"fulfill:pickup:{product_id}:{offer_id}",
                        )
                    ],
                ]
            )
        else:
            rows.extend(
                [
                    [
                        InlineKeyboardButton(
                            text=f"Забрать по СБП · {price_rub} ₽",
                            callback_data=f"secret:buy:rolly:{offer_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=f"Забрать за {price_stars} ⭐",
                            callback_data=f"secret:buy:stars:{offer_id}",
                        )
                    ],
                ]
            )
    rows.append(
        [InlineKeyboardButton(text="← В бонусный клуб", callback_data="bonus:back")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fulfillment_cancel_keyboard(back_callback: str = "catalog") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data=back_callback)],
            [InlineKeyboardButton(text="Главное меню", callback_data="home")],
        ]
    )


def checkout_keyboard(
    *, amount_rub: int | None, amount_stars: int | None, back_callback: str
) -> InlineKeyboardMarkup:
    settings = get_settings()
    rows: list[list[InlineKeyboardButton]] = []
    if amount_rub and settings.rollypay_enabled:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Оплатить по СБП · {amount_rub} ₽",
                    callback_data="checkout:rolly",
                    icon_custom_emoji_id="5350452584119279096",
                    style="success",
                )
            ]
        )
    if amount_stars:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Оплатить звёздами · {amount_stars} ⭐",
                    callback_data="checkout:stars",
                    icon_custom_emoji_id="5309958691854754293",
                    style="success",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=back_callback)])
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bonus_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← В бонусный клуб", callback_data="bonus:cancel"
                )
            ]
        ]
    )


def home_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Главное меню",
                    callback_data="home",
                    icon_custom_emoji_id="5309958691854754293",
                    style="primary",
                )
            ]
        ]
    )


def bonus_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="← В бонусный клуб", callback_data="bonus:back"
                )
            ],
            [InlineKeyboardButton(text="Главное меню", callback_data="home")],
        ]
    )


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← В управление", callback_data="admin:home")],
            [InlineKeyboardButton(text="Главное меню", callback_data="home")],
        ]
    )


def admin_promos_keyboard(promos: list[PromoCode]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'Активен' if promo.is_active else 'Выключен'} · {promo.code} · {promo.bonus_amount} · {promo.used_count}/{promo.max_uses}",
                callback_data=f"admin:promo:toggle:{promo.id}",
            )
        ]
        for promo in promos
    ]
    rows.append(
        [InlineKeyboardButton(text="Создать промокод", callback_data="admin:promo:add")]
    )
    rows.append(
        [InlineKeyboardButton(text="← В управление", callback_data="admin:home")]
    )
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_products_keyboard(products: list[Product]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{p.title} · {product_price(p)}",
                callback_data=f"admin:product:{p.id}",
            )
        ]
        for p in products
    ]
    rows.append([InlineKeyboardButton(text="Создать товар", callback_data="admin:add")])
    rows.append(
        [InlineKeyboardButton(text="← В управление", callback_data="admin:home")]
    )
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_product_keyboard(product: Product) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚡ Цена СБП", callback_data=f"admin:edit:rub:{product.id}"
                ),
                InlineKeyboardButton(
                    text="⭐ Цена Stars", callback_data=f"admin:edit:stars:{product.id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Название и описание",
                    callback_data=f"admin:edit:text:{product.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🙈 Скрыть" if product.is_active else "👁 Показать",
                    callback_data=f"admin:toggle:{product.id}",
                )
            ],
            [InlineKeyboardButton(text="‹ К товарам", callback_data="admin:products")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
        ]
    )


def support_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="‹ Назад в главное меню", callback_data="support:cancel"
                )
            ]
        ]
    )


def support_ticket_keyboard(ticket: SupportTicket) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if ticket.status != SupportStatus.CLOSED.value:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✉️ Ответить", callback_data=f"support:reply:{ticket.id}"
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Закрыть обращение",
                    callback_data=f"support:close:{ticket.id}",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ Открыть снова", callback_data=f"support:reopen:{ticket.id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="‹ Назад к обращениям", callback_data="admin:support:all"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="‹ Назад в админ-панель", callback_data="admin:cancel"
                )
            ]
        ]
    )


def product_kind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Физический товар", callback_data="admin:kind:physical"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💾 Цифровой товар", callback_data="admin:kind:digital"
                )
            ],
            [InlineKeyboardButton(text="✖️ Отменить", callback_data="admin:cancel")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
        ]
    )


def support_tickets_keyboard(
    tickets: list[SupportTicket], scope: str
) -> InlineKeyboardMarkup:
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
    rows.append(
        [InlineKeyboardButton(text="‹ Админ-панель", callback_data="admin:home")]
    )
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
