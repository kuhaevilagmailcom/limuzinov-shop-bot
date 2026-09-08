DIVIDER = "—"
PREMIUM_EMOJIS = {
    # News Emoji: https://t.me/addemoji/NewsEmoji
    "default": ("💎", "5427168083074628963"),
    "success": ("✔️", "5206607081334906820"),
    "warning": ("⚠️", "5447644880824181073"),
    "money": ("💵", "5409048419211682843"),
    "orders": ("🛍", "5229064374403998351"),
}


def premium_emoji(fallback: str) -> str:
    """Animated Telegram emoji with a normal Unicode fallback."""
    key = "default"
    if fallback in {"✅", "🔥"}:
        key = "success"
    elif fallback in {"⚠️", "❗", "❗️"}:
        key = "warning"
    elif fallback in {"💸", "💳", "💰"}:
        key = "money"
    elif fallback in {"📦", "🧾"}:
        key = "orders"
    emoji, emoji_id = PREMIUM_EMOJIS[key]
    return f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'


def screen(icon: str, title: str, body: str, footer: str | None = None) -> str:
    heading = f"{premium_emoji(icon)}  <b>{title}</b>" if icon else f"<b>{title}</b>"
    text = f"{heading}\n\n{body.strip()}"
    if footer:
        text += f"\n\n{DIVIDER}\n<i>{footer}</i>"
    return text


def success(title: str, body: str) -> str:
    return screen("✅", title, body)


def warning(title: str, body: str) -> str:
    return screen("⚠️", title, body)


ORDER_STATUS_LABELS = {
    "created": "Ожидает оплаты",
    "processing": "В обработке",
    "paid": "Оплачен",
    "canceled": "Отменён",
    "expired": "Срок оплаты истёк",
    "refunded": "Возврат",
    "chargeback": "Платёж отменён",
}
