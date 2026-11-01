"""Regression tests for post attribution and administrator-only announcements."""
import atexit
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import MessageEntity

# Never import the production database when running the tests.
_TEST_DIR = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"DB_PATH": str(Path(_TEST_DIR.name) / "test.sqlite3")}):
    import main as app


def _cleanup():
    app.db.close()
    _TEST_DIR.cleanup()


atexit.register(_cleanup)


def entity_text(content, entity):
    utf16 = content.encode("utf-16-le")
    return utf16[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le")


class PostFooterTests(unittest.TestCase):
    def test_formatting_and_clickable_links_survive_unicode(self):
        original = "Привет 😃"
        existing = MessageEntity(type="bold", offset=0, length=6)
        content, entities = app.publication_content(original, [existing])
        self.assertEqual(entity_text(content, entities[0]), "Привет")
        self.assertEqual(entities[1].type, "blockquote")
        self.assertEqual(entity_text(content, entities[1]), app.PUBLICATION_DISCLAIMER)
        links = [entity for entity in entities if entity.type == "text_link"]
        self.assertEqual(len(links), 2)
        for (label, url), entity in zip(app.PUBLICATION_LINKS, links):
            self.assertEqual(entity_text(content, entity), label)
            self.assertEqual(entity.url, url)

    def test_moderation_keyboards_keep_review_actions_and_announcements(self):
        pending = app.buttons(123)
        processed = app.status_buttons(123, "published")
        for keyboard in (pending, processed):
            actions = {
                button.callback_data
                for row in keyboard.inline_keyboard
                for button in row
            }
            self.assertIn("mod:promo_wait:123", actions)
            self.assertIn("mod:promo_subscribe:123", actions)
        self.assertTrue(any(
            button.text == "Отправить" and button.callback_data == "mod:yes:123"
            for row in pending.inline_keyboard for button in row
        ))


class ModerationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_admin_can_use_stats(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=98765, type="private"),
            from_user=SimpleNamespace(id=42),
        )
        with patch.object(app, "is_moderation_admin", new=AsyncMock(return_value=True)):
            self.assertTrue(await app.can_view_moderation_stats(message, AsyncMock()))

    async def test_non_admin_receives_diagnostic_instead_of_silence(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=98765, type="private"),
            from_user=SimpleNamespace(id=43),
        )
        with patch.object(app, "is_moderation_admin", new=AsyncMock(return_value=False)):
            with patch.object(app, "safe_answer", new=AsyncMock()) as reply:
                self.assertFalse(await app.can_view_moderation_stats(message, AsyncMock()))
                reply.assert_awaited_once()
                self.assertIn("MODERATION_CHAT_ID", reply.await_args.args[1])


class ChannelAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app.broadcast_sessions.clear()

    async def asyncTearDown(self):
        app.broadcast_sessions.clear()

    async def test_confirm_posts_to_channel_not_database_users(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-777, type="supergroup"),
            message_id=13,
        )
        callback = SimpleNamespace(
            message=message,
            from_user=SimpleNamespace(id=1),
            data="broadcast:confirm",
            answer=AsyncMock(),
        )
        app.broadcast_sessions[1] = {
            "mode": "channel",
            "text": app.CHANNEL_PROMOS["promo_wait"],
            "confirm_message_id": 13,
        }
        with patch.object(app, "moderation_chat_id", return_value=-777):
            with patch.object(app, "destination_chat_id", return_value=-100111):
                with patch.object(app, "is_moderation_admin", new=AsyncMock(return_value=True)):
                    with patch.object(app, "safe_bot_send_message", new=AsyncMock()) as send:
                        with patch.object(app, "safe_edit_text", new=AsyncMock()):
                            with patch.object(app.db, "all_user_ids", side_effect=AssertionError("DM not allowed")):
                                await app.broadcast_callback(callback, AsyncMock())
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[1], -100111)
        self.assertNotIn(1, app.broadcast_sessions)

    async def test_non_admin_cannot_confirm(self):
        message = SimpleNamespace(chat=SimpleNamespace(id=-777), message_id=13)
        callback = SimpleNamespace(
            message=message,
            from_user=SimpleNamespace(id=1),
            data="broadcast:confirm",
            answer=AsyncMock(),
        )
        app.broadcast_sessions[1] = {
            "mode": "channel",
            "text": "hi",
            "confirm_message_id": 13,
        }
        with patch.object(app, "moderation_chat_id", return_value=-777):
            with patch.object(app, "is_moderation_admin", new=AsyncMock(return_value=False)):
                await app.broadcast_callback(callback, AsyncMock())
        self.assertIn(1, app.broadcast_sessions)
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])


if __name__ == "__main__":
    unittest.main()
