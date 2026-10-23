"""Regression tests for replying to suggestions after the moderation decision."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main as app
from storage import Storage


def actions(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class ProcessedSuggestionKeyboardTests(unittest.TestCase):
    def test_processed_cards_keep_reply_action(self):
        for status in ("published", "rejected", "blocked", "publish_uncertain"):
            with self.subTest(status=status):
                for custom_icons in (True, False):
                    markup = app.status_buttons(42, status, custom_icons=custom_icons)
                    self.assertIn("mod:reply:42", actions(markup))
                    self.assertIn("mod:status:42", actions(markup))
                    self.assertNotIn("mod:yes:42", actions(markup))
                    self.assertNotIn("mod:no:42", actions(markup))

    def test_cleanup_version_resets_processed_cards_for_keyboard_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(str(Path(tmp) / "test.sqlite3"))
            try:
                sub_id, _ = db.add(
                    user_id=7, full_name="User", username=None,
                    message_id=88, album_key=None,
                )
                self.assertTrue(db.move(sub_id, "collecting", "notifying"))
                db.set_card_pending(sub_id, 321)
                self.assertTrue(db.move(sub_id, "pending", "rejected"))
                db.mark_keyboard_cleaned(sub_id)
                self.assertEqual(db.get(sub_id)["keyboard_cleaned"], 1)
                db.prepare_keyboard_cleanup_version("5")
                self.assertEqual(db.get(sub_id)["keyboard_cleaned"], 0)
                self.assertIn(sub_id, [row["id"] for row in db.cards_needing_keyboard_cleanup()])
            finally:
                db.close()


class ProcessedSuggestionReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        for moderator_id in list(app.reply_sessions):
            app.clear_reply_session(moderator_id)

    async def test_keyboard_refresh_keeps_reply_on_processed_messages(self):
        sub = {
            "id": 42,
            "admin_card_id": 321,
            "admin_card_mode": "content",
        }
        edit = AsyncMock()
        fake_db = SimpleNamespace(moderation_message_ids=lambda _id: [321, 322])
        with patch.object(app, "db", fake_db), \
             patch.object(app, "moderation_chat_id", return_value=-100222), \
             patch.object(app, "safe_edit_reply_markup", edit):
            for status in ("published", "rejected"):
                with self.subTest(status=status):
                    edit.reset_mock()
                    self.assertTrue(await app.refresh_submission_keyboards(object(), sub, status))
                    self.assertEqual(edit.await_count, 2)
                    for call in edit.await_args_list:
                        self.assertIn("mod:reply:42", actions(call.args[3]))

    async def test_reply_callback_starts_after_accept_or_reject(self):
        fake_db = SimpleNamespace(
            get=lambda _id: {
                "id": 42, "user_id": 7, "admin_card_id": 321,
                "quote_text": "Test suggestion", "status": self.current_status,
            },
            moderation_message_ids=lambda _id: [321],
        )
        for status in ("published", "rejected"):
            with self.subTest(status=status):
                self.current_status = status
                callback = SimpleNamespace(
                    data="mod:reply:42",
                    message=SimpleNamespace(
                        chat=SimpleNamespace(id=-100222),
                        message_id=321,
                    ),
                    from_user=SimpleNamespace(id=999),
                    answer=AsyncMock(),
                )
                prompt_send = AsyncMock(return_value=SimpleNamespace(message_id=555))
                with patch.object(app, "db", fake_db), \
                     patch.object(app, "moderation_chat_id", return_value=-100222), \
                     patch.object(app, "safe_bot_send_message", prompt_send):
                    await app.moderate(callback, object())
                self.assertEqual(app.reply_sessions[999]["sub_id"], 42)
                self.assertEqual(app.reply_sessions[999]["user_id"], 7)
                prompt_send.assert_awaited_once()
                self.assertIn("Ответ открыт", callback.answer.await_args.args[0])
                app.clear_reply_session(999)

    async def test_group_reply_is_delivered_after_decision(self):
        for status in ("published", "rejected"):
            with self.subTest(status=status):
                app.reply_sessions[999] = {
                    "sub_id": 42, "user_id": 7,
                    "prompt_message_id": 555,
                    "deadline": asyncio.get_running_loop().time() + 100,
                }
                message = SimpleNamespace(
                    from_user=SimpleNamespace(id=999),
                    chat=SimpleNamespace(id=-100222),
                    reply_to_message=SimpleNamespace(message_id=321),
                )
                fake_db = SimpleNamespace(
                    get=lambda _id: {
                        "id": 42, "status": status, "admin_card_id": 321,
                    },
                    moderation_message_ids=lambda _id: [321],
                )
                deliver = AsyncMock()
                with patch.object(app, "db", fake_db), \
                     patch.object(app, "moderation_chat_id", return_value=-100222), \
                     patch.object(app, "deliver_group_reply", deliver), \
                     patch.object(app, "edit_reply_prompt", AsyncMock()), \
                     patch.object(app, "safe_answer", AsyncMock()):
                    await app.group_reply_handler(message, object())
                deliver.assert_awaited_once()
                self.assertNotIn(999, app.reply_sessions)


if __name__ == "__main__":
    unittest.main()
