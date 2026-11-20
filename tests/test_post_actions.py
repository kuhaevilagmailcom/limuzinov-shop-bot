"""Regression coverage: channel links, opt-in privacy and paid action persistence."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from post_lookup import InvalidPostLink, parse_post_link
from storage import Storage


class ChannelPostLinkTests(unittest.TestCase):
    def test_private_channel_post_link(self):
        self.assertEqual(
            parse_post_link("https://t.me/c/1234567890/456?single"),
            ("-1001234567890", 456),
        )

    def test_public_channel_post_link(self):
        self.assertEqual(
            parse_post_link("https://t.me/My_Channel/456"),
            ("@My_Channel", 456),
        )
        self.assertEqual(
            parse_post_link("https://t.me/s/My_Channel/456"),
            ("@My_Channel", 456),
        )

    def test_rejects_invalid_urls(self):
        for url in (
            "https://evil.example/c/1234567890/123",
            "https://t.me.evil.example/c/1234567890/123",
            "http://t.me/c/1234567890/123",
            "https://t.me/c/1234567890",
            "https://t.me/c/abc/123",
            "https://t.me/c/1234567890/0",
            "https://t.me/share/url?url=hello",
            "https://t.me/My_Channel/not-a-message",
        ):
            with self.subTest(url=url), self.assertRaises(InvalidPostLink):
                parse_post_link(url)


class PostActionStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "test.sqlite3")
        self.db = Storage(self.path)
        self.db.register_user(105, "Poster105", "Post Author")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def create_public_post(self, message_id=601):
        sid, created = self.db.add(
            user_id=105, full_name="Post Author", username="Poster105",
            message_id=message_id, album_key=None, quote_text="Post",
        )
        self.assertTrue(created)
        self.db.move(sid, "collecting", "publishing")
        self.db.set_publication(sid, -1001234567890, [800, 801, 802])
        self.db.move(sid, "publishing", "published")
        return sid

    def test_lookup_album_parts_and_footer_in_channel(self):
        sid = self.create_public_post()
        for message_id in (800, 801, 802):
            row = self.db.find_published_submission(-1001234567890, message_id)
            self.assertEqual(row["id"], sid)
        self.assertIsNone(self.db.find_published_submission(-1008888888888, 801))
        self.assertIsNone(self.db.find_published_submission(-1001234567890, 999))

    def test_opt_in_is_explicit_not_retroactive_and_can_be_revoked(self):
        old_post = self.create_public_post(601)
        self.assertFalse(self.db.get(old_post)["author_disclosure_consent"])
        self.db.set_author_disclosure(105, True)
        new_post = self.create_public_post(602)
        self.assertTrue(self.db.get(new_post)["author_disclosure_consent"])
        self.assertFalse(self.db.get(old_post)["author_disclosure_consent"])
        self.db.set_author_disclosure(105, False)
        self.assertFalse(self.db.author_disclosure_enabled(105))

    def test_pending_payments_keep_both_actions_and_buyer(self):
        sid = self.create_public_post()
        self.db.create_removal_payment(
            "payment-delete", "postrm-1-a", sid, 300, 100,
            action="post_removal",
        )
        self.db.create_removal_payment(
            "payment-info", "postinfo-1-b", sid, 301, 100,
            action="author_lookup",
        )
        pending = {row["payment_id"]: row for row in self.db.pending_removal_payments()}
        self.assertEqual(pending["payment-delete"]["action"], "post_removal")
        self.assertEqual(pending["payment-info"]["action"], "author_lookup")
        self.assertEqual(pending["payment-info"]["buyer_user_id"], 301)
        self.assertEqual(pending["payment-info"]["username"], "Poster105")

    def test_action_selection_survives_restart_and_expires(self):
        self.db.set_post_action(300, "author_lookup")
        self.db.close()
        self.db = Storage(self.path)
        self.assertEqual(self.db.get_post_action(300), "author_lookup")
        with patch("storage.time.time", return_value=time.time() + 16 * 60):
            self.assertIsNone(self.db.get_post_action(300))
