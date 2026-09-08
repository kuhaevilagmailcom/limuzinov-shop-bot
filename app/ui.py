DIVIDER = "—"


def screen(icon: str, title: str, body: str, footer: str | None = None) -> str:
    heading = f"{icon}  <b>{title}</b>" if icon else f"<b>{title}</b>"
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
