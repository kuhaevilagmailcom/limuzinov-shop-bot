from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.config import get_settings
from app.db import Product, PromoCode, SupportStatus, SupportTicket

_TelegramInlineKeyboardMarkup = InlineKeyboardMarkup
_TelegramReplyKeyboardMarkup = ReplyKeyboardMarkup


def InlineKeyboardMarkup(
    *, inline_keyboard: list[list[InlineKeyboardButton]]
) -> _TelegramInlineKeyboardMarkup:
    """Applies the shop's green style to every inline button."""
    colored_rows = [
        [button.model_copy(update={"style": "success"}) for button in row]
        for row in inline_keyboard
    ]
    return _TelegramInlineKeyboardMarkup(inline_keyboard=colored_rows)


def ReplyKeyboardMarkup(
    *, keyboard: list[list[KeyboardButton]], **kwargs
) -> _TelegramReplyKeyboardMarkup:
    """Applies the shop's green style to the persistent bottom menu."""
    colored_rows = [
        [button.model_copy(update={"style": "success"}) for button in row]
        for row in keyboard
    ]
    return _TelegramReplyKeyboardMarkup(keyboard=colored_rows, **kwargs)


NEWS_EMOJI = {
    "catalog": "5229064374403998351",  # 🛍
    "orders": "5222444124698853913",  # 🔖
    "profile": "5461117441612462242",  # 🙂
    "bonus": "5427168083074628963",  # 💎
    "support": "5443038326535759644",  # 💬
    "admin": "5341715473882955310",  # ⚙️
    "home": "5416041192905265756",  # 🏠
    "back": "5416117059207572332",  # ➡️
    "pay": "5409048419211682843",  # 💵
    "stars": "5438496463044752972",  # ⭐️
    "delivery": "5391032818111363540",  # 📍
    "gift": "5461151367559141950",  # 🎉
    "check": "5206607081334906820",  # ✔️
    "refresh": "5375338737028841420",  # 🔄
    "promo": "5341498088408234504",  # 💯
    "invite": "5271604874419647061",  # 🔗
    "history": "5231200819986047254",  # 📊
    "add": "5397916757333654639",  # ➕
    "edit": "5395444784611480792",  # ✏️
    "view": "5210956306952758910",  # 👀
    "delete": "5445267414562389170",  # 🗑
}


def home_button(text: str = "Главное меню") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data="home",
        icon_custom_emoji_id=NEWS_EMOJI["home"],
    )


def main_keyboard(admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [
            KeyboardButton(
                text="Каталог",
                icon_custom_emoji_id=NEWS_EMOJI["catalog"],
                style="primary",
            ),
            KeyboardButton(
                text="Мои заказы", icon_custom_emoji_id=NEWS_EMOJI["orders"]
            ),
        ],
        [
            KeyboardButton(text="Профиль", icon_custom_emoji_id=NEWS_EMOJI["profile"]),
            KeyboardButton(text="Бонусы", icon_custom_emoji_id=NEWS_EMOJI["bonus"]),
        ],
        [KeyboardButton(text="Поддержка", icon_custom_emoji_id=NEWS_EMOJI["support"])],
    ]
    if admin:
        rows.append(
            [
                KeyboardButton(
                    text="Управление магазином",
                    icon_custom_emoji_id=NEWS_EMOJI["admin"],
                )
            ]
        )
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
                icon_custom_emoji_id=NEWS_EMOJI["catalog"],
            )
        ]
        for product in products
    ]
    rows.append([home_button()])
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
                        icon_custom_emoji_id=NEWS_EMOJI["delivery"],
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Самовывоз · Гостиный Двор",
                        callback_data=f"fulfill:pickup:{product.id}:0",
                        icon_custom_emoji_id=NEWS_EMOJI["home"],
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
                        icon_custom_emoji_id=NEWS_EMOJI["pay"],
                        style="success",
                    )
                ]
            )
        if product.price_stars:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Оплатить звёздами · {product.price_stars} ⭐",
                        callback_data=f"buy:stars:{product.id}",
                        icon_custom_emoji_id=NEWS_EMOJI["stars"],
                        style="success",
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="← В каталог", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_url_keyboard(url: str, order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Перейти к оплате",
                    url=url,
                    icon_custom_emoji_id=NEWS_EMOJI["pay"],
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Проверить платёж",
                    callback_data=f"status:{order_id}",
                    icon_custom_emoji_id=NEWS_EMOJI["refresh"],
                )
            ],
            [InlineKeyboardButton(text="← В каталог", callback_data="catalog")],
            [home_button()],
        ]
    )


