"""Restore users and historical publications from a Telegram Desktop HTML export."""
from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from post_lookup import publication_text_fingerprint
from storage import Storage, create_database_backup, resolve_database_path


ROOT = Path(__file__).resolve().parent
AUTHOR_RE = re.compile(
    r"Отправитель:\s*(?P<username>@[A-Za-z0-9_]{3,}|без username)"
    r".*?\bID\s*:?[\s·•|]*(?P<user_id>\d{5,20})",
    re.IGNORECASE | re.DOTALL,
)
NAME_RE = re.compile(r"(?:^|\n)[^\n]*?Имя:\s*(?P<name>[^\n]+)", re.IGNORECASE)
SENT_RE = re.compile(r"Отправлено:\s*(?P<date>\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})")
REMOVE_RE = re.compile(r"[?&]start=remove_(?P<submission_id>\d+)_(?P<token>[A-Za-z0-9_-]+)")
ACCEPTED_RE = re.compile(r"(?:Заявка принята и опубликована|\bПринято\b)", re.IGNORECASE)


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def descendants(self) -> list["Node"]:
        result: list[Node] = []
        for child in self.children:
            if isinstance(child, Node):
                result.append(child)
                result.extend(child.descendants())
        return result


class TelegramExportParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[Node] = []
        self.stack: list[Node] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key): str(value or "") for key, value in attrs}
        is_message = (
            tag == "div"
            and "message" in values.get("class", "").split()
            and values.get("id", "").startswith("message")
        )
        if not self.stack and not is_message:
            return
        node = Node(tag=tag, attrs=values)
        if self.stack:
            self.stack[-1].children.append(node)
        if tag in {"br", "img", "input", "link", "meta", "hr"}:
            if tag == "br" and self.stack:
                self.stack[-1].children.append("\n")
            return
        self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if self.stack and tag == "br":
            self.stack[-1].children.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            return
        if self.stack[-1].tag != tag:
            return
        node = self.stack.pop()
        if not self.stack and "message" in node.classes:
            self.messages.append(node)

    def handle_data(self, data: str) -> None:
        if self.stack:
            self.stack[-1].children.append(data)


def node_text(node: Node) -> str:
    parts: list[str] = []

    def visit(value: Node | str) -> None:
        if isinstance(value, str):
            parts.append(value)
            return
        if value.tag == "br":
            parts.append("\n")
        for child in value.children:
            visit(child)

    visit(node)
    lines = [" ".join(line.split()) for line in "".join(parts).splitlines()]
    return "\n".join(line for line in lines if line).strip()


def direct_child(node: Node, *classes: str) -> Node | None:
    required = set(classes)
    for child in node.children:
        if isinstance(child, Node) and required.issubset(child.classes):
            return child
    return None


def message_body(message: Node) -> Node | None:
    return direct_child(message, "body")


def message_text(message: Node) -> str:
    body = message_body(message)
    if body is None:
        return ""
    text_node = direct_child(body, "text")
    return node_text(text_node) if text_node else ""


def message_links(message: Node) -> list[str]:
    return [node.attrs["href"] for node in message.descendants() if node.tag == "a" and node.attrs.get("href")]


def clean_content(text: str) -> str:
    if "Контент заявки:" in text:
        content = text.rsplit("Контент заявки:", 1)[1]
    else:
        content = text.split("━━━━━━━━━━━━━━", 1)[0]
    content = content.replace("🆕 Новая заявка", "").replace("Заявка обработана", "")
    return " ".join(content.split()).strip()


@dataclass(frozen=True)
class ExportedAuthor:
    user_id: int
    username: str | None
    full_name: str


@dataclass(frozen=True)
class ExportedPublication:
    author: ExportedAuthor
    content: str
    fingerprint: str
    published_at: str | None
    legacy_submission_id: int | None
    removal_token: str | None


def parse_author(text: str) -> ExportedAuthor | None:
    match = AUTHOR_RE.search(text)
    if not match:
        return None
    raw_username = match.group("username")
    username = None if raw_username.casefold() == "без username" else raw_username.lstrip("@")
    name_match = NAME_RE.search(text)
    full_name = name_match.group("name").strip() if name_match else "Пользователь"
    return ExportedAuthor(int(match.group("user_id")), username, full_name)


