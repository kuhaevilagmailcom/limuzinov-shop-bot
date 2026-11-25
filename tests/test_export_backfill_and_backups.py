import tempfile
import unittest
from pathlib import Path

from channel_export_backfill import load_export
from post_lookup import publication_text_fingerprint
from storage import Storage, create_database_backup, resolve_database_path


class ExportBackfillAndBackupTests(unittest.TestCase):
    def test_html_export_restores_author_and_accepted_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "messages.html").write_text(
                """<div class="message default clearfix" id="message42">
                <div class="body"><div class="text">Тестовый пост<br><br>━━━━━━━━━━━━━━<br>
                👤 Отправитель: <a href="https://t.me/test_user">@test_user</a> · ID 123456789<br>
                👤 Имя: Test User<br>⏲ Отправлено: 25.11.2026 18:56<br>
                ✅ Заявка принята и опубликована</div></div></div>""",
                encoding="utf-8",
            )
            users, posts = load_export(root)
        self.assertEqual(users[123456789].username, "test_user")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].content, "Тестовый пост")
        self.assertEqual(posts[0].fingerprint, publication_text_fingerprint("Тестовый пост"))

    def test_primary_database_is_restored_from_latest_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            path = resolve_database_path(root, data_dir=str(data))
            db = Storage(path)
            db.register_user(77, "saved", "Saved User")
            db.close()
            backup = create_database_backup(path, label="test")
            self.assertIsNotNone(backup)
            Path(path).unlink()
            restored_path = resolve_database_path(root, data_dir=str(data))
            restored = Storage(restored_path)
            try:
                self.assertEqual(restored.user_count(), 1)
            finally:
                restored.close()

    def test_explicit_database_path_is_restored_from_latest_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = root / "data" / "custom.sqlite3"
            db = Storage(str(configured))
            db.register_user(88, "explicit", "Explicit Path")
            db.close()
            backup = create_database_backup(configured, label="test")
            self.assertIsNotNone(backup)
            configured.unlink()
            restored_path = resolve_database_path(root, configured_path=str(configured))
            restored = Storage(restored_path)
            try:
                self.assertEqual(restored.user_count(), 1)
            finally:
                restored.close()

    def test_exported_post_is_idempotent_and_lazily_binds_real_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Storage(str(Path(tmp) / "test.sqlite3"))
            fingerprint = publication_text_fingerprint("Старый пост")
            try:
                first, created = db.import_published_submission(
                    user_id=12,
                    full_name="Poster",
                    username="poster",
                    published_chat_id=-1001358443115,
                    published_message_ids=[],
                    quote_text="Старый пост",
                    publication_fingerprint=fingerprint,
                )
                second, created_again = db.import_published_submission(
                    user_id=12,
                    full_name="Poster",
                    username="poster",
                    published_chat_id=-1001358443115,
                    published_message_ids=[],
                    quote_text="Старый пост",
                    publication_fingerprint=fingerprint,
                )
                self.assertEqual((first, created), (second, True))
                self.assertFalse(created_again)
                found = db.find_published_submission_by_fingerprint(fingerprint)
                self.assertEqual(int(found["id"]), first)
                db.attach_publication_message(first, -1001358443115, 9001)
                self.assertEqual(int(db.find_published_submission(-1001358443115, 9001)["id"]), first)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
