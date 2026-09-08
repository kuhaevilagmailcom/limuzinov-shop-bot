import hashlib
import hmac
import json
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cleanup import CleanBot
from app.config import OWNER_ADMIN_ID, Settings
from app.db import (
    Base,
    BonusAccount,
    DailyBonusProfile,
    PaymentEvent,
    Product,
    SecretOffer,
    SupportStatus,
    add_support_message,
    apply_referral,
    claim_daily_bonus,
    create_order,
    create_promo_code,
    create_support_ticket,
    daily_bonus_status,
    get_active_support_ticket,
    get_or_create_secret_offer,
    get_order_fulfillment,
    get_shop_analytics,
    list_support_tickets,
    mark_order_paid,
    record_payment_event,
    redeem_promo_code,
    register_user,
    reserve_secret_offer,
    save_order_fulfillment,
    set_support_ticket_status,
    support_rate_limited,
    support_ticket_messages,
)
from app.keyboards import (
    admin_back_keyboard,
    admin_keyboard,
    admin_product_keyboard,
    bonus_back_keyboard,
    bonus_keyboard,
    catalog_keyboard,
    checkout_keyboard,
    home_inline_keyboard,
    main_keyboard,
    product_keyboard,
    product_kind_keyboard,
    secret_offer_keyboard,
    stars_invoice_keyboard,
    support_cancel_keyboard,
    support_ticket_keyboard,
)
from app.payments.rollypay import verify_webhook as verify_rolly
from app.ui import DIVIDER, screen
from app.web import _same_amount, create_web_app


