from __future__ import annotations

from datetime import datetime, timedelta, timezone
from sqlalchemy import func, select

from app.db import Order, OrderStatus, User


async def users_count(session):
    return await session.scalar(select(func.count(User.telegram_id))) or 0


async def sales_sum(session, days: int | None = None):
    query = select(func.sum(Order.amount_rub)).where(Order.status == OrderStatus.PAID.value)
    if days:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        query = query.where(Order.paid_at >= since)
    return await session.scalar(query) or 0


async def paid_orders_count(session):
    return await session.scalar(
        select(func.count(Order.id)).where(Order.status == OrderStatus.PAID.value)
    ) or 0
