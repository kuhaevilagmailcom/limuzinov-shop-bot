"""Restore historical publication/author mappings from Telegram channel history.

This utility uses a Telegram user session (MTProto), because the Bot API cannot
enumerate historical messages. Credentials are read only from environment
variables and the local session file is ignored by git.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from storage import Storage, resolve_database_path


ROOT = Path(__file__).resolve().parent
AUTHOR_RE = re.compile(
    r"Отправитель:\s*(?P<username>@[A-Za-z0-9_]{3,}|без username)\s*[·•|]\s*ID\s*(?P<user_id>\d+)",
    re.IGNORECASE,
)
NAME_RE = re.compile(r"(?:^|\n)[^\r\n]*?Имя:\s*(?P<name>[^\r\n]+)", re.IGNORECASE)
REMOVE_RE = re.compile(r"[?&]start=remove_(?P<submission_id>\d+)_(?P<token>[A-Za-z0-9_-]+)")
MOD_CALLBACK_RE = re.compile(rb"(?:mod:(?:yes|no|status|reply)|publish):(?P<submission_id>\d+)")
SPACE_RE = re.compile(r"\s+")


@dataclass
class AuthorRecord:
    user_id: int
    full_name: str
    username: str | None
    legacy_submission_id: int | None
    text_key: str
    date: datetime | None


@dataclass
class PublicationRecord:
    message_ids: list[int] = field(default_factory=list)
    legacy_submission_id: int | None = None
    removal_token: str | None = None
    text_key: str = ""
    quote_text: str = ""
    date: datetime | None = None


def message_text(message) -> str:
    return str(getattr(message, "raw_text", None) or getattr(message, "message", None) or "")


def normalized_content(text: str) -> str:
    """Build a stable key shared by moderation and publication copies."""
    text = text.split("━━━━━━━━━━━━━━", 1)[0]
    text = text.split("This channel fully complies", 1)[0]
    text = re.sub(r"(?:^|\n)\s*Контент заявки:\s*", "\n", text, flags=re.IGNORECASE)
    text = REMOVE_RE.sub("", text)
    return SPACE_RE.sub(" ", text).strip().casefold()[:1200]


def parse_author(text: str) -> tuple[int, str | None, str] | None:
    match = AUTHOR_RE.search(text)
    if not match:
        return None
    username_raw = match.group("username")
    username = None if username_raw.casefold() == "без username" else username_raw.lstrip("@")
    name_match = NAME_RE.search(text)
    full_name = name_match.group("name").strip() if name_match else "Пользователь"
    return int(match.group("user_id")), username, full_name


def iter_buttons(message):
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            yield button


def moderation_submission_id(message) -> int | None:
    for button in iter_buttons(message):
        data = getattr(button, "data", None)
        if not isinstance(data, bytes):
            continue
        match = MOD_CALLBACK_RE.search(data)
        if match:
            return int(match.group("submission_id"))
    return None


def publication_link(message) -> tuple[int, str] | None:
    candidates = [message_text(message)]
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", None)
        if url:
            candidates.append(str(url))
    for button in iter_buttons(message):
        url = getattr(button, "url", None)
        if url:
            candidates.append(str(url))
    for value in candidates:
        match = REMOVE_RE.search(value)
        if match:
            return int(match.group("submission_id")), match.group("token")
    return None


async def scan_authors(client, channel) -> tuple[dict[int, AuthorRecord], dict[str, AuthorRecord]]:
    by_id: dict[int, AuthorRecord] = {}
    by_text: dict[str, AuthorRecord] = {}
    async for message in client.iter_messages(channel, reverse=True):
        text = message_text(message)
        parsed = parse_author(text)
        if not parsed:
            continue
        user_id, username, full_name = parsed
        record = AuthorRecord(
            user_id=user_id,
            full_name=full_name,
            username=username,
            legacy_submission_id=moderation_submission_id(message),
            text_key=normalized_content(text),
            date=getattr(message, "date", None),
        )
        if record.legacy_submission_id is not None:
            by_id[record.legacy_submission_id] = record
        if record.text_key:
            by_text[record.text_key] = record
    return by_id, by_text


async def scan_publications(client, channel) -> list[PublicationRecord]:
    grouped: dict[str, PublicationRecord] = {}
    message_to_group: dict[int, str] = {}
    async for message in client.iter_messages(channel, reverse=True):
        grouped_id = getattr(message, "grouped_id", None)
        key = f"album:{grouped_id}" if grouped_id is not None else f"message:{message.id}"
        link = publication_link(message)
        reply_to = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
        if link and isinstance(reply_to, int) and reply_to in message_to_group:
            key = message_to_group[reply_to]
        record = grouped.setdefault(key, PublicationRecord(date=getattr(message, "date", None)))
        record.message_ids.append(int(message.id))
        message_to_group[int(message.id)] = key
        text = message_text(message)
        content_key = normalized_content(text)
        if content_key and not record.text_key:
            record.text_key = content_key
            record.quote_text = text[:4000]
        if link:
            record.legacy_submission_id, record.removal_token = link
    return list(grouped.values())


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def run(args: argparse.Namespace) -> int:
    from telethon import TelegramClient

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"].strip()
    client = TelegramClient(args.session, api_id, api_hash)
    await client.start()
    try:
        moderation = await client.get_entity(args.moderation_channel)
        publication = await client.get_entity(args.publication_channel)
        authors_by_id, authors_by_text = await scan_authors(client, moderation)
        publications = await scan_publications(client, publication)
        publication_chat_id = int(getattr(publication, "id"))
        if publication_chat_id > 0:
            publication_chat_id = int(f"-100{publication_chat_id}")

        storage = Storage(args.db)
        imported = existing = unmatched = 0
        try:
            for post in publications:
                author = None
                if post.legacy_submission_id is not None:
                    author = authors_by_id.get(post.legacy_submission_id)
                if author is None and post.text_key:
                    author = authors_by_text.get(post.text_key)
                if author is None:
                    unmatched += 1
                    continue
                if args.dry_run:
                    imported += 1
                    continue
                _, created = storage.import_published_submission(
                    user_id=author.user_id,
                    full_name=author.full_name,
                    username=author.username,
                    published_chat_id=publication_chat_id,
                    published_message_ids=post.message_ids,
                    quote_text=post.quote_text,
                    published_at=iso_utc(post.date),
                    removal_token=post.removal_token,
                    legacy_submission_id=post.legacy_submission_id,
                )
                imported += int(created)
                existing += int(not created)
        finally:
            storage.close()

        print(
            f"authors={len(authors_by_id)} publications={len(publications)} "
            f"imported={imported} existing={existing} unmatched={unmatched} dry_run={args.dry_run}"
        )
        return 0 if unmatched == 0 else 2
    finally:
        await client.disconnect()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Backfill historical Telegram publications into SQLite")
    result.add_argument("--moderation-channel", required=True, help="Admin group/channel ID or @username")
    result.add_argument("--publication-channel", required=True, help="Public channel ID or @username")
    result.add_argument("--db", required=True, help="Path to the bot SQLite database")
    result.add_argument("--session", default=str(ROOT / "channel_backfill"), help="Local Telethon session path")
    result.add_argument("--dry-run", action="store_true", help="Scan and report without changing SQLite")
    return result


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    return asyncio.run(run(parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
