import tempfile
import unittest
from pathlib import Path

from storage import Storage, resolve_database_path


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
        self.db.set_card_pending(sid, 99)
        self.assertEqual(self.db.get(sid)["status"], "pending")
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
        self.assertEqual(self.db.get(sid)["status"], "delivery_failed")
        self.db.set_setting("channel_id", "-100123456789")
        self.assertEqual(self.db.get_setting("channel_id"), "-100123456789")

    def test_blocked_users(self):
        self.assertFalse(self.db.is_blocked(123))
        self.db.block_user(123, blocked_by=11)
        self.assertTrue(self.db.is_blocked(123))
        self.db.block_user(123, blocked_by=22)
        self.assertTrue(self.db.is_blocked(123))
        self.assertTrue(self.db.unblock_user(123))
        self.assertFalse(self.db.is_blocked(123))
        self.assertFalse(self.db.unblock_user(123))

    def test_post_removal_target_and_payment_are_persistent(self):
        sid, _ = self.db.add(
            user_id=1, full_name="User", username=None,
            message_id=30, album_key=None,
        )
        self.db.move(sid, "collecting", "published")
        token = self.db.ensure_removal_token(sid)
        self.db.set_publication(sid, -100123, [501, 502])
        target = self.db.removal_target(sid, token)
        self.assertEqual(target["published_chat_id"], "-100123")
        self.assertEqual(target["published_message_ids"], "[501, 502]")
        self.db.create_removal_payment("pay-1", "postrm-1-x", sid, 77, 100)
        self.assertEqual(len(self.db.pending_removal_payments()), 1)
        self.db.set_removal_payment_status("pay-1", "paid")
        self.db.mark_removed(sid)
        self.assertIsNotNone(self.db.get(sid)["removed_at"])
        self.assertEqual(self.db.pending_removal_payments(), [])

    def test_historical_publication_import_is_idempotent(self):
        sid, created = self.db.import_published_submission(
            user_id=777,
            full_name="Старый автор",
            username="old_author",
            published_chat_id=-100123,
            published_message_ids=[902, 901, 902],
            quote_text="Старый пост",
            published_at="2025-01-02 03:04:05",
            removal_token="legacy-token",
            legacy_submission_id=55,
        )
        self.assertTrue(created)
        restored = self.db.find_published_submission(-100123, 901)
        self.assertEqual(restored["id"], sid)
        self.assertEqual(restored["user_id"], 777)
        self.assertEqual(restored["published_message_ids"], "[901, 902]")
        self.assertEqual(restored["author_disclosure_consent"], 1)

        same_sid, created_again = self.db.import_published_submission(
            user_id=777,
            full_name="Старый автор",
            username="old_author",
            published_chat_id=-100123,
            published_message_ids=[901, 902],
        )
        self.assertFalse(created_again)
        self.assertEqual(same_sid, sid)


    def test_unpaid_published_post_remains_payable_after_reopen(self):
        sid, _ = self.db.add(
            user_id=19, full_name="Poster", username=None, message_id=88,
            album_key=None, quote_text="Persistent post",
        )
        self.db.move(sid, "collecting", "publishing")
        token = self.db.ensure_removal_token(sid)
        self.db.set_publication(sid, "-100987", [801, 802])
        self.db.move(sid, "publishing", "published")

        self.assertIsNotNone(self.db.removal_target(sid, token))
        self.db.close()
        self.db = Storage(str(Path(self.tmp.name) / "test.sqlite3"))
        self.assertIsNotNone(self.db.removal_target(sid, token))
        self.assertIsNone(self.db.removal_target(sid, "wrong-token"))
        # A pending or abandoned payment does not invalidate the post's link.
        self.db.create_removal_payment("pay-pending", "postrm-99-abc", sid, 55, 100)
        self.assertIsNotNone(self.db.removal_target(sid, token))

    def test_removal_link_still_valid_when_publication_state_is_uncertain(self):
        sid, _ = self.db.add(
            user_id=19, full_name="Poster", username=None, message_id=90,
            album_key=None,
        )
        self.db.move(sid, "collecting", "publishing")
        token = self.db.ensure_removal_token(sid)
        self.db.set_publication(sid, "-100987", [900])
        self.db.move(sid, "publishing", "publish_uncertain")
        self.assertIsNotNone(self.db.removal_target(sid, token))


class PersistentDatabasePathTests(unittest.TestCase):
    def test_migrates_existing_legacy_database_to_persistent_data_dir(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "suggestions.sqlite3"
            original = Storage(str(old))
            sid, _ = original.add(
                user_id=100, full_name="Poster", username=None,
                message_id=400, album_key=None,
            )
            original.move(sid, "collecting", "published")
            token = original.ensure_removal_token(sid)
            original.set_publication(sid, -100123, [700])
            original.close()

            persistent = root / "data"
            path = resolve_database_path(root, data_dir=str(persistent))
            self.assertEqual(path, str(persistent / "suggestions.sqlite3"))
            restored = Storage(path)
            self.assertIsNotNone(restored.removal_target(sid, token))
            restored.close()

            # A second startup uses the persistent database, not a stale legacy copy.
            path_again = resolve_database_path(root, data_dir=str(persistent))
            self.assertEqual(path_again, path)
            reopened = Storage(path_again)
            self.assertIsNotNone(reopened.removal_target(sid, token))
            reopened.close()

    def test_explicit_database_path_remains_authoritative(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configured = str(root / "custom.sqlite3")
            self.assertEqual(
                resolve_database_path(
                    root, configured_path=configured, data_dir=str(root / "data")
                ),
                configured,
            )



if __name__ == "__main__":
    unittest.main()
