"""No money for unverified posts; admin-only MTProto old-post deletion."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mtproto_removal import (
    AdminDeletionUnavailable,
    admin_deletion_configured,
    delete_verified_target,
    find_deletion_target,
)
from storage import Storage


class OldPostStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(str(Path(self.tmp.name) / "posts.sqlite3"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_persists_external_post_invoice_after_restart(self):
        self.db.create_legacy_removal_payment(
            "pid", "oldpost-999-abc", 42, -1001234567890, [1000, 1001], 120
        )
        self.db.close()
        self.db = Storage(str(Path(self.tmp.name) / "posts.sqlite3"))
        payments = self.db.pending_legacy_removal_payments()
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0]["chat_id"], "-1001234567890")
        self.assertEqual(payments[0]["message_ids_json"], "[1000, 1001]")
        self.db.mark_legacy_removal_payment("pid", "paid")
        self.assertEqual(self.db.pending_legacy_removal_payments(), [])

    def test_refuses_unverified_empty_invoice(self):
        with self.assertRaises(ValueError):
            self.db.create_legacy_removal_payment(
                "pid", "oldpost-999-abc", 42, -1001234567890, [], 120
            )


class OldPostAdminTests(unittest.IsolatedAsyncioTestCase):
    def test_requires_explicit_secret_setup(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(admin_deletion_configured())

    async def test_album_identifies_related_messages(self):
        client = AsyncMock()
        root = SimpleNamespace(id=700, grouped_id=123, action=None)
        sibling = SimpleNamespace(id=701, grouped_id=123, action=None)
        other = SimpleNamespace(id=702, grouped_id=None, action=None)
        client.get_messages.side_effect = [root, [root, sibling, other]]
        with patch("mtproto_removal._connect", new=AsyncMock(return_value=client)):
            with patch("mtproto_removal._authorized_channel", new=AsyncMock(return_value="channel")):
                target = await find_deletion_target(-1001234567890, 700)
        self.assertEqual(target.message_ids, (700, 701))
        self.assertEqual(target.chat_id, -1001234567890)
        client.disconnect.assert_awaited_once()

    async def test_nonexistent_post_is_rejected_before_payment(self):
        client = AsyncMock()
        client.get_messages.return_value = None
        with patch("mtproto_removal._connect", new=AsyncMock(return_value=client)):
            with patch("mtproto_removal._authorized_channel", new=AsyncMock(return_value="channel")):
                with self.assertRaises(AdminDeletionUnavailable):
                    await find_deletion_target(-1001234567890, 700)

    async def test_deleted_old_messages_are_verified(self):
        client = AsyncMock()
        present = SimpleNamespace(id=700)
        client.get_messages.side_effect = [[present], [None]]
        with patch("mtproto_removal._connect", new=AsyncMock(return_value=client)):
            with patch("mtproto_removal._authorized_channel", new=AsyncMock(return_value="channel")):
                self.assertTrue(await delete_verified_target(-1001234567890, (700,)))
        client.delete_messages.assert_awaited_once_with("channel", [700], revoke=True)
