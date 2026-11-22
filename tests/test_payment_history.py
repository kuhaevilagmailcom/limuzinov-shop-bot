"""Payment history, verified paid status, and reliable group alert regressions."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from storage import Storage


class PaymentLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "payments.sqlite3")
        self.db = Storage(self.path)
        self.db.register_user(101, "buyer_name", "Buyer <One>")
        sid, _ = self.db.add(
            user_id=202, full_name="Poster", username="poster",
            message_id=5, album_key=None,
        )
        self.db.create_removal_payment("post-1", "post-order-1", sid, 101, 150, action="author_lookup")
        self.db.create_invoice("invoice-1", "invoice-order-1", 101, 202, 400, "Оплата услуг")
        giveaway_id = self.db.create_giveaway("Prize", 100, 202, -100987)
        self.db.create_giveaway_ticket("ticket-1", "ticket-order-1", giveaway_id, 101, 200, 2)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_created_paid_and_unpaid_filters_across_all_payment_types(self):
        counts = self.db.payment_counts()
        self.assertEqual((counts["total"], counts["paid"], counts["unpaid"]), (3, 0, 3))
        self.assertEqual(len(self.db.list_payments("unpaid")), 3)
        self.assertEqual(self.db.pending_payment_notifications(), [])

        # Provider has confirmed the post payment; fulfillment can still be in progress.
        self.db.set_removal_payment_status("post-1", "processing")
        self.db.set_invoice_status("invoice-1", "paid")
        counts = self.db.payment_counts()
        self.assertEqual((counts["paid"], counts["unpaid"], counts["revenue"]), (2, 1, 550))
        paid = self.db.list_payments("paid")
        self.assertEqual({item["payment_type"] for item in paid}, {"post_action", "invoice"})
        self.assertTrue(all(item["buyer_name"] == "Buyer <One>" for item in paid))
        self.assertTrue(all(item["buyer_username"] == "buyer_name" for item in paid))
        self.assertTrue(all(item["buyer_id"] == 101 for item in paid))
        self.assertEqual(self.db.list_payments("unpaid")[0]["payment_id"], "ticket-1")
        self.assertEqual(len(self.db.list_payments("all", offset=1, limit=1)), 1)

    def test_notification_outbox_is_idempotent_and_persistent(self):
        self.db.set_removal_payment_status("post-1", "processing")
        self.db.set_removal_payment_status("post-1", "paid")
        self.db.set_removal_payment_status("post-1", "paid")
        self.db.set_invoice_status("invoice-1", "paid")
        self.db.set_invoice_status("invoice-1", "paid")
        self.db.set_giveaway_ticket_status("ticket-1", "paid")
        pending = self.db.pending_payment_notifications()
        self.assertEqual(len(pending), 3)
        self.assertEqual({row["payment_type"] for row in pending}, {"post_action", "invoice", "giveaway"})
        self.assertEqual(self.db.payment_counts()["revenue"], 750)
        self.db.mark_payment_notified("invoice", "invoice-1")
        self.assertEqual(len(self.db.pending_payment_notifications()), 2)

        self.db.close()
        self.db = Storage(self.path)
        self.assertEqual(len(self.db.pending_payment_notifications()), 2)
        self.assertEqual(len(self.db.list_payments("paid")), 3)
        with self.assertRaises(ValueError):
            self.db.list_payments("invalid")

    def test_duplicate_paid_post_is_counted_as_paid(self):
        self.db.set_removal_payment_status("post-1", "paid_duplicate")
        self.assertEqual(self.db.payment_counts()["paid"], 1)
        self.assertEqual(self.db.list_payments("paid")[0]["status"], "paid_duplicate")
        self.assertIsNotNone(self.db.list_payments("paid")[0]["paid_at"])


class GroupPaymentAlertsTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_send_is_retried_and_alert_escapes_buyer_name(self):
        import main as app
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import SendMessage

        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(str(Path(tmp) / "test.sqlite3"))
            try:
                db.register_user(123, "buyer", "Buyer <script>")
                db.create_invoice("pay-1", "order-1", 123, 456, 600, "Счёт & услуга")
                db.set_invoice_status("pay-1", "paid")
                sender = AsyncMock(side_effect=[
                    TelegramBadRequest(
                        method=SendMessage(chat_id=-10055, text="test"),
                        message="chat not found",
                    ),
                    None,
                ])
                with patch.object(app, "db", db), patch.object(app, "moderation_chat_id", return_value=-10055):
                    with patch.object(app, "safe_bot_send_message", sender):
                        await app.flush_payment_group_notifications(AsyncMock())
                        self.assertEqual(len(db.pending_payment_notifications()), 1)
                        await app.flush_payment_group_notifications(AsyncMock())
                        self.assertEqual(len(db.pending_payment_notifications()), 0)
                self.assertEqual(sender.await_count, 2)
                text = sender.await_args.args[2]
                self.assertIn("Buyer &lt;script&gt;", text)
                self.assertIn("@buyer", text)
                self.assertIn("123", text)
                self.assertIn("600 ₽", text)
                self.assertIn("Счёт &amp; услуга", text)
            finally:
                db.close()

    async def test_admin_page_shows_payment_identity_and_filters(self):
        import main as app

        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(str(Path(tmp) / "test.sqlite3"))
            try:
                db.register_user(42, "hello", "Мария")
                db.create_invoice("payment-42", "order-42", 42, 42, 220, "Услуга")
                db.set_invoice_status("payment-42", "paid")
                with patch.object(app, "db", db):
                    page = app.render_payments_page("paid")
                    self.assertIn("Мария", page)
                    self.assertIn("@hello", page)
                    self.assertIn("<code>42</code>", page)
                    self.assertIn("220 ₽", page)
                    self.assertIn("payment-42", page)
                    self.assertIn("Не оплачено: <b>0</b>", page)
                    self.assertIn("gpay:unpaid:0", str(app.payments_page_keyboard("paid").model_dump()))
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
