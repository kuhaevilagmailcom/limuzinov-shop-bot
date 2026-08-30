from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, String, Text, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings


class Base(DeclarativeBase):
    pass


class OrderStatus(StrEnum):
    CREATED = "created"
    PROCESSING = "processing"
    PAID = "paid"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REFUNDED = "refunded"
    CHARGEBACK = "chargeback"


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    balance_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    purchases_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # physical | digital_song
    product_key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    amount_rub: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    amount_stars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default=OrderStatus.CREATED.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


settings = get_settings()
engine = create_async_engine(settings.database_url, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    if settings.database_url.startswith("sqlite"):
        database_path = settings.database_url.removeprefix("sqlite+aiosqlite:///")
        if database_path and database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str | None, full_name: str) -> User:
    user = await session.get(User, telegram_id)
    if user is None:
        user = User(telegram_id=telegram_id, username=username, full_name=full_name)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    else:
        changed = user.username != username or user.full_name != full_name
        user.username = username
        user.full_name = full_name
        if changed:
            await session.commit()
    return user


async def create_order(
    session: AsyncSession,
    *,
    user_id: int,
    kind: str,
    product_key: str,
    title: str,
    description: str = "",
    amount_rub: Decimal | None = None,
    amount_stars: int | None = None,
) -> Order:
    order = Order(
        user_id=user_id,
        kind=kind,
        product_key=product_key,
        title=title,
        description=description,
        amount_rub=amount_rub,
        amount_stars=amount_stars,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def mark_order_paid(
    session: AsyncSession,
    order_id: str,
    *,
    payment_method: str,
    provider_payment_id: str | None = None,
) -> tuple[Order | None, bool]:
    """Atomically marks an order paid. Duplicate webhook delivery is harmless."""
    existing = await session.get(Order, order_id)
    if existing is None:
        return None, False

    values = {
        "status": OrderStatus.PAID.value,
        "payment_method": payment_method,
        "paid_at": datetime.now(timezone.utc),
    }
    if provider_payment_id:
        values["provider_payment_id"] = provider_payment_id

    result = await session.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.status.in_([OrderStatus.CREATED.value, OrderStatus.PROCESSING.value]),
        )
        .values(**values)
    )
    changed = result.rowcount == 1
    if changed:
        await session.execute(
            update(User)
            .where(User.telegram_id == existing.user_id)
            .values(purchases_count=User.purchases_count + 1)
        )
    await session.commit()
    order = await session.get(Order, order_id)
    return order, changed


async def update_order_status(session: AsyncSession, order_id: str, status: str) -> Order | None:
    order = await session.get(Order, order_id)
    if order is None:
        return None
    allowed = {
        OrderStatus.PROCESSING.value,
        OrderStatus.CANCELED.value,
        OrderStatus.EXPIRED.value,
        OrderStatus.REFUNDED.value,
        OrderStatus.CHARGEBACK.value,
    }
    if status in allowed and not (
        order.status == OrderStatus.PAID.value
        and status not in {OrderStatus.REFUNDED.value, OrderStatus.CHARGEBACK.value}
    ):
        order.status = status
        await session.commit()
    return order


async def recent_orders(session: AsyncSession, user_id: int, limit: int = 10) -> list[Order]:
    result = await session.execute(
        select(Order).where(Order.user_id == user_id).order_by(Order.created_at.desc()).limit(limit)
    )
    return list(result.scalars())
