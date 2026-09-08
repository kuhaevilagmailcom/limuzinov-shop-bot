DIVIDER = "—"
PREMIUM_EMOJIS = {
    "default": ("💎", "5309958691854754293"),
    "success": ("🔥", "5312241539987020022"),
    "warning": ("❗️", "5379748062124056162"),
    "money": ("💰", "5350452584119279096"),
    "orders": ("📰", "5434144690511290129"),
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
