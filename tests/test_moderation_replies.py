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


class SwipeReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(str(Path(self.tmp.name) / "swipe.sqlite3"))

    def tearDown(self):
        for moderator_id in list(app.reply_sessions):
            app.clear_reply_session(moderator_id)
        self.db.close()
        self.tmp.cleanup()

    def make_submission(self, user_id, message_id, copied_id, card_id, status="pending"):
        sid, _ = self.db.add(
            user_id=user_id, full_name="Sender", username=None,
            message_id=message_id, album_key=None, content_type="text",
            content_text="Hello",
        )
        self.db.set_moderation_message_ids(sid, [copied_id])
        self.db.move(sid, "collecting", "notifying")
        self.db.set_card_pending(sid, card_id)
        if status != "pending":
            self.db.move(sid, "pending", status)
        return sid

    async def send_swipe(self, replied_id, *, moderator_id=999, chat_id=-100222, text="Answer"):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            from_user=SimpleNamespace(id=moderator_id, is_bot=False),
            reply_to_message=SimpleNamespace(message_id=replied_id),
            text=text,
        )
        with patch.object(app, "db", self.db), \
             patch.object(app, "moderation_chat_id", return_value=-100222), \
             patch.object(app, "deliver_group_reply", new=AsyncMock()) as deliver, \
             patch.object(app, "safe_answer", new=AsyncMock()) as notify, \
             patch.object(app, "edit_reply_prompt", new=AsyncMock()):
            await app.group_reply_handler(message, object())
        return deliver, notify

    async def test_swipe_without_button_works_before_and_after_decision(self):
        for index, status in enumerate(("pending", "published", "rejected", "blocked")):
            with self.subTest(status=status):
                sid = self.make_submission(
                    user_id=400 + index,
                    message_id=20 + index,
                    copied_id=300 + index,
                    card_id=500 + index,
                    status=status,
                )
                self.assertEqual(app.reply_sessions, {})
                deliver, notify = await self.send_swipe(300 + index)
                deliver.assert_awaited_once()
                self.assertEqual(deliver.await_args.args[2]["sub_id"], sid)
                self.assertEqual(deliver.await_args.args[2]["user_id"], 400 + index)
                notify.assert_awaited_once()

    async def test_swipe_on_control_card_or_any_album_item(self):
        sid, _ = self.db.add(
            user_id=700, full_name="Album user", username=None,
            message_id=61, album_key="album-1", content_type="photo",
        )
        self.db.add(
            user_id=700, full_name="Album user", username=None,
            message_id=62, album_key="album-1", content_type="photo",
        )
        self.db.set_moderation_message_ids(sid, [601, 602])
        self.db.move(sid, "collecting", "notifying")
        self.db.set_card_pending(sid, 603, "album_control")
        for replied_id in (601, 602, 603):
            with self.subTest(replied_id=replied_id):
                deliver, _ = await self.send_swipe(replied_id)
                deliver.assert_awaited_once()
                self.assertEqual(deliver.await_args.args[2]["user_id"], 700)

    async def test_swipe_to_other_post_ignores_open_reply_session(self):
        first = self.make_submission(801, 81, 811, 812)
        second = self.make_submission(901, 91, 911, 912)
        app.reply_sessions[999] = {
            "sub_id": first, "user_id": 801,
            "prompt_message_id": 9999,
            "deadline": asyncio.get_running_loop().time() + 300,
        }
        deliver, _ = await self.send_swipe(911)
        deliver.assert_awaited_once()
        self.assertEqual(deliver.await_args.args[2]["sub_id"], second)
        self.assertEqual(deliver.await_args.args[2]["user_id"], 901)
        self.assertEqual(app.reply_sessions[999]["sub_id"], first)

    async def test_unrelated_replies_and_commands_are_not_forwarded(self):
        self.make_submission(800, 80, 810, 820)
        for msg_id, chat_id, text_value in (
            (99999, -100222, "Not a suggestion"),
            (810, -100999, "Other group"),
            (810, -100222, "/cleanbuttons"),
        ):
            with self.subTest(msg_id=msg_id, chat_id=chat_id, text=text_value):
                deliver, notify = await self.send_swipe(
                    msg_id, chat_id=chat_id, text=text_value
                )
                deliver.assert_not_awaited()
                notify.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
