from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.catalog import PRODUCT_SEEDS, REMOVED_PRODUCT_KEYS
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


class SupportStatus(StrEnum):
    NEW = "new"
    ANSWERED = "answered"
    CLOSED = "closed"


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    balance_rub: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    purchases_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # physical | digital_song
    product_key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    amount_rub: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    amount_stars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=OrderStatus.CREATED.value, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Review(Base):
    """One customer review per order: 1-5 stars plus an optional comment."""

    __tablename__ = "reviews"
    __table_args__ = (CheckConstraint("rating >= 1 AND rating <= 5"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(
        String(32), default=SupportStatus.NEW.value, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(Integer, index=True)
    sender: Mapped[str] = mapped_column(String(16))  # user | admin
    content_type: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text, default="")
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delivered_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class BonusAccount(Base):
    __tablename__ = "bonus_accounts"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    referred_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class BonusTransaction(Base):
    __tablename__ = "bonus_transactions"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "reason", "reference", name="uq_bonus_reason_reference"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(32))
    reference: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class DailyBonusProfile(Base):
    __tablename__ = "daily_bonus_profiles"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    streak: Mapped[int] = mapped_column(Integer, default=0)
    last_claim_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class SecretOffer(Base):
    __tablename__ = "secret_offers"
    __table_args__ = (
        UniqueConstraint("user_id", "cycle_key", name="uq_secret_offer_cycle"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    product_id: Mapped[int] = mapped_column(Integer, index=True)
    cycle_key: Mapped[str] = mapped_column(String(16), index=True)
    discount_percent: Mapped[int] = mapped_column(Integer)
    price_rub: Mapped[int] = mapped_column(Integer)
    price_stars: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    order_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OrderFulfillment(Base):
    __tablename__ = "order_fulfillments"

    order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    method: Mapped[str] = mapped_column(String(16))  # delivery | pickup | digital
    address: Mapped[str] = mapped_column(Text, default="")
    fee_rub: Mapped[int] = mapped_column(Integer, default=0)
    fee_stars: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    scheduled_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    bonus_amount: Mapped[int] = mapped_column(Integer)
    max_uses: Mapped[int] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class PromoRedemption(Base):
    __tablename__ = "promo_redemptions"
    __table_args__ = (UniqueConstraint("promo_id", "user_id", name="uq_promo_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    promo_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    bonus_amount: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    order_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    provider_payment_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    event_status: Mapped[str] = mapped_column(String(32))
    result: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String(255), default="")
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    delivery_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class PaymentReceipt(Base):
    __tablename__ = "payment_receipts"
    __table_args__ = (
        UniqueConstraint("provider", "provider_payment_id", name="uq_provider_payment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_payment_id: Mapped[str] = mapped_column(String(255), index=True)
    order_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


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

        def _apply_light_migrations(sync_conn) -> None:
            """Lightweight migrations for databases created before a feature."""
            inspector = inspect(sync_conn)
            pending = {
                "order_fulfillments": {
                    "scheduled_date": "DATE",
                    "scheduled_time": "VARCHAR(5)",
                },
                "orders": {"issued_at": "DATETIME"},
            }
            existing_tables = set(inspector.get_table_names())
            for table, columns in pending.items():
                if table not in existing_tables:
                    continue
                present = {column["name"] for column in inspector.get_columns(table)}
                for name, ddl in columns.items():
                    if name not in present:
                        sync_conn.execute(
                            text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                        )

        await conn.run_sync(_apply_light_migrations)
    async with SessionLocal() as session:
        for seed in PRODUCT_SEEDS:
            existing = await session.scalar(
                select(Product).where(Product.key == seed.key)
            )
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
        await session.execute(
            delete(Product).where(Product.key.in_(REMOVED_PRODUCT_KEYS))
        )
        await session.commit()


async def register_user(
    session: AsyncSession, telegram_id: int, username: str | None, full_name: str
) -> tuple[User, bool]:
    user = await session.get(User, telegram_id)
    created = user is None
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
    return user, created


async def get_or_create_user(
    session: AsyncSession, telegram_id: int, username: str | None, full_name: str
) -> User:
    user, _ = await register_user(session, telegram_id, username, full_name)
    return user


async def get_bonus_account(session: AsyncSession, user_id: int) -> BonusAccount:
    account = await session.get(BonusAccount, user_id)
    if account is None:
        account = BonusAccount(user_id=user_id)
        session.add(account)
        await session.flush()
    return account


DAILY_REWARDS = (10, 15, 20, 25, 30, 40, 50)
SHOP_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")


def shop_today() -> date:
    return datetime.now(SHOP_TIMEZONE).date()


async def daily_bonus_status(
    session: AsyncSession, user_id: int
) -> dict[str, int | bool]:
    today = shop_today()
    profile = await session.get(DailyBonusProfile, user_id)
    if profile is None:
        return {"claimed": False, "streak": 0, "next_reward": DAILY_REWARDS[0]}
    claimed = profile.last_claim_date == today
    if claimed or profile.last_claim_date == today - timedelta(days=1):
        next_streak = profile.streak % len(DAILY_REWARDS) + 1
        visible_streak = profile.streak
    else:
        next_streak = 1
        visible_streak = 0
    return {
        "claimed": claimed,
        "streak": visible_streak,
        "next_reward": DAILY_REWARDS[next_streak - 1],
    }


async def claim_daily_bonus(
    session: AsyncSession, user_id: int
) -> tuple[bool, int, int]:
    """Credits the daily reward once per shop-local calendar day."""
    today = shop_today()
    profile = await session.get(DailyBonusProfile, user_id)
    if profile is not None and profile.last_claim_date == today:
        return False, 0, profile.streak

    previous_streak = profile.streak if profile else 0
    continued = profile is not None and profile.last_claim_date == today - timedelta(
        days=1
    )
    streak = previous_streak % len(DAILY_REWARDS) + 1 if continued else 1
    reward = DAILY_REWARDS[streak - 1]
    account = await get_bonus_account(session, user_id)
    if profile is None:
        profile = DailyBonusProfile(user_id=user_id)
        session.add(profile)
    profile.streak = streak
    profile.last_claim_date = today
    profile.updated_at = datetime.now(timezone.utc)
    account.balance += reward
    session.add(
        BonusTransaction(
            user_id=user_id,
            amount=reward,
            reason="daily",
            reference=today.isoformat(),
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        stored = await session.get(DailyBonusProfile, user_id)
        return False, 0, stored.streak if stored else 0
    return True, reward, streak


def _offer_cycle(now: datetime) -> str:
    local = now.astimezone(SHOP_TIMEZONE)
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02d}"


async def get_or_create_secret_offer(
    session: AsyncSession, user_id: int, *, now: datetime | None = None
) -> tuple[SecretOffer | None, Product | None]:
    """Returns one deterministic personal offer per user and ISO week."""
    now = now or datetime.now(timezone.utc)
    cycle = _offer_cycle(now)
    existing = await session.scalar(
        select(SecretOffer).where(
            SecretOffer.user_id == user_id, SecretOffer.cycle_key == cycle
        )
    )
    if existing is not None:
        return existing, await session.get(Product, existing.product_id)

    products = await active_products(session)
    if not products:
        return None, None
    digest = hashlib.sha256(f"{user_id}:{cycle}".encode()).digest()
    product = products[int.from_bytes(digest[:4], "big") % len(products)]
    discount = (10, 15, 20)[digest[4] % 3]
    offer = SecretOffer(
        user_id=user_id,
        product_id=product.id,
        cycle_key=cycle,
        discount_percent=discount,
        price_rub=max(1, (int(product.price_rub or 0) * (100 - discount) + 99) // 100),
        price_stars=max(
            1, (int(product.price_stars or 0) * (100 - discount) + 99) // 100
        ),
        expires_at=now + timedelta(hours=2),
    )
    session.add(offer)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        offer = await session.scalar(
            select(SecretOffer).where(
                SecretOffer.user_id == user_id, SecretOffer.cycle_key == cycle
            )
        )
        return offer, await session.get(Product, offer.product_id) if offer else None
    await session.refresh(offer)
    return offer, product


async def reserve_secret_offer(
    session: AsyncSession,
    *,
    offer_id: str,
    user_id: int,
    order_id: str,
    now: datetime | None = None,
) -> SecretOffer | None:
    now = now or datetime.now(timezone.utc)
    result = await session.execute(
        update(SecretOffer)
        .where(
            SecretOffer.id == offer_id,
            SecretOffer.user_id == user_id,
            SecretOffer.status == "active",
            SecretOffer.expires_at > now,
        )
        .values(status="reserved", order_id=order_id)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if result.rowcount != 1:
        return None
    return await session.scalar(
        select(SecretOffer)
        .where(SecretOffer.id == offer_id)
        .execution_options(populate_existing=True)
    )


async def release_secret_offer(
    session: AsyncSession, *, offer_id: str, order_id: str
) -> None:
    await session.execute(
        update(SecretOffer)
        .where(
            SecretOffer.id == offer_id,
            SecretOffer.order_id == order_id,
            SecretOffer.status == "reserved",
        )
        .values(status="active", order_id=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def save_order_fulfillment(
    session: AsyncSession,
    *,
    order_id: str,
    method: str,
    address: str = "",
    fee_rub: int = 0,
    fee_stars: int = 0,
    scheduled_date: date | None = None,
    scheduled_time: str | None = None,
) -> OrderFulfillment:
    fulfillment = OrderFulfillment(
        order_id=order_id,
        method=method,
        address=address.strip(),
        fee_rub=fee_rub,
        fee_stars=fee_stars,
        scheduled_date=scheduled_date,
        scheduled_time=scheduled_time,
    )
    session.add(fulfillment)
    await session.commit()
    await session.refresh(fulfillment)
    return fulfillment


async def get_order_fulfillment(
    session: AsyncSession, order_id: str
) -> OrderFulfillment | None:
    return await session.get(OrderFulfillment, order_id)


async def apply_referral(
    session: AsyncSession,
    *,
    new_user_id: int,
    referrer_id: int,
    referrer_bonus: int = 100,
    new_user_bonus: int = 50,
) -> bool:
    """Awards a referral exactly once. Invalid and repeated referrals are harmless."""
    if new_user_id == referrer_id or await session.get(User, referrer_id) is None:
        return False
    account = await get_bonus_account(session, new_user_id)
    if account.referred_by is not None:
        return False
    inviter_account = await get_bonus_account(session, referrer_id)
    account.referred_by = referrer_id
    account.balance += new_user_bonus
    inviter_account.balance += referrer_bonus
    reference = str(new_user_id)
    session.add_all(
        [
            BonusTransaction(
                user_id=new_user_id,
                amount=new_user_bonus,
                reason="referral_join",
                reference=reference,
            ),
            BonusTransaction(
                user_id=referrer_id,
                amount=referrer_bonus,
                reason="referral_invite",
                reference=reference,
            ),
        ]
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True


async def redeem_promo_code(
    session: AsyncSession, *, user_id: int, code: str
) -> tuple[str, int]:
    normalized = code.strip().upper()
    promo = await session.scalar(select(PromoCode).where(PromoCode.code == normalized))
    if promo is None:
        return "not_found", 0
    if not promo.is_active:
        return "inactive", 0
    already_used = await session.scalar(
        select(PromoRedemption.id).where(
            PromoRedemption.promo_id == promo.id, PromoRedemption.user_id == user_id
        )
    )
    if already_used is not None:
        return "already_used", 0
    claimed = await session.execute(
        update(PromoCode)
        .where(
            PromoCode.id == promo.id,
            PromoCode.is_active.is_(True),
            PromoCode.used_count < PromoCode.max_uses,
        )
        .values(used_count=PromoCode.used_count + 1)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        return "limit_reached", 0
    account = await get_bonus_account(session, user_id)
    account.balance += promo.bonus_amount
    session.add(
        PromoRedemption(
            promo_id=promo.id, user_id=user_id, bonus_amount=promo.bonus_amount
        )
    )
    session.add(
        BonusTransaction(
            user_id=user_id,
            amount=promo.bonus_amount,
            reason="promo",
            reference=str(promo.id),
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return "already_used", 0
    return "ok", promo.bonus_amount


async def create_promo_code(
    session: AsyncSession, *, code: str, bonus_amount: int, max_uses: int
) -> PromoCode | None:
    promo = PromoCode(
        code=code.strip().upper(), bonus_amount=bonus_amount, max_uses=max_uses
    )
    session.add(promo)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None
    await session.refresh(promo)
    return promo


async def list_promo_codes(session: AsyncSession, limit: int = 30) -> list[PromoCode]:
    result = await session.execute(
        select(PromoCode).order_by(PromoCode.id.desc()).limit(limit)
    )
    return list(result.scalars())


async def recent_bonus_transactions(
    session: AsyncSession, user_id: int, limit: int = 10
) -> list[BonusTransaction]:
    result = await session.execute(
        select(BonusTransaction)
        .where(BonusTransaction.user_id == user_id)
        .order_by(BonusTransaction.id.desc())
        .limit(limit)
    )
    return list(result.scalars())


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

    if provider_payment_id:
        await session.execute(
            sqlite_insert(PaymentReceipt)
            .values(
                provider=payment_method,
                provider_payment_id=provider_payment_id,
                order_id=order_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    PaymentReceipt.provider,
                    PaymentReceipt.provider_payment_id,
                ]
            )
        )
        receipt = await session.scalar(
            select(PaymentReceipt).where(
                PaymentReceipt.provider == payment_method,
                PaymentReceipt.provider_payment_id == provider_payment_id,
            )
        )
        if receipt is None or receipt.order_id != order_id:
            await session.commit()
            return existing, False

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
        await session.execute(
            update(SecretOffer)
            .where(
                SecretOffer.order_id == order_id,
                SecretOffer.status == "reserved",
            )
            .values(status="redeemed")
        )
    await session.commit()
    order = await session.get(Order, order_id)
    return order, changed


async def update_order_status(
    session: AsyncSession, order_id: str, status: str
) -> Order | None:
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
        if status in {OrderStatus.CANCELED.value, OrderStatus.EXPIRED.value}:
            await session.execute(
                update(SecretOffer)
                .where(
                    SecretOffer.order_id == order_id,
                    SecretOffer.status == "reserved",
                )
                .values(status="active", order_id=None)
            )
        await session.commit()
    return order


async def mark_order_issued(
    session: AsyncSession, order_id: str
) -> tuple[Order | None, bool]:
    """Confirms that the order was handed to the customer. Repeat calls are no-ops."""
    order = await session.get(Order, order_id)
    if order is None:
        return None, False
    if order.status != OrderStatus.PAID.value:
        return order, False
    if order.issued_at is not None:
        return order, False
    order.issued_at = datetime.now(timezone.utc)
    await session.commit()
    return order, True


async def revert_order_issuance(session: AsyncSession, order_id: str) -> Order | None:
    """Moves a paid order back to the 'waiting for handover' list."""
    order = await session.get(Order, order_id)
    if order is None:
        return None
    order.issued_at = None
    await session.commit()
    return order


async def cancel_paid_order(
    session: AsyncSession, order_id: str
) -> tuple[Order | None, bool]:
    """Admin-side 'order not completed': cancels a paid order and frees its offer.

    Kept separate from update_order_status so that provider webhooks can never
    cancel an order that is already paid.
    """
    order = await session.get(Order, order_id)
    if order is None or order.status not in {
        OrderStatus.CREATED.value,
        OrderStatus.PROCESSING.value,
        OrderStatus.PAID.value,
    }:
        return order, False
    order.status = OrderStatus.CANCELED.value
    order.issued_at = None
    await session.execute(
        update(SecretOffer)
        .where(SecretOffer.order_id == order_id, SecretOffer.status == "reserved")
        .values(status="active", order_id=None)
    )
    await session.commit()
    return order, True


async def get_user(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.get(User, telegram_id)


async def users_by_ids(session: AsyncSession, user_ids: list[int]) -> dict[int, User]:
    """Loads buyers for an order list in one query."""
    ids = {user_id for user_id in user_ids if user_id}
    if not ids:
        return {}
    rows = await session.scalars(select(User).where(User.telegram_id.in_(ids)))
    return {user.telegram_id: user for user in rows}


async def paid_orders(
    session: AsyncSession, *, unissued_only: bool = False, limit: int = 25
) -> list[Order]:
    statement = (
        select(Order)
        .where(Order.status == OrderStatus.PAID.value)
        .order_by(Order.paid_at.desc())
        .limit(limit)
    )
    if unissued_only:
        statement = statement.where(Order.issued_at.is_(None))
    return list((await session.execute(statement)).scalars())


async def pending_issue_count(session: AsyncSession) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.status == OrderStatus.PAID.value,
                Order.issued_at.is_(None),
            )
        )
    ) or 0


async def create_review(
    session: AsyncSession,
    *,
    order_id: str,
    user_id: int,
    title: str,
    rating: int,
    comment: str = "",
) -> Review | None:
    """Stores the first review for an order; later attempts return None."""
    existing = await session.scalar(
        select(Review.id).where(Review.order_id == order_id)
    )
    if existing:
        return None
    review = Review(
        order_id=order_id,
        user_id=user_id,
        title=title[:255],
        rating=max(1, min(5, int(rating))),
        comment=comment.strip()[:1000],
    )
    session.add(review)
    await session.commit()
    return review


async def has_review(session: AsyncSession, order_id: str) -> bool:
    return bool(
        await session.scalar(select(Review.id).where(Review.order_id == order_id))
    )


async def reviewed_order_ids(session: AsyncSession, user_id: int) -> set[str]:
    rows = await session.scalars(
        select(Review.order_id).where(Review.user_id == user_id)
    )
    return set(rows)


async def recent_reviews(session: AsyncSession, limit: int = 20) -> list[Review]:
    rows = await session.scalars(
        select(Review).order_by(Review.created_at.desc()).limit(limit)
    )
    return list(rows)


async def record_payment_event(
    session: AsyncSession,
    *,
    event_key: str,
    provider: str,
    event_status: str,
    result: str,
    order_id: str | None = None,
    provider_payment_id: str | None = None,
    reason: str = "",
    amount: Decimal | None = None,
    currency: str | None = None,
    payload_hash: str = "",
) -> bool:
    """Upserts a sanitized payment event and counts duplicate deliveries."""
    existed = await session.scalar(
        select(PaymentEvent.id).where(PaymentEvent.event_key == event_key)
    )
    now = datetime.now(timezone.utc)
    statement = (
        sqlite_insert(PaymentEvent)
        .values(
            event_key=event_key,
            provider=provider,
            order_id=order_id,
            provider_payment_id=provider_payment_id,
            event_status=event_status[:32],
            result=result[:32],
            reason=reason[:255],
            amount=amount,
            currency=currency[:8] if currency else None,
            payload_hash=payload_hash[:64],
            delivery_count=1,
            created_at=now,
            last_seen_at=now,
        )
        .on_conflict_do_update(
            index_elements=[PaymentEvent.event_key],
            set_={
                "delivery_count": PaymentEvent.delivery_count + 1,
                "last_seen_at": now,
            },
        )
    )
    await session.execute(statement)
    await session.commit()
    return existed is None


async def recent_payment_events(
    session: AsyncSession, limit: int = 20
) -> list[PaymentEvent]:
    result = await session.execute(
        select(PaymentEvent).order_by(PaymentEvent.last_seen_at.desc()).limit(limit)
    )
    return list(result.scalars())


async def get_shop_analytics(session: AsyncSession) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    paid_buyers = (
        await session.execute(
            select(func.count(func.distinct(Order.user_id))).where(
                Order.status == OrderStatus.PAID.value
            )
        )
    ).scalar_one()

    async def sales_since(since: datetime) -> dict[str, object]:
        row = (
            await session.execute(
                select(
                    func.count(Order.id),
                    func.coalesce(func.sum(Order.amount_rub), 0),
                    func.coalesce(func.sum(Order.amount_stars), 0),
                ).where(Order.status == OrderStatus.PAID.value, Order.paid_at >= since)
            )
        ).one()
        return {"orders": int(row[0]), "rub": Decimal(row[1]), "stars": int(row[2])}

    popular_rows = (
        await session.execute(
            select(Order.title, func.count(Order.id).label("sales"))
            .where(Order.status == OrderStatus.PAID.value)
            .group_by(Order.product_key, Order.title)
            .order_by(func.count(Order.id).desc(), Order.title)
            .limit(5)
        )
    ).all()
    return {
        "users": int(users),
        "paid_buyers": int(paid_buyers),
        "conversion": (float(paid_buyers) / users * 100) if users else 0.0,
        "day": await sales_since(now - timedelta(days=1)),
        "week": await sales_since(now - timedelta(days=7)),
        "month": await sales_since(now - timedelta(days=30)),
        "popular": [(str(title), int(sales)) for title, sales in popular_rows],
    }


async def recent_orders(
    session: AsyncSession, user_id: int, limit: int = 10
) -> list[Order]:
    result = await session.execute(
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
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


async def delete_product(session: AsyncSession, product_id: int) -> Product | None:
    """Permanently delete a product and its temporary personal offers.

    Order history is intentionally preserved: it references the product by
    title and key, so past purchases stay readable after the deletion.
    """
    product = await session.get(Product, product_id)
    if product is None:
        return None
    await session.execute(
        delete(SecretOffer).where(SecretOffer.product_id == product_id)
    )
    await session.delete(product)
    await session.commit()
    return product


async def get_product_by_key(session: AsyncSession, key: str) -> Product | None:
    return await session.scalar(select(Product).where(Product.key == key))


async def get_active_support_ticket(
    session: AsyncSession, user_id: int
) -> SupportTicket | None:
    return await session.scalar(
        select(SupportTicket)
        .where(
            SupportTicket.user_id == user_id,
            SupportTicket.status != SupportStatus.CLOSED.value,
        )
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


async def support_rate_limited(
    session: AsyncSession, user_id: int, seconds: int = 10
) -> bool:
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
    result = await session.execute(
        query.order_by(SupportTicket.last_message_at.desc()).limit(limit)
    )
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
    ticket.closed_at = (
        datetime.now(timezone.utc) if status == SupportStatus.CLOSED else None
    )
    await session.commit()
    await session.refresh(ticket)
    return ticket
