"""Regression tests for values entered from the group admin panel."""
import atexit
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_TEST_DIR = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"DB_PATH": str(Path(_TEST_DIR.name) / "bot.sqlite3")}):
    import main as app


atexit.register(_TEST_DIR.cleanup)


def group_message(text: str, chat_id: int = -10042):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=111, is_bot=False),
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        text=text,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=55)),
    )


class AdminGroupSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app.admin_sessions.clear()

    async def test_group_price_input_is_consumed_and_saved(self):
        message = group_message("250")
        app.admin_sessions[111] = ("ticket_price", -10042)
        with patch.object(app, "is_moderation_admin", new=AsyncMock(return_value=True)), \
             patch.object(app.db, "set_setting") as set_setting, \
             patch.object(app.db, "set_active_giveaway_price") as set_active_price:
            self.assertTrue(await app.consume_admin_session(message, AsyncMock()))
        set_setting.assert_called_once_with("giveaway_ticket_price_rub", "250")
        set_active_price.assert_called_once_with(250)
        self.assertNotIn(111, app.admin_sessions)

    async def test_group_creation_collects_title_price_and_end_date(self):
        message = group_message("Telegram Premium")
        app.admin_sessions[111] = ("giveaway_title", -10042)
        with patch.object(app, "is_moderation_admin", new=AsyncMock(return_value=True)), \
             patch.object(app.db, "create_giveaway", return_value=9) as create, \
             patch.object(app.db, "set_giveaway_message"), \
             patch.object(app, "giveaway_card", return_value="card"), \
             patch.object(app, "giveaway_keyboard", return_value=object()), \
             patch.object(app, "parse_giveaway_end", return_value="2026-11-25 18:00:00"):
            self.assertTrue(await app.consume_admin_session(message, AsyncMock()))
            self.assertEqual(app.admin_sessions[111][0], "giveaway_price")
            message.text = "100"
            self.assertTrue(await app.consume_admin_session(message, AsyncMock()))
            self.assertEqual(app.admin_sessions[111][0], "giveaway_ends_at")
            message.text = "4д"
            self.assertTrue(await app.consume_admin_session(message, AsyncMock()))
        create.assert_called_once_with("Telegram Premium", 100, 111, -10042, "2026-11-25 18:00:00")

    async def test_group_invoice_input_creates_invoice(self):
        message = group_message("500 Оплата услуги")
        app.admin_sessions[111] = ("invoice", -10042)
        payment = {"payment_id": "payment-1", "pay_url": "https://pay.example/1"}
        with patch.object(app, "is_moderation_admin", new=AsyncMock(return_value=True)), \
             patch.object(app, "ROLLYPAY_API_KEY", "key"), \
             patch.object(app, "ROLLYPAY_TERMINAL_ID", "terminal"), \
             patch.object(app, "create_payment", new=AsyncMock(return_value=payment)), \
             patch.object(app.db, "create_invoice") as create_invoice:
            self.assertTrue(await app.consume_admin_session(message, AsyncMock()))
        self.assertEqual(create_invoice.call_args.args[2:5], (111, 111, 500))
        self.assertNotIn(111, app.admin_sessions)

    async def test_session_cannot_consume_text_from_another_chat(self):
        app.admin_sessions[111] = ("ticket_price", -10042)
        with patch.object(app, "is_moderation_admin", new=AsyncMock(return_value=True)):
            self.assertFalse(await app.consume_admin_session(group_message("250", -10099), AsyncMock()))

    async def test_admin_menu_uses_telegram_custom_emoji_button_icons(self):
        keyboard = app.admin_menu_keyboard()
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertIn("RollyPay", [button.text for button in buttons])
        self.assertTrue(all(button.icon_custom_emoji_id for button in buttons))
        self.assertTrue(all(not button.text.startswith(("🎁", "🎟", "📢", "📨", "📊", "🏆", "💳", "💸", "👥", "📈")) for button in buttons))

    async def test_restart_notice_has_magnitogorsk_datetime_and_signature(self):
        notice = app.restart_notice()
        self.assertIn("Бот перезапущен", notice)
        self.assertRegex(notice, r"Магнитогорск: <b>\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}</b>")
        self.assertTrue(notice.endswith("Позор МГН крутой бот, йоу"))

    async def test_rollypay_admin_card_shows_live_balance_and_counts(self):
        with patch.object(app, "ROLLYPAY_API_KEY", "key"), \
             patch.object(app, "ROLLYPAY_TERMINAL_ID", "terminal-1234567890"), \
             patch.object(app, "get_balance", new=AsyncMock(return_value={
                 "terminal_id": "terminal-1234567890", "available_usdt": "12.50", "hold_usdt": "1.25"
             })), \
             patch.object(app, "get_rate", new=AsyncMock(return_value={"rate": "100.00"})), \
             patch.object(app, "list_rollypay_payments", new=AsyncMock(side_effect=[
                 {"items": [], "total": 9}, {"items": [], "total": 6}
             ])):
            card = await app.rollypay_admin_card()
        self.assertIn("12.50 USDT", card)
        self.assertIn("1250.00 ₽", card)
        self.assertIn("оплачено: <b>6</b>", card)
