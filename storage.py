"""SQLite storage for submissions, moderation state, and blocked users."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def resolve_database_path(
    project_dir: Path,
    *,
    configured_path: str = "",
    data_dir: str = "",
) -> str:
    """Use BotHost's persistent data volume; migrate legacy DB with SQLite backup."""
    if configured_path.strip():
        return configured_path.strip()
    legacy = project_dir / "suggestions.sqlite3"
    if not data_dir.strip():
        return str(legacy)

    target = Path(data_dir.strip()).expanduser() / "suggestions.sqlite3"
    if target.resolve() == legacy.resolve():
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Backup API copies committed rows AND WAL pages safely, unlike shutil.copy.
    if (not target.exists() or target.stat().st_size == 0) and (
        legacy.is_file() and legacy.stat().st_size > 0
    ):
        with closing(sqlite3.connect(str(legacy))) as source:
            with closing(sqlite3.connect(str(target))) as destination:
                source.backup(destination)

    return str(target)


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
                admin_card_mode TEXT NOT NULL DEFAULT 'content',
                quote_text TEXT NOT NULL DEFAULT '',
                keyboard_cleaned INTEGER NOT NULL DEFAULT 0,
                reviewed_by_id INTEGER,
                reviewed_by_name TEXT,
                published_at TEXT,
                removal_token TEXT,
                published_chat_id TEXT,
                published_message_ids TEXT NOT NULL DEFAULT '[]',
                removed_at TEXT,
                author_disclosure_consent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(user_id, album_key)
            );
            CREATE TABLE IF NOT EXISTS items (
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                moderation_message_id INTEGER,
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
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                author_disclosure_opt_in INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS bot_users_seen_idx
                ON bot_users(first_seen_at DESC, user_id DESC);
            CREATE TABLE IF NOT EXISTS submission_spam_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS submission_spam_events_lookup_idx
                ON submission_spam_events(user_id, fingerprint, created_at);
            CREATE TABLE IF NOT EXISTS submission_cooldowns (
                user_id INTEGER PRIMARY KEY,
                until_ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS removal_payments (
                payment_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL UNIQUE,
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                buyer_user_id INTEGER NOT NULL,
                amount_rub INTEGER NOT NULL,
                action TEXT NOT NULL DEFAULT 'post_removal',
                status TEXT NOT NULL DEFAULT 'created',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                paid_at TEXT
            );
            CREATE INDEX IF NOT EXISTS removal_payments_pending_idx
                ON removal_payments(status, created_at);
            CREATE TABLE IF NOT EXISTS post_action_sessions (
                user_id INTEGER PRIMARY KEY,
                action TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invoices (
                payment_id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE,
                buyer_user_id INTEGER NOT NULL, created_by_id INTEGER NOT NULL,
                amount_rub INTEGER NOT NULL, description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'created', created_at TEXT NOT NULL DEFAULT (datetime('now')),
                paid_at TEXT
            );
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                ticket_price_rub INTEGER NOT NULL, created_by_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL, message_id INTEGER, status TEXT NOT NULL DEFAULT 'active',
                winner_user_id INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')), drawn_at TEXT,
                ends_at TEXT, channel_chat_id TEXT, channel_message_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS giveaway_tickets (
                payment_id TEXT PRIMARY KEY, giveaway_id INTEGER NOT NULL REFERENCES giveaways(id),
                user_id INTEGER NOT NULL, order_id TEXT NOT NULL UNIQUE, amount_rub INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'created', created_at TEXT NOT NULL DEFAULT (datetime('now')), paid_at TEXT
            );
            CREATE INDEX IF NOT EXISTS giveaway_tickets_pending_idx ON giveaway_tickets(status, created_at);
        """)

        # Add fields when opening databases made by the earlier direct-publication version.
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(submissions)")}
        if "admin_card_id" not in columns:
            self.conn.execute("ALTER TABLE submissions ADD COLUMN admin_card_id INTEGER")
        if "admin_card_mode" not in columns:
            self.conn.execute(
                "ALTER TABLE submissions ADD COLUMN admin_card_mode TEXT NOT NULL DEFAULT 'content'"
            )
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
        if "published_at" not in columns:
            self.conn.execute("ALTER TABLE submissions ADD COLUMN published_at TEXT")
        if "removal_token" not in columns:
            self.conn.execute("ALTER TABLE submissions ADD COLUMN removal_token TEXT")
        if "published_chat_id" not in columns:
            self.conn.execute("ALTER TABLE submissions ADD COLUMN published_chat_id TEXT")
        if "published_message_ids" not in columns:
            self.conn.execute("ALTER TABLE submissions ADD COLUMN published_message_ids TEXT NOT NULL DEFAULT '[]'")
        if "removed_at" not in columns:
            self.conn.execute("ALTER TABLE submissions ADD COLUMN removed_at TEXT")
        if "author_disclosure_consent" not in columns:
            self.conn.execute("ALTER TABLE submissions ADD COLUMN author_disclosure_consent INTEGER NOT NULL DEFAULT 0")

        user_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(bot_users)")}
        if "author_disclosure_opt_in" not in user_columns:
            self.conn.execute("ALTER TABLE bot_users ADD COLUMN author_disclosure_opt_in INTEGER NOT NULL DEFAULT 0")
        payment_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(removal_payments)")}
        if "action" not in payment_columns:
            self.conn.execute("ALTER TABLE removal_payments ADD COLUMN action TEXT NOT NULL DEFAULT 'post_removal'")
        ticket_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(giveaway_tickets)")}
        if "quantity" not in ticket_columns:
            self.conn.execute("ALTER TABLE giveaway_tickets ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
        giveaway_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(giveaways)")}
        if "ends_at" not in giveaway_columns:
            self.conn.execute("ALTER TABLE giveaways ADD COLUMN ends_at TEXT")
        if "channel_chat_id" not in giveaway_columns:
            self.conn.execute("ALTER TABLE giveaways ADD COLUMN channel_chat_id TEXT")
        if "channel_message_id" not in giveaway_columns:
            self.conn.execute("ALTER TABLE giveaways ADD COLUMN channel_message_id INTEGER")

        item_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
        if "moderation_message_id" not in item_columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN moderation_message_id INTEGER")
        if "content_type" not in item_columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN content_type TEXT NOT NULL DEFAULT ''")
        if "content_text" not in item_columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN content_text TEXT NOT NULL DEFAULT ''")
        if "entities_json" not in item_columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN entities_json TEXT NOT NULL DEFAULT '[]'")
        if "content_fingerprint" not in item_columns:
            self.conn.execute(
                "ALTER TABLE items ADD COLUMN content_fingerprint TEXT NOT NULL DEFAULT ''"
            )

        # Preserve users already known from submissions and blocks when upgrading an existing database.
        self.conn.execute("""
            INSERT OR IGNORE INTO bot_users(
                user_id, username, full_name, first_seen_at, last_seen_at
            )
            SELECT user_id, MAX(username), COALESCE(MAX(full_name), 'Пользователь'),
                   MIN(created_at), MAX(created_at)
            FROM submissions
            GROUP BY user_id
        """)
        self.conn.execute("""
            INSERT OR IGNORE INTO bot_users(user_id, full_name)
            SELECT user_id, 'Пользователь' FROM blocked_users
        """)

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
        content_fingerprint: str = "",
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
                        "INSERT INTO submissions(user_id,full_name,username,album_key,quote_text,author_disclosure_consent) "
                        "VALUES (?,?,?,?,?,?)",
                        (user_id, full_name, username, album_key, quote_text, int(self.author_disclosure_enabled(user_id))),
                    )
                    submission_id, fresh = int(cur.lastrowid), True
            else:
                cur = self.conn.execute(
                    "INSERT INTO submissions(user_id,full_name,username,album_key,quote_text,author_disclosure_consent) "
                    "VALUES (?,?,?,NULL,?,?)",
                    (user_id, full_name, username, quote_text, int(self.author_disclosure_enabled(user_id))),
                )
                submission_id, fresh = int(cur.lastrowid), True

            self.conn.execute(
                "INSERT INTO items("
                "submission_id,user_id,message_id,content_type,content_text,entities_json,content_fingerprint"
                ") VALUES (?,?,?,?,?,?,?)",
                (
                    submission_id,
                    user_id,
                    message_id,
                    content_type,
                    content_text,
                    entities_json,
                    content_fingerprint,
                ),
            )
            if quote_text and quote_text.strip():
                self.conn.execute(
                    "UPDATE submissions SET quote_text=? "
                    "WHERE id=? AND (quote_text='' OR quote_text LIKE '[%')",
                    (quote_text, submission_id),
                )
            return submission_id, fresh

    def submission_spam_delay(
        self,
        submission_id: int,
        *,
        now: int | None = None,
        max_identical: int = 3,
        cooldown_seconds: int = 5 * 60,
    ) -> int:
        """Record one complete post, or return seconds until a user may post again.

        Only accepted posts count towards the limit. A media group is one post,
        irrespective of how many messages it contains. The check is persisted
        and protected by SQLite's writer lock so concurrent sends cannot bypass it.
        """
        timestamp = int(time.time()) if now is None else int(now)
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            sub = self.get(submission_id)
            if not sub:
                raise ValueError(f"Submission {submission_id} not found")
            user_id = int(sub["user_id"])
            cooldown = self.conn.execute(
                "SELECT until_ts FROM submission_cooldowns WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if cooldown and int(cooldown["until_ts"]) > timestamp:
                return int(cooldown["until_ts"]) - timestamp
            if cooldown:
                self.conn.execute(
                    "DELETE FROM submission_cooldowns WHERE user_id=?", (user_id,)
                )

            items = self.conn.execute(
                "SELECT content_type, content_text, content_fingerprint FROM items "
                "WHERE submission_id=? ORDER BY message_id ASC",
                (submission_id,),
            ).fetchall()
            if not items:
                raise ValueError(f"Submission {submission_id} has no items")
            components = [
                (str(item["content_type"]),
                 str(item["content_fingerprint"] or item["content_text"] or ""))
                for item in items
            ]
            fingerprint = hashlib.sha256(
                json.dumps(components, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()

            # Keep only the 5-minute rolling window; deleting old rows also
            # prevents the event table from growing indefinitely.
            self.conn.execute(
                "DELETE FROM submission_spam_events WHERE created_at<=?",
                (timestamp - cooldown_seconds,),
            )
            count = self.conn.execute(
                "SELECT COUNT(*) FROM submission_spam_events "
                "WHERE user_id=? AND fingerprint=? AND created_at>?",
                (user_id, fingerprint, timestamp - cooldown_seconds),
            ).fetchone()[0]
            if count >= max_identical:
                self.conn.execute(
                    "INSERT INTO submission_cooldowns(user_id, until_ts) VALUES (?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET until_ts=excluded.until_ts",
                    (user_id, timestamp + cooldown_seconds),
                )
                return cooldown_seconds
            self.conn.execute(
                "INSERT INTO submission_spam_events(user_id, fingerprint, created_at) "
                "VALUES (?,?,?)",
                (user_id, fingerprint, timestamp),
            )
            return 0

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

    def ensure_removal_token(self, submission_id: int) -> str:
        row = self.get(submission_id)
        if not row:
            raise ValueError("Submission not found")
        if row["removal_token"]:
            return str(row["removal_token"])
        token = secrets.token_urlsafe(12)
        with self.conn:
            self.conn.execute(
                "UPDATE submissions SET removal_token=? WHERE id=? AND removal_token IS NULL",
                (token, submission_id),
            )
        return str(self.get(submission_id)["removal_token"])

    def set_publication(self, submission_id: int, chat_id: int | str, message_ids: list[int]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE submissions SET published_chat_id=?, published_message_ids=? WHERE id=?",
                (str(chat_id), json.dumps([int(x) for x in message_ids]), submission_id),
            )

    def removal_target(self, submission_id: int, token: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM submissions "
            "WHERE id=? AND removal_token=? "
            "AND published_chat_id IS NOT NULL AND published_chat_id!='' "
            "AND published_message_ids IS NOT NULL AND published_message_ids!='[]'",
            (submission_id, token),
        ).fetchone()

    def create_removal_payment(self, payment_id: str, order_id: str, submission_id: int,
                               buyer_user_id: int, amount_rub: int, *, action: str = "post_removal") -> None:
        if action not in {"post_removal", "author_lookup"}:
            raise ValueError("Unsupported paid post action")
        with self.conn:
            self.conn.execute(
                "INSERT INTO removal_payments(payment_id,order_id,submission_id,buyer_user_id,amount_rub,action) "
                "VALUES(?,?,?,?,?,?)",
                (payment_id, order_id, submission_id, buyer_user_id, amount_rub, action),
            )

    def pending_removal_payments(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT p.*, s.published_chat_id, s.published_message_ids, s.removed_at, "
            "s.username, s.full_name, s.user_id, s.author_disclosure_consent "
            "FROM removal_payments p JOIN submissions s ON s.id=p.submission_id "
            "WHERE p.status IN ('created','processing') ORDER BY p.created_at LIMIT 50"
        ))

    def set_removal_payment_status(self, payment_id: str, status: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE removal_payments SET status=?, paid_at=CASE WHEN ?='paid' THEN datetime('now') ELSE paid_at END "
                "WHERE payment_id=? AND status!='paid'", (status[:32], status, payment_id)
            )

    def author_disclosure_enabled(self, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT author_disclosure_opt_in FROM bot_users WHERE user_id=?", (user_id,)
        ).fetchone()
        return bool(row and row["author_disclosure_opt_in"])

    def set_author_disclosure(self, user_id: int, enabled: bool) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE bot_users SET author_disclosure_opt_in=? WHERE user_id=?",
                (int(enabled), user_id),
            )
            if not enabled:
                # Revocation permanently disables disclosure of already published posts.
                # Re-enabling only consents to posts submitted from that point onward.
                self.conn.execute(
                    "UPDATE submissions SET author_disclosure_consent=0 WHERE user_id=?",
                    (user_id,),
                )

    def set_post_action(self, user_id: int, action: str) -> None:
        if action not in {"post_removal", "author_lookup"}:
            raise ValueError("Unknown post action")
        with self.conn:
            self.conn.execute(
                "INSERT INTO post_action_sessions(user_id,action,created_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET action=excluded.action,created_at=excluded.created_at",
                (user_id, action, int(time.time())),
            )

    def get_post_action(self, user_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT action,created_at FROM post_action_sessions WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if row and int(time.time()) - int(row["created_at"]) <= 900:
            return str(row["action"])
        if row:
            self.clear_post_action(user_id)
        return None

    def clear_post_action(self, user_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM post_action_sessions WHERE user_id=?", (user_id,))

    def find_published_submission(
        self, channel_id: str | int, message_id: int, *, channel_alias: str = ""
    ) -> sqlite3.Row | None:
        """Lookup only tracked copies; album and disclaimer IDs map to the same post."""
        for row in self.conn.execute(
            "SELECT * FROM submissions WHERE published_chat_id IN (?,?) "
            "AND published_message_ids IS NOT NULL AND published_message_ids!='[]' "
            "ORDER BY id DESC",
            (str(channel_id), channel_alias or str(channel_id)),
        ):
            try:
                ids = json.loads(row["published_message_ids"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(ids, list) and message_id in ids:
                return row
        return None

    def mark_removed(self, submission_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE submissions SET removed_at=COALESCE(removed_at,datetime('now')) WHERE id=?",
                (submission_id,),
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
            assignments = ["status=?"]
            params: list[object] = [to_status]
            if to_status == "published":
                assignments.append("published_at=datetime('now')")
            if reviewed_by_id is not None or reviewed_by_name is not None:
                assignments.extend(["reviewed_by_id=?", "reviewed_by_name=?"])
                params.extend([reviewed_by_id, reviewed_by_name])
            params.extend([submission_id, from_status])
            cur = self.conn.execute(
                f"UPDATE submissions SET {', '.join(assignments)} WHERE id=? AND status=?",
                params,
            )
            return cur.rowcount == 1

    def set_card_pending(
        self, submission_id: int, card_id: int, card_mode: str = "content"
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE submissions SET admin_card_id=?, admin_card_mode=?, keyboard_cleaned=0, "
                "status=CASE WHEN status='notifying' THEN 'pending' ELSE status END "
                "WHERE id=?",
                (card_id, card_mode, submission_id),
            )

    def set_moderation_message_ids(
        self, submission_id: int, moderation_message_ids: list[int]
    ) -> None:
        rows = list(
            self.conn.execute(
                "SELECT message_id FROM items WHERE submission_id=? ORDER BY message_id ASC",
                (submission_id,),
            )
        )
        if len(rows) != len(moderation_message_ids):
            raise ValueError("Moderation message count does not match submission items")
        with self.conn:
            self.conn.executemany(
                "UPDATE items SET moderation_message_id=? "
                "WHERE submission_id=? AND message_id=?",
                [
                    (moderation_id, submission_id, int(row["message_id"]))
                    for row, moderation_id in zip(rows, moderation_message_ids)
                ],
            )

    def moderation_message_ids(self, submission_id: int) -> list[int]:
        return [
            int(row["moderation_message_id"])
            for row in self.conn.execute(
                "SELECT moderation_message_id FROM items "
                "WHERE submission_id=? AND moderation_message_id IS NOT NULL "
                "ORDER BY message_id ASC",
                (submission_id,),
            )
        ]

    def submission_for_moderation_message(self, moderation_message_id: int) -> sqlite3.Row | None:
        """Find the suggestion whose copied content or control card was replied to."""
        item = self.conn.execute(
            "SELECT submission_id FROM items WHERE moderation_message_id=? LIMIT 1",
            (moderation_message_id,),
        ).fetchone()
        if item:
            return self.get(int(item["submission_id"]))
        return self.conn.execute(
            "SELECT * FROM submissions WHERE admin_card_id=? ORDER BY id DESC LIMIT 1",
            (moderation_message_id,),
        ).fetchone()

    def remember_primary_moderation_message_id(
        self, submission_id: int, moderation_message_id: int
    ) -> None:
        row = self.conn.execute(
            "SELECT message_id FROM items WHERE submission_id=? ORDER BY message_id ASC LIMIT 1",
            (submission_id,),
        ).fetchone()
        if row:
            with self.conn:
                self.conn.execute(
                    "UPDATE items SET moderation_message_id=? "
                    "WHERE submission_id=? AND message_id=?",
                    (moderation_message_id, submission_id, int(row["message_id"])),
                )

    def register_user(
        self, user_id: int, username: str | None, full_name: str
    ) -> None:
        """Remember every person who opens or messages the bot in private."""
        name = full_name.strip() or "Пользователь"
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO bot_users(user_id, username, full_name, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    full_name=excluded.full_name,
                    last_seen_at=datetime('now')
                """,
                (int(user_id), username, name),
            )

    def sync_known_users(self) -> None:
        """Rebuild the recipient registry from every user still present in this database."""
        with self.conn:
            self.conn.execute("""
                INSERT OR IGNORE INTO bot_users(
                    user_id, username, full_name, first_seen_at, last_seen_at
                )
                SELECT user_id, MAX(username), COALESCE(MAX(full_name), 'Пользователь'),
                       MIN(created_at), MAX(created_at)
                FROM submissions
                GROUP BY user_id
            """)
            self.conn.execute("""
                INSERT OR IGNORE INTO bot_users(user_id, full_name)
                SELECT user_id, 'Пользователь' FROM items
                GROUP BY user_id
            """)
            self.conn.execute("""
                INSERT OR IGNORE INTO bot_users(user_id, full_name)
                SELECT user_id, 'Пользователь' FROM blocked_users
            """)

    def known_submission_user_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS total FROM submissions"
        ).fetchone()
        return int(row["total"] or 0)

    def user_count(self) -> int:
        self.sync_known_users()
        row = self.conn.execute("SELECT COUNT(*) AS total FROM bot_users").fetchone()
        return int(row["total"] or 0)

    def list_users(self, offset: int = 0, limit: int = 10) -> list[sqlite3.Row]:
        self.sync_known_users()
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 20))
        return list(
            self.conn.execute(
                """
                SELECT user_id, username, full_name, first_seen_at, last_seen_at
                FROM bot_users
                ORDER BY first_seen_at DESC, user_id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
        )

    def all_user_ids(self) -> list[int]:
        self.sync_known_users()
        return [
            int(row["user_id"])
            for row in self.conn.execute("SELECT user_id FROM bot_users ORDER BY user_id")
        ]

    def published_counts(
        self,
        *,
        today_start: str,
        week_start: str,
        month_start: str,
        now: str,
    ) -> dict[str, int]:
        """Count published submissions in local-time windows expressed as UTC strings."""
        row = self.conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE
                    WHEN COALESCE(published_at, created_at) >= ?
                     AND COALESCE(published_at, created_at) < ? THEN 1 ELSE 0 END), 0) AS today,
                COALESCE(SUM(CASE
                    WHEN COALESCE(published_at, created_at) >= ?
                     AND COALESCE(published_at, created_at) < ? THEN 1 ELSE 0 END), 0) AS week,
                COALESCE(SUM(CASE
                    WHEN COALESCE(published_at, created_at) >= ?
                     AND COALESCE(published_at, created_at) < ? THEN 1 ELSE 0 END), 0) AS month
            FROM submissions
            WHERE status='published'
            """,
            (today_start, now, week_start, now, month_start, now),
        ).fetchone()
        return {
            "today": int(row["today"] or 0),
            "week": int(row["week"] or 0),
            "month": int(row["month"] or 0),
        }

    def moderator_top(self, limit: int = 10) -> list[sqlite3.Row]:
        """Return moderators ranked by the number of completed decisions."""
        limit = max(1, min(int(limit), 20))
        return list(
            self.conn.execute(
                """
                SELECT
                    reviewed_by_id,
                    MAX(COALESCE(NULLIF(reviewed_by_name, ''), 'Модератор')) AS reviewed_by_name,
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) AS accepted,
                    SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,
                    SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked
                FROM submissions
                WHERE reviewed_by_id IS NOT NULL
                  AND status IN ('published', 'rejected', 'blocked')
                GROUP BY reviewed_by_id
                ORDER BY total DESC, accepted DESC, rejected ASC, reviewed_by_id ASC
                LIMIT ?
                """,
                (limit,),
            )
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

    def prepare_keyboard_cleanup_version(self, version: str) -> None:
        """Force one cleanup pass when the keyboard-cleanup logic changes."""
        current = self.get_setting("keyboard_cleanup_version")
        if current == version:
            return
        with self.conn:
            self.conn.execute(
                "UPDATE submissions SET keyboard_cleaned=0 "
                "WHERE admin_card_id IS NOT NULL AND status != 'pending'"
            )
            self.conn.execute(
                "INSERT INTO settings(key,value) VALUES('keyboard_cleanup_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (version,),
            )

    def cards_needing_keyboard_cleanup(self) -> list[sqlite3.Row]:
        """Return pending cards to refresh and processed cards with keyboards still to clear."""
        return list(
            self.conn.execute(
                "SELECT id, admin_card_id, admin_card_mode, status FROM submissions "
                "WHERE admin_card_id IS NOT NULL "
                "AND (status='pending' OR keyboard_cleaned=0) ORDER BY id ASC"
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

    def create_invoice(self, payment_id: str, order_id: str, buyer_id: int, admin_id: int, amount: int, description: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO invoices(payment_id,order_id,buyer_user_id,created_by_id,amount_rub,description) VALUES(?,?,?,?,?,?)", (payment_id, order_id, buyer_id, admin_id, amount, description[:255]))

    def pending_invoices(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT i.*, u.username, u.full_name FROM invoices i "
            "LEFT JOIN bot_users u ON u.user_id=i.buyer_user_id "
            "WHERE i.status='created' ORDER BY i.created_at LIMIT 50"
        ))

    def set_invoice_status(self, payment_id: str, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE invoices SET status=?, paid_at=CASE WHEN ?='paid' THEN datetime('now') ELSE paid_at END WHERE payment_id=? AND status!='paid'", (status, status, payment_id))

    def create_giveaway(self, title: str, price: int, admin_id: int, chat_id: int, ends_at: str | None = None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO giveaways(title,ticket_price_rub,created_by_id,chat_id,ends_at) VALUES(?,?,?,?,?)",
                (title[:180], price, admin_id, chat_id, ends_at),
            )
            return int(cur.lastrowid)

    def set_giveaway_message(self, giveaway_id: int, message_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE giveaways SET message_id=? WHERE id=?", (message_id, giveaway_id))

    def set_giveaway_channel_message(self, giveaway_id: int, chat_id: str | int, message_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE giveaways SET channel_chat_id=?, channel_message_id=? WHERE id=?",
                (str(chat_id), message_id, giveaway_id),
            )

    def giveaway(self, giveaway_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)).fetchone()

    def active_giveaway(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM giveaways WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()

    def due_giveaways(self, now_utc: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM giveaways WHERE status='active' AND ends_at IS NOT NULL AND ends_at<=? ORDER BY id",
            (now_utc,),
        ))

    def set_active_giveaway_price(self, price: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE giveaways SET ticket_price_rub=? WHERE status='active'", (price,))

    def create_giveaway_ticket(self, payment_id: str, order_id: str, giveaway_id: int, user_id: int, amount: int, quantity: int = 1) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO giveaway_tickets(payment_id,giveaway_id,user_id,order_id,amount_rub,quantity) VALUES(?,?,?,?,?,?)", (payment_id, giveaway_id, user_id, order_id, amount, quantity))

    def pending_giveaway_tickets(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT t.*, g.title, g.status AS giveaway_status, u.username, u.full_name "
            "FROM giveaway_tickets t JOIN giveaways g ON g.id=t.giveaway_id "
            "LEFT JOIN bot_users u ON u.user_id=t.user_id "
            "WHERE t.status='created' ORDER BY t.created_at LIMIT 50"
        ))

    def set_giveaway_ticket_status(self, payment_id: str, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE giveaway_tickets SET status=?, paid_at=CASE WHEN ?='paid' THEN datetime('now') ELSE paid_at END WHERE payment_id=? AND status!='paid'", (status, status, payment_id))

    def giveaway_stats(self, giveaway_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT g.*, COALESCE(SUM(CASE WHEN t.status='paid' THEN t.quantity END),0) tickets, COUNT(DISTINCT CASE WHEN t.status='paid' THEN t.user_id END) buyers, COALESCE(SUM(CASE WHEN t.status='paid' THEN t.amount_rub END),0) revenue FROM giveaways g LEFT JOIN giveaway_tickets t ON t.giveaway_id=g.id WHERE g.id=? GROUP BY g.id", (giveaway_id,)).fetchone()

    def draw_giveaway(self, giveaway_id: int) -> int | None:
        tickets = list(self.conn.execute("SELECT user_id, quantity FROM giveaway_tickets WHERE giveaway_id=? AND status='paid' ORDER BY payment_id", (giveaway_id,)))
        total = sum(int(row["quantity"]) for row in tickets)
        if not total:
            with self.conn:
                self.conn.execute(
                    "UPDATE giveaways SET status='drawn', drawn_at=datetime('now') WHERE id=? AND status='active'",
                    (giveaway_id,),
                )
            return None
        selected = secrets.randbelow(total)
        owner = None
        for ticket in tickets:
            selected -= int(ticket["quantity"])
            if selected < 0:
                owner = int(ticket["user_id"])
                break
        if owner is None: return None
        with self.conn:
            changed = self.conn.execute("UPDATE giveaways SET status='drawn', winner_user_id=?, drawn_at=datetime('now') WHERE id=? AND status='active'", (owner, giveaway_id)).rowcount
        return owner if changed else None

    def close(self) -> None:
        self.conn.close()
