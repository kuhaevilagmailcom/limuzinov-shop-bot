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


if __name__ == "__main__":
    unittest.main()
