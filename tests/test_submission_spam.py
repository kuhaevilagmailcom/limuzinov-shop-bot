"""Regression tests for the five-minute duplicate-post limit."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from storage import Storage
import main as app


class SpamLimitStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "submissions.sqlite3")
        self.db = Storage(self.path)
        self.message_id = 0

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def post(self, user=1, text="Привет", album=None, parts=None):
        if parts is None:
            parts = [text]
        sub_id = None
        for part in parts:
            self.message_id += 1
            sub_id, _ = self.db.add(
                user_id=user, full_name="Sender", username=None,
                message_id=self.message_id,
                album_key=album,
                content_type="text" if album is None else "photo",
                content_text=part,
                content_fingerprint=part,
            )
        return sub_id

    def test_only_fourth_identical_post_triggers_five_minute_pause(self):
        for _ in range(3):
            self.assertEqual(self.db.submission_spam_delay(self.post(), now=1000), 0)
        fourth = self.post()
        self.assertEqual(self.db.submission_spam_delay(fourth, now=1000), 300)
        self.assertEqual(self.db.submission_spam_delay(self.post(text="Другое"), now=1120), 180)
        self.assertEqual(self.db.submission_spam_delay(self.post(text="Другое"), now=1301), 0)

    def test_different_users_and_posts_have_separate_counts(self):
        for _ in range(3):
            self.assertEqual(self.db.submission_spam_delay(self.post(1, "same"), now=1000), 0)
        self.assertEqual(self.db.submission_spam_delay(self.post(1, "different"), now=1000), 0)
        self.assertEqual(self.db.submission_spam_delay(self.post(2, "same"), now=1000), 0)
        self.assertEqual(self.db.submission_spam_delay(self.post(1, "same"), now=1000), 300)

    def test_album_counts_as_one_post_and_media_identity_matters(self):
        for n in range(3):
            album_id = f"album-{n}"
            sid = self.post(album=album_id, parts=["photo-id-1", "photo-id-2"])
            self.assertEqual(len(self.db.items_for_submission(sid)), 2)
            self.assertEqual(self.db.submission_spam_delay(sid, now=1000), 0)
        fourth = self.post(album="album-3", parts=["photo-id-1", "photo-id-2"])
        self.assertEqual(self.db.submission_spam_delay(fourth, now=1000), 300)

    def test_counter_survives_database_restart(self):
        for _ in range(3):
            self.assertEqual(self.db.submission_spam_delay(self.post(), now=1000), 0)
        self.db.close()
        self.db = Storage(self.path)
        self.assertEqual(self.db.submission_spam_delay(self.post(), now=1000), 300)
        self.db.close()
        self.db = Storage(self.path)
        self.assertEqual(self.db.submission_spam_delay(self.post(), now=1001), 299)

    def test_old_posts_outside_window_do_not_count(self):
        for n in range(3):
            self.assertEqual(self.db.submission_spam_delay(self.post(), now=1000 + n), 0)
        self.assertEqual(self.db.submission_spam_delay(self.post(), now=1303), 0)


class ContentIdentityTests(unittest.TestCase):
    def test_text_normalizes_whitespace_and_case(self):
        first = SimpleNamespace(text=" Привет  МИР\n", content_type="text")
        second = SimpleNamespace(text="привет мир", content_type="text")
        self.assertEqual(app.content_identity(first), app.content_identity(second))

    def test_file_identity_distinguishes_different_photos(self):
        def photo(unique_id, caption="Привет"):
            return SimpleNamespace(
                text=None, caption=caption, content_type="photo",
                photo=[SimpleNamespace(file_unique_id=unique_id)],
            )
        self.assertEqual(app.content_identity(photo("file-1")), app.content_identity(photo("file-1")))
        self.assertNotEqual(app.content_identity(photo("file-1")), app.content_identity(photo("file-2")))
        self.assertNotEqual(app.content_identity(photo("file-1")), app.content_identity(photo("file-1", "Иное")))


class SpamSubmissionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fourth_post_never_reaches_moderation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(str(Path(tmp) / "bot.sqlite3"))
            bot = AsyncMock()
            try:
                for n in range(4):
                    sid, _ = db.add(
                        user_id=333, full_name="Sender", username=None,
                        message_id=n + 1, album_key=None,
                        content_type="text", content_fingerprint="same post",
                    )
                    with patch.object(app, "db", db), \
                         patch.object(app, "moderation_chat_id", return_value=-100111), \
                         patch.object(app, "copy_submission", new=AsyncMock(return_value=([200], None))) as copy, \
                         patch.object(app, "update_moderation_card", new=AsyncMock()), \
                         patch.object(app, "safe_notify", new=AsyncMock()) as notify:
                        await app.send_to_moderation_group(bot, sid)
                    if n < 3:
                        copy.assert_awaited_once()
                        self.assertEqual(db.get(sid)["status"], "pending")
                    else:
                        copy.assert_not_awaited()
                        self.assertEqual(db.get(sid)["status"], "spam_rejected")
                        self.assertIn("Без спама, подождите 5 минут", notify.await_args.args[2])
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
