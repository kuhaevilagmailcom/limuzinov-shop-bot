from __future__ import annotations


def format_stats(*, users: int, today: str, week: str, month: str, top_products: list[str], conversion: str) -> str:
    products = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(top_products)) or "Нет данных"
    return (
        "📊 <b>Аналитика LIMYZINOV</b>\n\n"
        f"👥 Пользователи: <b>{users}</b>\n\n"
        f"💰 Сегодня: <b>{today}</b>\n"
        f"📅 Неделя: <b>{week}</b>\n"
        f"🗓 Месяц: <b>{month}</b>\n\n"
        f"🔥 Популярные товары:\n{products}\n\n"
        f"📈 Конверсия: <b>{conversion}</b>"
    )
