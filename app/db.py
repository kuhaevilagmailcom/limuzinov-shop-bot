from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Numeric, String, Text, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings
from app.catalog import PRODUCT_SEEDS, REMOVED_PRODUCT_KEYS


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


class SupportStatus(StrEnum):
    NEW = "new"
    ANSWERED = "answered"
    CLOSED = "closed"


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    balance_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    purchases_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    price_rub: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_stars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    emoji: Mapped[str] = mapped_column(String(16), default="🛍")
    kind: Mapped[str] = mapped_column(String(32), default="physical")
    requires_brief: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
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


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default=SupportStatus.NEW.value, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(Integer, index=True)
    sender: Mapped[str] = mapped_column(String(16))  # user | admin
    content_type: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text, default="")
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delivered_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


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
    async with SessionLocal() as session:
        for seed in PRODUCT_SEEDS:
            existing = await session.scalar(select(Product).where(Product.key == seed.key))
            if existing is None:
                session.add(
                    Product(
                        key=seed.key,
                        title=seed.title,
                        description=seed.description,
                        price_rub=seed.price_rub,
                        price_stars=seed.price_stars,
                        emoji=seed.emoji,
                        kind=seed.kind,
                        requires_brief=seed.requires_brief,
                    )
                )
        await session.execute(delete(Product).where(Product.key.in_(REMOVED_PRODUCT_KEYS)))
        await session.commit()


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


async def active_products(session: AsyncSession) -> list[Product]:
    result = await session.execute(
        select(Product)
        .where(
            Product.is_active.is_(True),
            Product.price_rub.is_not(None),
            Product.price_rub > 0,
            Product.price_stars.is_not(None),
            Product.price_stars > 0,
        )
        .order_by(Product.id)
    )
    return list(result.scalars())


async def all_products(session: AsyncSession) -> list[Product]:
    result = await session.execute(select(Product).order_by(Product.id))
    return list(result.scalars())


async def get_product(session: AsyncSession, product_id: int) -> Product | None:
    return await session.get(Product, product_id)


async def get_product_by_key(session: AsyncSession, key: str) -> Product | None:
    return await session.scalar(select(Product).where(Product.key == key))


async def get_active_support_ticket(session: AsyncSession, user_id: int) -> SupportTicket | None:
    return await session.scalar(
        select(SupportTicket)
        .where(SupportTicket.user_id == user_id, SupportTicket.status != SupportStatus.CLOSED.value)
        .order_by(SupportTicket.id.desc())
        .limit(1)
    )


async def create_support_ticket(
    session: AsyncSession,
    *,
    user_id: int,
    username: str | None,
    full_name: str,
) -> SupportTicket:
    ticket = SupportTicket(user_id=user_id, username=username, full_name=full_name)
    session.add(ticket)
    await session.commit()
    await session.refresh(ticket)
    return ticket


async def add_support_message(
    session: AsyncSession,
    *,
    ticket: SupportTicket,
    sender: str,
    content_type: str,
    body: str = "",
    source_message_id: int | None = None,
    delivered_message_id: int | None = None,
) -> SupportMessage:
    now = datetime.now(timezone.utc)
    item = SupportMessage(
        ticket_id=ticket.id,
        sender=sender,
        content_type=content_type[:32],
        body=body[:4000],
        source_message_id=source_message_id,
        delivered_message_id=delivered_message_id,
        created_at=now,
    )
    session.add(item)
    ticket.last_message_at = now
    if sender == "user":
        ticket.status = SupportStatus.NEW.value
        ticket.closed_at = None
    elif sender == "admin":
        ticket.status = SupportStatus.ANSWERED.value
    await session.commit()
    await session.refresh(item)
    return item


async def support_rate_limited(session: AsyncSession, user_id: int, seconds: int = 10) -> bool:
    last_sent = await session.scalar(
        select(SupportMessage.created_at)
        .join(SupportTicket, SupportTicket.id == SupportMessage.ticket_id)
        .where(SupportTicket.user_id == user_id, SupportMessage.sender == "user")
        .order_by(SupportMessage.created_at.desc())
        .limit(1)
    )
    if last_sent is None:
        return False
    if last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_sent).total_seconds() < seconds


async def list_support_tickets(
    session: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 30,
) -> list[SupportTicket]:
    query = select(SupportTicket)
    if status:
        query = query.where(SupportTicket.status == status)
    result = await session.execute(query.order_by(SupportTicket.last_message_at.desc()).limit(limit))
    return list(result.scalars())


async def support_ticket_messages(
    session: AsyncSession,
    ticket_id: int,
    limit: int = 20,
) -> list[SupportMessage]:
    result = await session.execute(
        select(SupportMessage)
        .where(SupportMessage.ticket_id == ticket_id)
        .order_by(SupportMessage.created_at.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


async def set_support_ticket_status(
    session: AsyncSession,
    ticket_id: int,
    status: SupportStatus,
) -> SupportTicket | None:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None:
        return None
    ticket.status = status.value
    ticket.closed_at = datetime.now(timezone.utc) if status == SupportStatus.CLOSED else None
    await session.commit()
    await session.refresh(ticket)
    return ticket
