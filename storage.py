"""Durable SQLite storage for submissions and their original Telegram message IDs."""
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
                admin_kind TEXT NOT NULL DEFAULT 'bare',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, album_key)
            );
            CREATE TABLE IF NOT EXISTS items (
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                source_kind TEXT NOT NULL DEFAULT 'unknown',
                source_text TEXT,
                PRIMARY KEY (user_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS items_sub_id_idx ON items(submission_id);
            CREATE INDEX IF NOT EXISTS submissions_status_idx ON submissions(status);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        # Keep earlier submissions available when updating a running bot.
        for table, name, definition in (
            ("submissions", "admin_kind", "TEXT NOT NULL DEFAULT 'bare'"),
            ("items", "source_kind", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("items", "source_text", "TEXT"),
        ):
            columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if name not in columns:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def add(self, *, user_id: int, full_name: str, username: str | None,
            message_id: int, album_key: str | None,
            source_kind: str = "unknown", source_text: str | None = None) -> tuple[int, bool]:
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
                        "INSERT INTO submissions(user_id,full_name,username,album_key) VALUES (?,?,?,?)",
                        (user_id, full_name, username, album_key),
                    )
                    submission_id, new = cur.lastrowid, True
            else:
                cur = self.conn.execute(
                    "INSERT INTO submissions(user_id,full_name,username,album_key) VALUES (?,?,?,NULL)",
                    (user_id, full_name, username),
                )
                submission_id, new = cur.lastrowid, True
            self.conn.execute(
                "INSERT INTO items(submission_id,user_id,message_id,source_kind,source_text) VALUES (?,?,?,?,?)",
                (submission_id, user_id, message_id, source_kind, source_text),
            )
            return int(submission_id), new

    def get(self, submission_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()

    def message_ids(self, submission_id: int) -> list[int]:
        return [row["message_id"] for row in self.conn.execute(
            "SELECT message_id FROM items WHERE submission_id=? ORDER BY message_id ASC",
            (submission_id,),
        )]

    def first_item(self, submission_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM items WHERE submission_id=? ORDER BY message_id LIMIT 1",
            (submission_id,),
        ).fetchone()

    def move(self, submission_id: int, from_status: str, to_status: str) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE submissions SET status=? WHERE id=? AND status=?",
                (to_status, submission_id, from_status),
            )
            return cur.rowcount == 1

    def set_card_pending(self, submission_id: int, card_id: int, kind: str = "bare") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE submissions SET admin_card_id=?, admin_kind=?, status='pending' WHERE id=? AND status='notifying'",
                (card_id, kind, submission_id),
            )

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def count_pending(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM submissions WHERE status='pending'").fetchone()[0])

    def recoverable_ids(self) -> list[int]:
        return [row["id"] for row in self.conn.execute(
            "SELECT id FROM submissions WHERE status='collecting' ORDER BY id ASC",
        )]

    def failed_ids(self) -> list[int]:
        return [row["id"] for row in self.conn.execute(
            "SELECT id FROM submissions WHERE status='delivery_failed' ORDER BY id ASC",
        )]

    def close(self) -> None:
        self.conn.close()
