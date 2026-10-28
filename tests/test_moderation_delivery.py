"""Regression tests for sender details on oversized moderation messages."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main as app


class ModerationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_caption_limit_is_measured_in_telegram_utf16_units(self):
        sub = {
            "user_id": 7,
            "username": "sender",
            "full_name": "Sender Name",
            "created_at": "2026-10-28 12:00:00",
            "reviewed_by_id": None,
            "reviewed_by_name": None,
        }
        item = {
            "content_type": "photo",
            "content_text": "😀" * 600,
            "entities_json": "[]",
        }

        self.assertIsNone(app.moderation_body(sub, item, "pending"))

    async def test_long_photo_caption_gets_separate_sender_control_card(self):
        sub = {
            "id": 42,
            "user_id": 7,
            "username": "sender",
            "full_name": "Sender Name",
            "quote_text": "x" * 700,
            "created_at": "2026-10-28 12:00:00",
            "reviewed_by_id": None,
            "reviewed_by_name": None,
        }
        items = [{
            "message_id": 10,
            "content_type": "photo",
            "content_text": "x" * 1000,
            "entities_json": "[]",
        }]
        fake_db = SimpleNamespace(
            get=lambda _id: sub,
            items_for_submission=lambda _id: items,
            set_moderation_message_ids=lambda *_args: None,
        )
        bot = SimpleNamespace(
            copy_message=AsyncMock(return_value=SimpleNamespace(message_id=100)),
        )
        send_card = AsyncMock(return_value=SimpleNamespace(message_id=101))

        with patch.object(app, "db", fake_db), \
             patch.object(app, "safe_edit_reply_markup", AsyncMock()), \
             patch.object(app, "safe_bot_send_message", send_card):
            copied_ids, card_id = await app.copy_submission(
                bot, -100222, 42, moderation_controls=True
            )

        self.assertEqual(copied_ids, [100])
        self.assertEqual(card_id, 101)
        text = send_card.await_args.kwargs["text"]
        self.assertIn("Отправитель: @sender · ID 7", text)
        self.assertIn("Имя: Sender Name", text)



class PublicationDisclaimerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def bot_mock():
        return SimpleNamespace(
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
        )

    async def test_long_photo_caption_never_creates_separate_post(self):
        bot = self.bot_mock()
        await app.add_publication_disclaimer(
            bot, -100222, [100],
            [{"content_type": "photo", "content_text": "x" * 1000, "entities_json": "[]"}],
        )
        bot.edit_message_caption.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    async def test_fitting_photo_caption_is_updated_in_existing_post(self):
        bot = self.bot_mock()
        await app.add_publication_disclaimer(
            bot, -100222, [100],
            [{"content_type": "photo", "content_text": "Hello", "entities_json": "[]"}],
        )
        actual = bot.edit_message_caption.await_args.kwargs
        self.assertEqual(actual["message_id"], 100)
        self.assertEqual(actual["caption"].splitlines()[0], "Hello")
        self.assertIn(app.PUBLICATION_DISCLAIMER, actual["caption"])
        self.assertEqual(actual["caption_entities"][-1].type, "blockquote")
        bot.send_message.assert_not_awaited()

    async def test_long_text_and_sticker_never_create_followup_posts(self):
        for content_type, original in [("text", "😀" * 2040), ("sticker", "")]:
            with self.subTest(content_type=content_type):
                bot = self.bot_mock()
                await app.add_publication_disclaimer(
                    bot, -100222, [100],
                    [{"content_type": content_type, "content_text": original, "entities_json": "[]"}],
                )
                bot.edit_message_caption.assert_not_awaited()
                bot.edit_message_text.assert_not_awaited()
                bot.send_message.assert_not_awaited()

    async def test_failed_edit_never_creates_separate_post(self):
        bot = self.bot_mock()
        bot.edit_message_caption.side_effect = Exception("Telegram edit rejected")
        with patch.object(app, "TelegramAPIError", Exception):
            await app.add_publication_disclaimer(
                bot, -100222, [100],
                [{"content_type": "photo", "content_text": "", "entities_json": "[]"}],
            )
        bot.edit_message_caption.assert_awaited_once()
        bot.send_message.assert_not_awaited()

    async def test_album_uses_spare_caption_instead_of_creating_post(self):
        bot = self.bot_mock()
        await app.add_publication_disclaimer(
            bot, -100222, [100, 101], [
                {"content_type": "photo", "content_text": "x" * 1000, "entities_json": "[]"},
                {"content_type": "photo", "content_text": "", "entities_json": "[]"},
            ],
        )
        self.assertEqual(bot.edit_message_caption.await_args.kwargs["message_id"], 101)
        bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
