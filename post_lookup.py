"""Strict parsing of Telegram channel post URLs (no external URL fetching)."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit


class InvalidPostLink(ValueError):
    pass


_DISCLAIMER_MARKER = "This channel fully complies with all platform rules"
_REMOVE_LINK_RE = re.compile(
    r"(?:https://t\.me/PozorMgnBot\?start=remove_[A-Za-z0-9_-]+|Удалить пост|Предложить пост)",
    re.IGNORECASE,
)


def normalize_publication_text(value: str) -> str:
    """Normalize the user-authored part of a published post.

    Telegram exports and forwarded messages may render the disclaimer and bot
    links differently.  Removing those generated fragments gives old exports
    and live forwards the same stable identity.
    """
    text = value or ""
    if _DISCLAIMER_MARKER in text:
        text = text.split(_DISCLAIMER_MARKER, 1)[0]
    if "━━━━━━━━━━━━━━" in text:
        text = text.split("━━━━━━━━━━━━━━", 1)[0]
    text = _REMOVE_LINK_RE.sub(" ", text)
    return " ".join(text.split()).strip().casefold()


def publication_text_fingerprint(value: str) -> str:
    normalized = normalize_publication_text(value)
    if not normalized or normalized in {"[фото]", "фотография", "photo"}:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_post_link(raw: str) -> tuple[str, int]:
    """Return (channel address, message_id).

    Private posts: https://t.me/c/<internal-chat-id>/<message-id>.
    Public posts: https://t.me/<channel_username>/<message-id>.
    """
    value = (raw or "").strip()
    if len(value) > 300 or not value:
        raise InvalidPostLink("Отправь полную ссылку https://t.me/c/...")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise InvalidPostLink("Некорректная ссылка Telegram.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"t.me", "telegram.me", "www.t.me"}
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise InvalidPostLink("Нужна ссылка на пост из Telegram-канала.")
    path = parsed.path.strip("/").split("/")
    if len(path) == 3 and path[0] == "c":
        internal_id, number = path[1:]
        if not re.fullmatch(r"[1-9][0-9]{2,18}", internal_id):
            raise InvalidPostLink("Некорректный ID канала в ссылке.")
        channel = "-100" + internal_id
    elif len(path) in {2, 3}:
        if len(path) == 3 and path[0] == "s":
            handle, number = path[1:]
        elif len(path) == 2:
            handle, number = path
        else:
            raise InvalidPostLink("Нужна ссылка непосредственно на публикацию.")
        if handle in {"c", "joinchat", "share", "addstickers", "login"} or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{4,31}", handle
        ):
            raise InvalidPostLink("Нужна ссылка на публикацию канала.")
        channel = "@" + handle
    else:
        raise InvalidPostLink("Пример ссылки: https://t.me/c/1234567890/123")
    if not re.fullmatch(r"[1-9][0-9]{0,15}", number):
        raise InvalidPostLink("В ссылке отсутствует ID публикации.")
    return channel, int(number)