def parse_timestamp(text: str) -> str | None:
    match = SENT_RE.search(text)
    if not match:
        return None
    local = datetime.strptime(match.group("date"), "%d.%m.%Y %H:%M")
    # The supplied export is from UTC+05:00 (Yekaterinburg/Magnitogorsk).
    utc = local.replace(tzinfo=timezone(timedelta(hours=5))).astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%d %H:%M:%S")


def load_export(export_dir: Path) -> tuple[dict[int, ExportedAuthor], list[ExportedPublication]]:
    users: dict[int, ExportedAuthor] = {}
    publications: list[ExportedPublication] = []
    files = sorted(export_dir.glob("messages*.html"), key=lambda path: (len(path.name), path.name))
    if not files:
        raise FileNotFoundError(f"No messages*.html files found in {export_dir}")
    for path in files:
        parser = TelegramExportParser()
        parser.feed(path.read_text(encoding="utf-8"))
        for message in parser.messages:
            text = message_text(message)
            author = parse_author(text)
            if author is None:
                continue
            users[author.user_id] = author
            if not ACCEPTED_RE.search(text):
                continue
            content = clean_content(text)
            fingerprint = publication_text_fingerprint(content)
            links = message_links(message)
            removal = next((REMOVE_RE.search(link) for link in links if REMOVE_RE.search(link)), None)
            legacy_id = int(removal.group("submission_id")) if removal else None
            token = removal.group("token") if removal else None
            if not fingerprint:
                source_id = message.attrs.get("id", "")
                fingerprint = "export-only:" + publication_text_fingerprint(
                    f"{author.user_id}:{parse_timestamp(text)}:{source_id}"
                )
            publications.append(ExportedPublication(
                author=author,
                content=content,
                fingerprint=fingerprint,
                published_at=parse_timestamp(text),
                legacy_submission_id=legacy_id,
                removal_token=token,
            ))
    return users, publications


def run(args: argparse.Namespace) -> int:
    users, publications = load_export(Path(args.export_dir).expanduser().resolve())
    unique_matchable = len({item.fingerprint for item in publications if not item.fingerprint.startswith("export-only:")})
    if args.dry_run:
        print(
            f"users={len(users)} accepted={len(publications)} "
            f"matchable_fingerprints={unique_matchable} dry_run=True"
        )
        return 0

    database_path = Path(args.db).expanduser().resolve()
    backup = create_database_backup(database_path, label="before-html-backfill")
    if backup:
        print(f"backup={backup}")
    storage = Storage(str(database_path))
    imported = existing = 0
    try:
        for author in users.values():
            storage.register_user(author.user_id, author.username, author.full_name)
        for item in publications:
            _, created = storage.import_published_submission(
                user_id=item.author.user_id,
                full_name=item.author.full_name,
                username=item.author.username,
                published_chat_id=args.publication_channel,
                published_message_ids=[],
                quote_text=item.content,
                published_at=item.published_at,
                removal_token=item.removal_token,
                legacy_submission_id=item.legacy_submission_id,
                publication_fingerprint=item.fingerprint,
            )
            imported += int(created)
            existing += int(not created)
    finally:
        storage.close()
    after = create_database_backup(database_path, label="after-html-backfill")
    print(
        f"users={len(users)} accepted={len(publications)} imported={imported} "
        f"existing={existing} matchable_fingerprints={unique_matchable} backup_after={after}"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    default_db = resolve_database_path(
        ROOT,
        configured_path=os.getenv("DB_PATH", ""),
        data_dir=os.getenv("DATA_DIR", str(ROOT / "data")),
    )
    result = argparse.ArgumentParser(description="Backfill SQLite from Telegram Desktop HTML export")
    result.add_argument("--export-dir", required=True)
    result.add_argument("--db", default=default_db)
    result.add_argument(
        "--publication-channel",
        default=os.getenv("DESTINATION_CHAT_ID", "-1001358443115"),
    )
    result.add_argument("--dry-run", action="store_true")
    return result


def main() -> int:
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")
    return run(parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
