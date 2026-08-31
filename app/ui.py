from __future__ import annotations


DIVIDER = "━━━━━━━━━━━━━━"


def screen(icon: str, title: str, body: str, footer: str | None = None) -> str:
    text = f"{icon} <b>{title}</b>\n{DIVIDER}\n{body.strip()}"
    if footer:
        text += f"\n\n<i>{footer}</i>"
    return text


def success(title: str, body: str) -> str:
    return screen("✅", title, body)


def warning(title: str, body: str) -> str:
    return screen("⚠️", title, body)


ORDER_STATUS_LABELS = {
    "created": "🕓 Ожидает оплаты",
    "processing": "⚡ Обрабатывается",
    "paid": "✅ Оплачен",
    "canceled": "✖️ Отменён",
    "expired": "⌛ Истёк",
    "refunded": "↩️ Возврат",
    "chargeback": "↩️ Платёж отменён",
}
