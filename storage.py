"""Durable SQLite storage for submissions, moderation cards, and blocked users."""
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
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, album_key)
            );
            CREATE TABLE IF NOT EXISTS items (
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS items_sub_id_idx ON items(submission_id);
            CREATE INDEX IF NOT EXISTS submissions_status_idx ON submissions(status);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_cards (
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                admin_id INTEGER NOT NULL,
                card_id INTEGER NOT NULL,
                PRIMARY KEY (submission_id, admin_id)
            );
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY,
                blocked_by INTEGER NOT NULL,
                blocked_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        # Migrate databases created by older bot versions without losing submissions.
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(submissions)")}
        if "quote_text" not in columns:
            self.conn.execute("ALTER TABLE submissions ADD COLUMN quote_text TEXT NOT NULL DEFAULT ''")

    def add(
        self,
        *,
        user_id: int,
        full_name: str,
        username: str | None,
        message_id: int,
        album_key: str | None,
        quote_text: str = "",
    ) -> tuple[int, bool]:
        """Add a message, returning (submission id, newly created submission).

        For albums, multiple Telegram updates share one submission.
        """
        with self.conn:
            existed = self.conn.execute(
                "SELECT submission_id FROM items WHERE user_id=? AND message_id=?",
                (user_id, message_id),
            ).fetchone()
            if existed:
                return existed["submission_id"], False

            if album_key:
                existing = self.conn.execute(
                    "SELECT id, status FROM submissions WHERE user_id=? AND album_key=?",
                    (user_id, album_key),
                ).fetchone()
                if existing:
                    # Do not attach messages arriving after publication/moderation.
                    if existing["status"] != "collecting":
                        raise ValueError("Media group was already submitted")
                    submission_id, new = existing["id"], False
                else:
                    cur = self.conn.execute(
                        "INSERT INTO submissions(user_id,full_name,username,album_key,quote_text) "
                        "VALUES (?,?,?,?,?)",
                        (user_id, full_name, username, album_key, quote_text),
                    )
                    submission_id, new = cur.lastrowid, True
            else:
                cur = self.conn.execute(
                    "INSERT INTO submissions(user_id,full_name,username,album_key,quote_text) "
                    "VALUES (?,?,?,NULL,?)",
                    (user_id, full_name, username, quote_text),
                )
                submission_id, new = cur.lastrowid, True

            self.conn.execute(
                "INSERT INTO items(submission_id,user_id,message_id) VALUES (?,?,?)",
                (submission_id, user_id, message_id),
            )
            # On an album, prefer a caption over a generic media placeholder.
            if quote_text and quote_text.strip():
                self.conn.execute(
                    "UPDATE submissions SET quote_text=? "
                    "WHERE id=? AND (quote_text='' OR quote_text LIKE '[%')",
                    (quote_text, submission_id),
                )
            return int(submission_id), new

    def get(self, submission_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM submissions WHERE id=?", (submission_id,)
        ).fetchone()

    def message_ids(self, submission_id: int) -> list[int]:
        return [
            row["message_id"]
            for row in self.conn.execute(
                "SELECT message_id FROM items WHERE submission_id=? ORDER BY message_id ASC",
                (submission_id,),
            )
        ]

    def move(self, submission_id: int, from_status: str, to_status: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE submissions SET status=? WHERE id=? AND status=?",
                (to_status, submission_id, from_status),
            )
            return cur.rowcount == 1

    def set_card_pending(
        self, submission_id: int, card_id: int, admin_id: int | None = None
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE submissions SET admin_card_id=COALESCE(admin_card_id, ?), "
                "status=CASE WHEN status='notifying' THEN 'pending' ELSE status END "
                "WHERE id=?",
                (card_id, submission_id),
            )
            if admin_id is not None:
                self.conn.execute(
                    "INSERT INTO admin_cards(submission_id,admin_id,card_id) VALUES (?,?,?) "
                    "ON CONFLICT(submission_id,admin_id) DO UPDATE SET card_id=excluded.card_id",
                    (submission_id, admin_id, card_id),
                )

    def admin_cards(self, submission_id: int) -> list[tuple[int, int]]:
        return [
            (row["admin_id"], row["card_id"])
            for row in self.conn.execute(
                "SELECT admin_id,card_id FROM admin_cards WHERE submission_id=? ORDER BY admin_id",
                (submission_id,),
            )
        ]

    def is_blocked(self, user_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM blocked_users WHERE user_id=?", (user_id,)
        ).fetchone() is not None

    def block_user(self, user_id: int, admin_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO blocked_users(user_id,blocked_by) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET blocked_by=excluded.blocked_by, "
                "blocked_at=datetime('now')",
                (user_id, admin_id),
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
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def count_pending(self) -> int:
        return int(
            self.conn.execute(
                "SELECT count(*) FROM submissions WHERE status='pending'"
            ).fetchone()[0]
        )

    def recoverable_ids(self) -> list[int]:
        return [
            row["id"]
            for row in self.conn.execute(
                "SELECT id FROM submissions WHERE status='collecting' ORDER BY id ASC"
            )
        ]

    def failed_ids(self) -> list[int]:
        return [
            row["id"]
            for row in self.conn.execute(
                "SELECT id FROM submissions WHERE status='delivery_failed' ORDER BY id ASC"
            )
        ]

    def close(self) -> None:
        self.conn.close()
