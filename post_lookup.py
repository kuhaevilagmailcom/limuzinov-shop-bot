"""Strict parsing of Telegram channel post URLs (no external URL fetching)."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


class InvalidPostLink(ValueError):
    pass


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
