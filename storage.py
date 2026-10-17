"""SQLite storage for submissions, moderation state, and blocked users."""
from __future__ import annotations

import sqlite3
from pathlib import Path


class Storage:
    def __init__(self, db_path: str):
        Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=20, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=20000")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                username TEXT,
                album_key TEXT,
                status TEXT NOT NULL DEFAULT 'collecting',
                admin_card_id INTEGER,
                quote_text TEXT NOT NULL DEFAULT '',
                keyboard_cleaned INTEGER NOT NULL DEFAULT 0,
                reviewed_by_id INTEGER,
                reviewed_by_name TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, album_key)
            );
            CREATE TABLE IF NOT EXISTS items (
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                content_type TEXT NOT NULL DEFAULT '',
                content_text TEXT NOT NULL DEFAULT '',
                entities_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (user_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS items_sub_id_idx ON items(submission_id);
            CREATE INDEX IF NOT EXISTS submissions_status_idx ON submissions(status);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY,
                blocked_by INTEGER NOT NULL,
                blocked_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)

        # Add fields when opening databases made by the earlier direct-publication version.
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(submissions)")}
        if "admin_card_id" not in columns:
            self.conn.execute("ALTER TABLE submissions ADD COLUMN admin_card_id INTEGER")
        if "quote_text" not in columns:
            self.conn.execute("ALTER TABLE submissions ADD COLUMN quote_text TEXT NOT NULL DEFAULT ''")
        if "keyboard_cleaned" not in columns:
            self.conn.execute(
                "ALTER TABLE submissions ADD COLUMN keyboard_cleaned INTEGER NOT NULL DEFAULT 0"
            )
        if "reviewed_by_id" not in columns:
            self.conn.execute("ALTER TABLE submissions ADD COLUMN reviewed_by_id INTEGER")
        if "reviewed_by_name" not in columns:
            self.conn.execute("ALTER TABLE submissions ADD COLUMN reviewed_by_name TEXT")

        item_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
        if "content_type" not in item_columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN content_type TEXT NOT NULL DEFAULT ''")
        if "content_text" not in item_columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN content_text TEXT NOT NULL DEFAULT ''")
        if "entities_json" not in item_columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN entities_json TEXT NOT NULL DEFAULT '[]'")

    def add(
        self,
        *,
        user_id: int,
        full_name: str,
        username: str | None,
        message_id: int,
        album_key: str | None,
        quote_text: str = "",
        content_type: str = "",
        content_text: str = "",
        entities_json: str = "[]",
    ) -> tuple[int, bool]:
        """Add a message and return (submission id, newly created submission)."""
        with self.conn:
            existed = self.conn.execute(
                "SELECT submission_id FROM items WHERE user_id=? AND message_id=?",
                (user_id, message_id),
            ).fetchone()
            if existed:
                return int(existed["submission_id"]), False

            if album_key:
                existing = self.conn.execute(
                    "SELECT id, status FROM submissions WHERE user_id=? AND album_key=?",
                    (user_id, album_key),
                ).fetchone()
                if existing:
                    if existing["status"] != "collecting":
                        raise ValueError("Media group was already submitted")
                    submission_id, fresh = int(existing["id"]), False
                else:
                    cur = self.conn.execute(
                        "INSERT INTO submissions(user_id,full_name,username,album_key,quote_text) "
                        "VALUES (?,?,?,?,?)",
                        (user_id, full_name, username, album_key, quote_text),
                    )
                    submission_id, fresh = int(cur.lastrowid), True
            else:
                cur = self.conn.execute(
                    "INSERT INTO submissions(user_id,full_name,username,album_key,quote_text) "
                    "VALUES (?,?,?,NULL,?)",
                    (user_id, full_name, username, quote_text),
                )
                submission_id, fresh = int(cur.lastrowid), True

            self.conn.execute(
                "INSERT INTO items("
                "submission_id,user_id,message_id,content_type,content_text,entities_json"
                ") VALUES (?,?,?,?,?,?)",
                (
                    submission_id,
                    user_id,
                    message_id,
                    content_type,
                    content_text,
                    entities_json,
                ),
            )
            if quote_text and quote_text.strip():
                self.conn.execute(
                    "UPDATE submissions SET quote_text=? "
                    "WHERE id=? AND (quote_text='' OR quote_text LIKE '[%')",
                    (quote_text, submission_id),
                )
            return submission_id, fresh

    def get(self, submission_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM submissions WHERE id=?", (submission_id,)
        ).fetchone()

    def message_ids(self, submission_id: int) -> list[int]:
        return [
            int(row["message_id"])
            for row in self.conn.execute(
                "SELECT message_id FROM items WHERE submission_id=? ORDER BY message_id ASC",
                (submission_id,),
            )
        ]

    def items_for_submission(self, submission_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM items WHERE submission_id=? ORDER BY message_id ASC",
                (submission_id,),
            )
        )

    def move(
        self,
        submission_id: int,
        from_status: str,
        to_status: str,
        *,
        reviewed_by_id: int | None = None,
        reviewed_by_name: str | None = None,
    ) -> bool:
        with self.conn:
            if reviewed_by_id is None and reviewed_by_name is None:
                cur = self.conn.execute(
                    "UPDATE submissions SET status=? WHERE id=? AND status=?",
                    (to_status, submission_id, from_status),
                )
            else:
                cur = self.conn.execute(
                    "UPDATE submissions SET status=?, reviewed_by_id=?, reviewed_by_name=? "
                    "WHERE id=? AND status=?",
                    (to_status, reviewed_by_id, reviewed_by_name, submission_id, from_status),
                )
            return cur.rowcount == 1

    def set_card_pending(self, submission_id: int, card_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE submissions SET admin_card_id=?, "
                "status=CASE WHEN status='notifying' THEN 'pending' ELSE status END "
                "WHERE id=?",
                (card_id, submission_id),
            )

    def is_blocked(self, user_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM blocked_users WHERE user_id=?", (user_id,)
        ).fetchone() is not None

    def block_user(self, user_id: int, blocked_by: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO blocked_users(user_id,blocked_by) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET blocked_by=excluded.blocked_by, "
                "blocked_at=datetime('now')",
                (user_id, blocked_by),
            )

    def unblock_user(self, user_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM blocked_users WHERE user_id=?", (user_id,)
            )
            return cur.rowcount == 1

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def cards_needing_keyboard_cleanup(self) -> list[sqlite3.Row]:
        """Return non-pending moderation cards whose old inline keyboards have not been checked."""
        return list(
            self.conn.execute(
                "SELECT id, admin_card_id FROM submissions "
                "WHERE admin_card_id IS NOT NULL AND status != 'pending' "
                "AND keyboard_cleaned=0 ORDER BY id ASC"
            )
        )

    def mark_keyboard_cleaned(self, submission_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE submissions SET keyboard_cleaned=1 WHERE id=?",
                (submission_id,),
            )

    def recoverable_ids(self) -> list[int]:
        return [
            int(row["id"])
            for row in self.conn.execute(
                "SELECT id FROM submissions WHERE status='collecting' ORDER BY id ASC"
            )
        ]

    def close(self) -> None:
        self.conn.close()