class CoreTests(unittest.TestCase):
    def test_owner_is_admin_without_env_setting(self):
        settings = Settings(bot_token="test", admin_ids="123")
        self.assertEqual(OWNER_ADMIN_ID, 8464597898)
        self.assertEqual(settings.admins, {8464597898, 123})

    def test_http_service_has_no_public_storefront(self):
        app = create_web_app(MagicMock())
        paths = {route.path for route in app.routes}
        self.assertNotIn("/", paths)
        self.assertEqual(paths, {"/health", "/rollypay/callback", "/webhooks/rollypay"})

    def test_amount_match_is_exact(self):
        self.assertTrue(_same_amount("1000.00", Decimal(1000)))
        self.assertFalse(_same_amount("999.99", Decimal(1000)))
        self.assertFalse(_same_amount("not-a-number", Decimal(1000)))

    @patch("app.keyboards.get_settings")
    def test_product_has_sbp_stars_and_admin_controls(self, mocked_settings):
        mocked_settings.return_value.rollypay_enabled = True
        product = Product(
            id=7,
            key="item",
            title="Товар",
            description="Описание",
            price_rub=990,
            price_stars=350,
            emoji="🛍",
            kind="digital",
            requires_brief=False,
        )
        buy_data = [
            button.callback_data
            for row in product_keyboard(product).inline_keyboard
            for button in row
            if button.callback_data
        ]
        admin_data = [
            button.callback_data
            for row in admin_product_keyboard(product).inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertIn("buy:stars:7", buy_data)
        self.assertIn("buy:rolly:7", buy_data)
        self.assertNotIn("buy:crypto:7", buy_data)
        self.assertIn("admin:edit:rub:7", admin_data)
        self.assertIn("admin:edit:stars:7", admin_data)
        buy_labels = [
            button.text
            for row in product_keyboard(product).inline_keyboard
            for button in row
        ]
        self.assertTrue(any("Оплатить по СБП" in label for label in buy_labels))
        self.assertTrue(any("Оплатить звёздами" in label for label in buy_labels))

        physical = Product(
            id=8,
            key="physical",
            title="Физический товар",
            price_rub=100,
            price_stars=40,
            kind="physical",
        )
        physical_actions = {
            button.callback_data
            for row in product_keyboard(physical).inline_keyboard
            for button in row
            if button.callback_data
        }
        self.assertIn("fulfill:delivery:8:0", physical_actions)
        self.assertIn("fulfill:pickup:8:0", physical_actions)
        checkout_labels = [
            button.text
            for row in checkout_keyboard(
                amount_rub=150, amount_stars=65, back_callback="product:8"
            ).inline_keyboard
            for button in row
        ]
        self.assertIn("Оплатить по СБП · 150 ₽", checkout_labels)
        self.assertIn("Оплатить звёздами · 65 ⭐", checkout_labels)

    def test_admin_main_menu_and_support_controls(self):
        regular_buttons = [
            button.text for row in main_keyboard(False).keyboard for button in row
        ]
        admin_buttons = [
            button.text for row in main_keyboard(True).keyboard for button in row
        ]
        self.assertNotIn("Управление магазином", regular_buttons)
        self.assertIn("Управление магазином", admin_buttons)
        self.assertIn("Мои заказы", regular_buttons)
        self.assertIn("Поддержка", regular_buttons)
        self.assertNotIn("← Назад", regular_buttons)
        self.assertIn("Бонусы", regular_buttons)
        self.assertNotIn("Бонусный клуб", regular_buttons)
        self.assertTrue(
            all(
                button.icon_custom_emoji_id
                for row in main_keyboard(False).keyboard
                for button in row
            )
        )
        self.assertTrue(
            all(
                button.style in {"primary", "success", "danger"}
                for row in main_keyboard(False).keyboard
                for button in row
            )
        )
        kind_actions = [
            button.callback_data
            for row in product_kind_keyboard().inline_keyboard
            for button in row
        ]
        self.assertIn("admin:kind:physical", kind_actions)
        self.assertIn("admin:kind:digital", kind_actions)
        styled = screen("✨", "Заголовок", "Текст", "Подсказка")
        self.assertIn("<b>Заголовок</b>", styled)
        self.assertIn("<i>Подсказка</i>", styled)
        self.assertIn(DIVIDER, styled)

    def test_every_main_section_has_back_navigation(self):
        product = Product(
            id=7, key="item", title="Товар", price_rub=100, price_stars=10
        )
        keyboards = [
            catalog_keyboard([product]),
            bonus_keyboard(),
            bonus_back_keyboard(),
            admin_keyboard(),
            admin_back_keyboard(),
            support_cancel_keyboard(),
            home_inline_keyboard(),
            stars_invoice_keyboard(),
        ]
        for keyboard in keyboards:
            actions = {
                button.callback_data
                for row in keyboard.inline_keyboard
                for button in row
                if button.callback_data
            }
            self.assertTrue(
                actions & {"home", "bonus:back", "support:cancel", "admin:home"}
            )
            self.assertTrue(
                all(
                    button.style in {"primary", "success", "danger"}
                    for row in keyboard.inline_keyboard
                    for button in row
                )
            )
        self.assertTrue(stars_invoice_keyboard().inline_keyboard[0][0].pay)

    @patch("app.payments.rollypay.get_settings")
    def test_rollypay_signature(self, mocked_settings):
        mocked_settings.return_value.rollypay_signing_secret = "secret"
        body = json.dumps({"status": "paid"}, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            b"secret", timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        self.assertTrue(verify_rolly(body, timestamp, signature))
        self.assertFalse(verify_rolly(body + b" ", timestamp, signature))


class SupportDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_ticket_messages_status_and_rate_limit(self):
        async with self.sessions() as session:
            self.assertIsNone(await get_active_support_ticket(session, 42))
            ticket = await create_support_ticket(
                session,
                user_id=42,
                username="buyer",
                full_name="Покупатель",
            )
            await add_support_message(
                session,
                ticket=ticket,
                sender="user",
                content_type="text",
                body="Нужна помощь",
                source_message_id=10,
            )
            self.assertTrue(await support_rate_limited(session, 42))
            self.assertEqual(
                (await list_support_tickets(session, status=SupportStatus.NEW.value))[
                    0
                ].id,
                ticket.id,
            )
            self.assertEqual(
                (await support_ticket_messages(session, ticket.id))[0].body,
                "Нужна помощь",
            )

            await add_support_message(
                session,
                ticket=ticket,
                sender="admin",
                content_type="text",
                body="Помогаем",
                source_message_id=11,
                delivered_message_id=12,
            )
            self.assertEqual(ticket.status, SupportStatus.ANSWERED.value)
            controls = [
                button.callback_data
                for row in support_ticket_keyboard(ticket).inline_keyboard
                for button in row
            ]
            self.assertIn(f"support:reply:{ticket.id}", controls)

            await set_support_ticket_status(session, ticket.id, SupportStatus.CLOSED)
            self.assertEqual(ticket.status, SupportStatus.CLOSED.value)
            self.assertIsNone(await get_active_support_ticket(session, 42))

    async def test_bonus_promos_referrals_and_duplicate_protection(self):
        async with self.sessions() as session:
            await register_user(session, 100, "inviter", "Пригласивший")
            await register_user(session, 200, "friend", "Друг")
            self.assertTrue(
                await apply_referral(session, new_user_id=200, referrer_id=100)
            )
            self.assertFalse(
                await apply_referral(session, new_user_id=200, referrer_id=100)
            )
            self.assertEqual((await session.get(BonusAccount, 100)).balance, 100)
            self.assertEqual((await session.get(BonusAccount, 200)).balance, 50)

            promo = await create_promo_code(
                session, code="welcome", bonus_amount=75, max_uses=1
            )
            self.assertIsNotNone(promo)
            self.assertEqual(
                await redeem_promo_code(session, user_id=200, code="WELCOME"),
                ("ok", 75),
            )
            self.assertEqual(
                await redeem_promo_code(session, user_id=200, code="WELCOME"),
                ("already_used", 0),
            )
            self.assertEqual((await session.get(BonusAccount, 200)).balance, 125)

    async def test_daily_bonus_streak_and_single_claim_per_day(self):
        async with self.sessions() as session:
            await register_user(session, 500, "daily", "Постоянный клиент")
            with patch("app.db.shop_today", return_value=date(2026, 9, 7)):
                self.assertEqual(await claim_daily_bonus(session, 500), (True, 10, 1))
                self.assertEqual(await claim_daily_bonus(session, 500), (False, 0, 1))
            with patch("app.db.shop_today", return_value=date(2026, 9, 8)):
                self.assertEqual(await claim_daily_bonus(session, 500), (True, 15, 2))
            profile = await session.get(DailyBonusProfile, 500)
            self.assertEqual(profile.streak, 2)
            self.assertEqual((await session.get(BonusAccount, 500)).balance, 25)
            with patch("app.db.shop_today", return_value=date(2026, 9, 10)):
                status = await daily_bonus_status(session, 500)
                self.assertEqual(status["streak"], 0)
                self.assertEqual(status["next_reward"], 10)

    async def test_two_hour_secret_offer_reservation_and_fulfillment(self):
        async with self.sessions() as session:
            await register_user(session, 700, "secret", "Покупатель")
            product = Product(
                key="gift",
                title="Подарок",
                description="Тест",
                price_rub=1000,
                price_stars=100,
                kind="physical",
                is_active=True,
            )
            session.add(product)
            await session.commit()
            await session.refresh(product)
            now = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)
            offer, selected = await get_or_create_secret_offer(session, 700, now=now)
            self.assertEqual(selected.id, product.id)
            self.assertEqual(
                offer.expires_at.replace(tzinfo=timezone.utc), now + timedelta(hours=2)
            )
            same, _ = await get_or_create_secret_offer(
                session, 700, now=now + timedelta(hours=1)
            )
            self.assertEqual(same.id, offer.id)
            order = await create_order(
                session,
                user_id=700,
                kind="physical",
                product_key=product.key,
                title=product.title,
                amount_stars=offer.price_stars + 25,
            )
            reserved = await reserve_secret_offer(
                session,
                offer_id=offer.id,
                user_id=700,
                order_id=order.id,
                now=now + timedelta(hours=1),
            )
            self.assertIsNotNone(reserved)
            self.assertIsNone(
                await reserve_secret_offer(
                    session, offer_id=offer.id, user_id=700, order_id="another"
                )
            )
            await save_order_fulfillment(
                session,
                order_id=order.id,
                method="delivery",
                address="Екатеринбург, Ленина, 1",
                fee_stars=25,
            )
            fulfillment = await get_order_fulfillment(session, order.id)
            self.assertEqual(fulfillment.fee_stars, 25)
            _, changed = await mark_order_paid(
                session,
                order.id,
                payment_method="telegram_stars",
                provider_payment_id="stars-1",
            )
            self.assertTrue(changed)
            self.assertEqual(
                (await session.get(SecretOffer, offer.id)).status, "redeemed"
            )

            offer_actions = {
                button.callback_data
                for row in secret_offer_keyboard(
                    offer.id,
                    product_id=product.id,
                    product_kind="physical",
                    price_rub=offer.price_rub,
                    price_stars=offer.price_stars,
                    available=True,
                ).inline_keyboard
                for button in row
                if button.callback_data
            }
            self.assertIn(f"fulfill:delivery:{product.id}:{offer.id}", offer_actions)

    async def test_unique_orders_atomic_payment_and_event_log(self):
        async with self.sessions() as session:
            await register_user(session, 300, "buyer", "Покупатель")
            first = await create_order(
                session,
                user_id=300,
                kind="digital",
                product_key="test",
                title="Тест",
                amount_rub=Decimal(500),
            )
            second = await create_order(
                session,
                user_id=300,
                kind="digital",
                product_key="test",
                title="Тест",
                amount_rub=Decimal(500),
            )
            UUID(first.id)
            self.assertNotEqual(first.id, second.id)
            _, changed_first = await mark_order_paid(
                session,
                first.id,
                payment_method="rollypay",
                provider_payment_id="pay-1",
            )
            _, changed_again = await mark_order_paid(
                session,
                first.id,
                payment_method="rollypay",
                provider_payment_id="pay-1",
            )
            _, changed_other_order = await mark_order_paid(
                session,
                second.id,
                payment_method="rollypay",
                provider_payment_id="pay-1",
            )
            self.assertTrue(changed_first)
            self.assertFalse(changed_again)
            self.assertFalse(changed_other_order)

            await record_payment_event(
                session,
                event_key="event-1",
                provider="rollypay",
                order_id=first.id,
                provider_payment_id="pay-1",
                event_status="paid",
                result="accepted",
            )
            await record_payment_event(
                session,
                event_key="event-1",
                provider="rollypay",
                order_id=first.id,
                provider_payment_id="pay-1",
                event_status="paid",
                result="duplicate",
            )
            event = await session.scalar(
                select(PaymentEvent).where(PaymentEvent.event_key == "event-1")
            )
            self.assertEqual(event.delivery_count, 2)
            analytics = await get_shop_analytics(session)
            self.assertEqual(analytics["users"], 1)
            self.assertEqual(analytics["paid_buyers"], 1)
            self.assertEqual(analytics["day"]["orders"], 1)


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_previous_bot_message_is_deleted_before_next_screen(self):
        first = SimpleNamespace(message_id=101)
        second = SimpleNamespace(message_id=102)
        with (
            patch.object(
                Bot, "send_message", new=AsyncMock(side_effect=[first, second])
            ) as send_mock,
            patch.object(
                Bot, "delete_message", new=AsyncMock(return_value=True)
            ) as delete_mock,
        ):
            bot = CleanBot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
            try:
                await bot.send_message(42, "Первый экран")
                await bot.send_message(42, "Второй экран")
            finally:
                await bot.session.close()
        self.assertEqual(send_mock.await_count, 2)
        delete_mock.assert_has_awaits([call(42, 101)])


if __name__ == "__main__":
    unittest.main()
