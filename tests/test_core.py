import hashlib
import hmac
import json
import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import OWNER_ADMIN_ID, Settings
from app.payments.rollypay import verify_webhook as verify_rolly
from app.db import (
    Base,
    Product,
    SupportStatus,
    add_support_message,
    create_support_ticket,
    get_active_support_ticket,
    list_support_tickets,
    set_support_ticket_status,
    support_ticket_messages,
    support_rate_limited,
)
from app.keyboards import (
    admin_product_keyboard,
    main_keyboard,
    product_keyboard,
    product_kind_keyboard,
    support_ticket_keyboard,
)
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
        self.assertTrue(_same_amount("1000.00", Decimal("1000")))
        self.assertFalse(_same_amount("999.99", Decimal("1000")))
        self.assertFalse(_same_amount("not-a-number", Decimal("1000")))

    @patch("app.keyboards.get_settings")
    def test_product_has_sbp_stars_and_admin_controls(self, mocked_settings):
        mocked_settings.return_value.rollypay_enabled = True
        product = Product(id=7, key="item", title="Товар", description="Описание", price_rub=990, price_stars=350, emoji="🛍", kind="digital", requires_brief=False)
        buy_data = [button.callback_data for row in product_keyboard(product).inline_keyboard for button in row if button.callback_data]
        admin_data = [button.callback_data for row in admin_product_keyboard(product).inline_keyboard for button in row if button.callback_data]
        self.assertIn("buy:stars:7", buy_data)
        self.assertIn("buy:rolly:7", buy_data)
        self.assertNotIn("buy:crypto:7", buy_data)
        self.assertIn("admin:edit:rub:7", admin_data)
        self.assertIn("admin:edit:stars:7", admin_data)
        buy_labels = [button.text for row in product_keyboard(product).inline_keyboard for button in row]
        self.assertTrue(any("Оплатить по СБП" in label for label in buy_labels))
        self.assertTrue(any("Оплатить Stars" in label for label in buy_labels))

    def test_admin_main_menu_and_support_controls(self):
        regular_buttons = [button.text for row in main_keyboard(False).keyboard for button in row]
        admin_buttons = [button.text for row in main_keyboard(True).keyboard for button in row]
        self.assertNotIn("⚙️ Админ-панель", regular_buttons)
        self.assertIn("⚙️ Админ-панель", admin_buttons)
        self.assertIn("📦 Заказы", regular_buttons)
        self.assertIn("💬 Поддержка", regular_buttons)
        kind_actions = [button.callback_data for row in product_kind_keyboard().inline_keyboard for button in row]
        self.assertIn("admin:kind:physical", kind_actions)
        self.assertIn("admin:kind:digital", kind_actions)
        self.assertIn(DIVIDER, screen("✨", "Заголовок", "Текст"))

    @patch("app.payments.rollypay.get_settings")
    def test_rollypay_signature(self, mocked_settings):
        mocked_settings.return_value.rollypay_signing_secret = "secret"
        body = json.dumps({"status": "paid"}, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(b"secret", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
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
            self.assertEqual((await list_support_tickets(session, status=SupportStatus.NEW.value))[0].id, ticket.id)
            self.assertEqual((await support_ticket_messages(session, ticket.id))[0].body, "Нужна помощь")

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


if __name__ == "__main__":
    unittest.main()
