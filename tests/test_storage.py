import tempfile
import unittest
from pathlib import Path

from storage import Storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(str(Path(self.tmp.name) / "test.sqlite3"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_single_and_double_action(self):
        sid, new = self.db.add(
            user_id=1, full_name="User", username="tester",
            message_id=10, album_key=None, quote_text="Original suggestion",
        )
        self.assertTrue(new)
        self.assertEqual(self.db.message_ids(sid), [10])
        self.assertEqual(self.db.get(sid)["quote_text"], "Original suggestion")
        self.assertTrue(self.db.move(sid, "collecting", "notifying"))
        self.db.set_card_pending(sid, 99, admin_id=11)
        self.assertEqual(self.db.count_pending(), 1)
        self.assertEqual(self.db.admin_cards(sid), [(11, 99)])
        self.assertTrue(self.db.move(sid, "pending", "publishing"))
        self.assertFalse(self.db.move(sid, "pending", "publishing"))
        self.assertTrue(self.db.move(sid, "publishing", "published"))

    def test_album(self):
        sid, _ = self.db.add(
            user_id=1, full_name="User", username=None,
            message_id=20, album_key="abc", quote_text="[Фото]",
        )
        same, _ = self.db.add(
            user_id=1, full_name="User", username=None,
            message_id=19, album_key="abc", quote_text="Album caption",
        )
        self.assertEqual(sid, same)
        self.assertEqual(self.db.message_ids(sid), [19, 20])
        self.assertEqual(self.db.get(sid)["quote_text"], "Album caption")
        self.assertEqual(
            self.db.add(
                user_id=1, full_name="User", username=None,
                message_id=20, album_key="abc",
            ),
            (sid, False),
        )
        self.db.move(sid, "collecting", "notifying")
        with self.assertRaises(ValueError):
            self.db.add(
                user_id=1, full_name="User", username=None,
                message_id=21, album_key="abc",
            )

    def test_retry_and_settings(self):
        sid, _ = self.db.add(
            user_id=1, full_name="A", username=None,
            message_id=10, album_key=None,
        )
        self.assertEqual(self.db.recoverable_ids(), [sid])
        self.db.move(sid, "collecting", "notifying")
        self.db.move(sid, "notifying", "delivery_failed")
        self.assertEqual(self.db.failed_ids(), [sid])
        self.db.set_setting("channel_id", "-100123456789")
        self.assertEqual(self.db.get_setting("channel_id"), "-100123456789")

    def test_blocked_users(self):
        self.assertFalse(self.db.is_blocked(123))
        self.db.block_user(123, admin_id=11)
        self.assertTrue(self.db.is_blocked(123))
        self.db.block_user(123, admin_id=22)
        self.assertTrue(self.db.is_blocked(123))


if __name__ == "__main__":
    unittest.main()
