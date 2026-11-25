"""Safety and UX regressions for post action chat flows."""
import atexit
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_TEST_DIR = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"DB_PATH": str(Path(_TEST_DIR.name) / "bot.sqlite3")}):
    import main as app


def _cleanup():
    _TEST_DIR.cleanup()


atexit.register(_cleanup)


def fake_message(text="Удалить пост"):
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=111, full_name="Buyer", username="buyer", is_bot=False
        ),
        text=text,
        answer=AsyncMock(),
    )


def fake_submission(consent=0):
    return {
        "id": 99, "published_chat_id": "-1001234567890",
        "published_message_ids": "[700, 701]", "removed_at": None,
        "author_disclosure_consent": consent, "user_id": 333,
        "username": "Poster333", "full_name": "Poster <One>",
    }


class PaidPostBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_button_starts_link_session(self):
        msg = fake_message("Удалить пост")
        with patch.object(app.db, "register_user"):
            with patch.object(app.db, "set_post_action") as selected:
                await app.inbox(msg, AsyncMock())
        selected.assert_called_once_with(111, "post_removal")
        self.assertIn("https://t.me/c/", msg.answer.await_args.args[0])

    async def test_lookup_button_starts_link_session(self):
        msg = fake_message("Узнать, кто выложил")
        with patch.object(app.db, "register_user"):
            with patch.object(app.db, "set_post_action") as selected:
                await app.inbox(msg, AsyncMock())
        selected.assert_called_once_with(111, "author_lookup")

    async def test_rejects_a_link_from_another_channel(self):
        from post_lookup import InvalidPostLink

        with patch.object(app, "destination_chat_id", return_value=-1001234567890):
            with self.assertRaises(InvalidPostLink):
                await app.resolve_post_from_link(
                    AsyncMock(), "https://t.me/c/9999999999/700"
                )

    async def test_private_channel_post_resolves_to_stored_publication(self):
        bot = AsyncMock()
        stored = fake_submission()
        with patch.object(app, "destination_chat_id", return_value=-1001234567890):
            with patch.object(app, "destination_chat_value", return_value=-1001234567890):
                with patch.object(app.db, "find_published_submission", return_value=stored) as find:
                    post = await app.resolve_post_from_link(
                        bot, "https://t.me/c/1234567890/700"
                    )
        self.assertIs(post, stored)
        self.assertEqual(find.call_args.args, (-1001234567890, 700))
        bot.get_chat.assert_not_awaited()

    async def test_author_lookup_creates_payment_without_privacy_toggle(self):
        msg = fake_message()
        with patch.object(app, "ROLLYPAY_API_KEY", "test-api-key"):
            with patch.object(app, "ROLLYPAY_TERMINAL_ID", "test-terminal"):
                with patch.object(app, "create_payment", new=AsyncMock(return_value={
                    "payment_id": "payment-info-0", "pay_url": "https://example.test/pay"
                })) as create:
                    with patch.object(app.db, "create_removal_payment"):
                        success = await app.start_post_action_payment(
                            msg, fake_submission(consent=0), "author_lookup"
                        )
        self.assertTrue(success)
        self.assertTrue(create.await_args.kwargs["order_id"].startswith("postinfo-99-"))
        self.assertIn("<b>Инфа #99</b>", msg.answer.await_args.args[0])
        self.assertEqual(create.await_args.kwargs["description"], "Инфа #99")

    async def test_paid_author_invoice_is_separate_action(self):
        msg = fake_message()
        with patch.object(app, "ROLLYPAY_API_KEY", "test-api-key"):
            with patch.object(app, "ROLLYPAY_TERMINAL_ID", "test-terminal"):
                with patch.object(app, "create_payment", new=AsyncMock(return_value={
                    "payment_id": "payment-info-1", "pay_url": "https://example.test/pay"
                })) as create:
                    with patch.object(app.db, "create_removal_payment") as stored:
                        success = await app.start_post_action_payment(
                            msg, fake_submission(consent=1), "author_lookup"
                        )
        self.assertTrue(success)
        self.assertTrue(create.await_args.kwargs["order_id"].startswith("postinfo-99-"))
        self.assertEqual(
            create.await_args.kwargs["success_url"], "https://t.me/PozorMgnBot"
        )
        self.assertEqual(stored.call_args.kwargs["action"], "author_lookup")

    async def test_paid_author_delivery_escapes_markup_and_targets_buyer(self):
        bot = AsyncMock()
        details = {**fake_submission(consent=1), "buyer_user_id": 444}
        await app.deliver_author_info(bot, details)
        sent = bot.send_message.await_args
        self.assertEqual(sent.kwargs["chat_id"], 444)
        self.assertIn("Poster &lt;One&gt;", sent.kwargs["text"])
        self.assertIn("Poster333", sent.kwargs["text"])
        self.assertIn("<code>333</code>", sent.kwargs["text"])


    async def test_forwarded_media_resolves_original_channel_and_post(self):
        msg = fake_message(None)
        msg.forward_origin = SimpleNamespace(
            type="channel", chat=SimpleNamespace(id=-1001234567890), message_id=701
        )
        stored = fake_submission()
        with patch.object(app, "destination_chat_id", return_value=-1001234567890), \
             patch.object(app, "destination_chat_value", return_value=-1001234567890), \
             patch.object(app.db, "find_published_submission", return_value=stored) as find, \
             patch.object(app, "start_post_action_payment", new=AsyncMock(return_value=True)) as pay:
            self.assertTrue(await app.process_post_link(msg, AsyncMock(), "post_removal"))
        self.assertEqual(find.call_args.args, (-1001234567890, 701))
        pay.assert_awaited_once_with(msg, stored, "post_removal", from_forward=True)

    async def test_forwarded_post_from_another_channel_is_rejected(self):
        msg = fake_message(None)
        msg.forward_origin = SimpleNamespace(
            type="channel", chat=SimpleNamespace(id=-1009999999999), message_id=701
        )
        with patch.object(app, "destination_chat_id", return_value=-1001234567890), \
             patch.object(app, "start_post_action_payment", new=AsyncMock()) as pay:
            self.assertFalse(await app.process_post_link(msg, AsyncMock(), "post_removal"))
        pay.assert_not_awaited()

    async def test_forward_first_offers_numbered_actions(self):
        msg = fake_message(None)
        msg.forward_origin = SimpleNamespace(
            type="channel", chat=SimpleNamespace(id=-1001234567890), message_id=701
        )
        with patch.object(app, "resolve_post_target", new=AsyncMock(return_value=fake_submission(1))):
            self.assertTrue(await app.offer_forwarded_post_actions(msg, AsyncMock()))
        rows = msg.answer.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual([row[0].text for row in rows], ["Удаление поста #99", "Инфа #99"])

    async def test_forward_first_omits_info_without_consent(self):
        msg = fake_message(None)
        msg.forward_origin = SimpleNamespace(
            type="channel", chat=SimpleNamespace(id=-1001234567890), message_id=701
        )
        with patch.object(app, "resolve_post_target", new=AsyncMock(return_value=fake_submission(0))):
            self.assertTrue(await app.offer_forwarded_post_actions(msg, AsyncMock()))
        rows = msg.answer.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual([row[0].text for row in rows], ["Удаление поста #99"])

    async def test_forwarded_info_invoice_requires_consent(self):
        msg = fake_message()
        with patch.object(app, "create_payment", new=AsyncMock()) as create:
            self.assertFalse(await app.start_post_action_payment(
                msg, fake_submission(0), "author_lookup", from_forward=True
            ))
        create.assert_not_awaited()

    async def test_removal_title_matches_invoice(self):
        msg = fake_message()
        with patch.object(app, "ROLLYPAY_API_KEY", "test-key"), \
             patch.object(app, "ROLLYPAY_TERMINAL_ID", "test-terminal"), \
             patch.object(app, "create_payment", new=AsyncMock(return_value={
                 "payment_id": "payment-delete", "pay_url": "https://example.test/pay"
             })) as create, patch.object(app.db, "create_removal_payment"):
            self.assertTrue(await app.start_post_action_payment(msg, fake_submission(), "post_removal"))
        self.assertIn("<b>Удаление поста #99</b>", msg.answer.await_args.args[0])
        self.assertEqual(create.await_args.kwargs["description"], "Удаление поста #99")