def stars_invoice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Оплатить звёздами",
                    pay=True,
                    icon_custom_emoji_id=NEWS_EMOJI["stars"],
                    style="success",
                )
            ],
            [home_button()],
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Товары",
                    callback_data="admin:products",
                    icon_custom_emoji_id=NEWS_EMOJI["catalog"],
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Создать товар",
                    callback_data="admin:add",
                    icon_custom_emoji_id=NEWS_EMOJI["add"],
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Аналитика",
                    callback_data="admin:analytics",
                    icon_custom_emoji_id=NEWS_EMOJI["history"],
                ),
                InlineKeyboardButton(
                    text="Промокоды",
                    callback_data="admin:promos",
                    icon_custom_emoji_id=NEWS_EMOJI["promo"],
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Журнал платежей",
                    callback_data="admin:payments",
                    icon_custom_emoji_id=NEWS_EMOJI["pay"],
                )
            ],
            [
                InlineKeyboardButton(
                    text="Новые обращения",
                    callback_data="admin:support:new",
                    icon_custom_emoji_id=NEWS_EMOJI["support"],
                )
            ],
            [
                InlineKeyboardButton(
                    text="Все",
                    callback_data="admin:support:all",
                    icon_custom_emoji_id=NEWS_EMOJI["view"],
                ),
                InlineKeyboardButton(
                    text="Закрытые",
                    callback_data="admin:support:closed",
                    icon_custom_emoji_id=NEWS_EMOJI["check"],
                ),
            ],
            [home_button()],
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
                    icon_custom_emoji_id=NEWS_EMOJI["gift"],
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text=daily_label,
                    callback_data="bonus:daily",
                    icon_custom_emoji_id=NEWS_EMOJI["check"],
                    style="success" if not daily_claimed else None,
                )
            ],
            [
                InlineKeyboardButton(
                    text="Активировать промокод",
                    callback_data="bonus:promo",
                    icon_custom_emoji_id=NEWS_EMOJI["promo"],
                )
            ],
            [
                InlineKeyboardButton(
                    text="Пригласить друга",
                    callback_data="bonus:referral",
                    icon_custom_emoji_id=NEWS_EMOJI["invite"],
                ),
                InlineKeyboardButton(
                    text="История",
                    callback_data="bonus:history",
                    icon_custom_emoji_id=NEWS_EMOJI["history"],
                ),
            ],
            [home_button()],
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
                            icon_custom_emoji_id=NEWS_EMOJI["delivery"],
                            style="primary",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Самовывоз · Гостиный Двор",
                            callback_data=f"fulfill:pickup:{product_id}:{offer_id}",
                            icon_custom_emoji_id=NEWS_EMOJI["home"],
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
                            icon_custom_emoji_id=NEWS_EMOJI["pay"],
                            style="success",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=f"Забрать за {price_stars} ⭐",
                            callback_data=f"secret:buy:stars:{offer_id}",
                            icon_custom_emoji_id=NEWS_EMOJI["stars"],
                            style="success",
                        )
                    ],
                ]
            )
    rows.append([InlineKeyboardButton(text="← В бонусы", callback_data="bonus:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fulfillment_cancel_keyboard(back_callback: str = "catalog") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data=back_callback)],
            [home_button()],
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
                    icon_custom_emoji_id=NEWS_EMOJI["pay"],
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
                    icon_custom_emoji_id=NEWS_EMOJI["stars"],
                    style="success",
                )
            ]
        )
    if amount_rub:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Наличными при получении · {amount_rub} ₽",
                    callback_data="checkout:cash",
                    icon_custom_emoji_id=NEWS_EMOJI["pay"],
                    style="primary",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=back_callback)])
    rows.append([home_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bonus_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← В бонусы", callback_data="bonus:cancel")]
        ]
    )


def home_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Главное меню",
                    callback_data="home",
                    icon_custom_emoji_id=NEWS_EMOJI["home"],
                    style="primary",
                )
            ]
        ]
    )


def bonus_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← В бонусы", callback_data="bonus:back")],
            [home_button()],
        ]
    )


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← В управление", callback_data="admin:home")],
            [home_button()],
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
        [
            InlineKeyboardButton(
                text="Создать промокод",
                callback_data="admin:promo:add",
                icon_custom_emoji_id=NEWS_EMOJI["add"],
                style="success",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="← В управление", callback_data="admin:home")]
    )
    rows.append([home_button()])
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
    rows.append(
        [
            InlineKeyboardButton(
                text="Создать товар",
                callback_data="admin:add",
                icon_custom_emoji_id=NEWS_EMOJI["add"],
                style="success",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="← В управление", callback_data="admin:home")]
    )
    rows.append([home_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_product_keyboard(product: Product) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Цена СБП",
                    callback_data=f"admin:edit:rub:{product.id}",
                    icon_custom_emoji_id=NEWS_EMOJI["pay"],
                ),
                InlineKeyboardButton(
                    text="Цена Stars",
                    callback_data=f"admin:edit:stars:{product.id}",
                    icon_custom_emoji_id=NEWS_EMOJI["stars"],
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Название и описание",
                    callback_data=f"admin:edit:text:{product.id}",
                    icon_custom_emoji_id=NEWS_EMOJI["edit"],
                )
            ],
            [
                InlineKeyboardButton(
                    text="Скрыть" if product.is_active else "Показать",
                    callback_data=f"admin:toggle:{product.id}",
                    icon_custom_emoji_id=NEWS_EMOJI["view"],
                    style="danger" if product.is_active else "success",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Удалить товар",
                    callback_data=f"admin:delete:{product.id}",
                    icon_custom_emoji_id=NEWS_EMOJI["delete"],
                )
            ],
            [InlineKeyboardButton(text="‹ К товарам", callback_data="admin:products")],
            [home_button()],
        ]
    )


def admin_delete_product_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить навсегда",
                    callback_data=f"admin:delete_confirm:{product_id}",
                    icon_custom_emoji_id=NEWS_EMOJI["delete"],
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена", callback_data=f"admin:product:{product_id}"
                )
            ],
            [home_button()],
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
                    text="Ответить",
                    callback_data=f"support:reply:{ticket.id}",
                    icon_custom_emoji_id=NEWS_EMOJI["support"],
                    style="primary",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Закрыть обращение",
                    callback_data=f"support:close:{ticket.id}",
                    icon_custom_emoji_id=NEWS_EMOJI["check"],
                    style="danger",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Открыть снова",
                    callback_data=f"support:reopen:{ticket.id}",
                    icon_custom_emoji_id=NEWS_EMOJI["refresh"],
                    style="success",
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
    rows.append([home_button()])
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
            [home_button()],
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
    rows.append([home_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)
