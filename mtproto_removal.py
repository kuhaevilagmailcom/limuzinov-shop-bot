"""Optional admin-authorized deletion of old posts in ONE configured Telegram channel.

Uses a dedicated Telegram user session with administrator rights. Session secrets
stay in BotHost environment variables and must never be logged or committed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class AdminDeletionUnavailable(Exception):
    """Admin connection, channel rights or the requested post is unavailable."""


@dataclass(frozen=True)
class DeletionTarget:
    chat_id: int
    message_ids: tuple[int, ...]


def admin_deletion_configured() -> bool:
    return bool(
        os.getenv("POST_ADMIN_API_ID", "").strip().isdigit()
        and os.getenv("POST_ADMIN_API_HASH", "").strip()
        and os.getenv("POST_ADMIN_SESSION", "").strip()
    )


async def _connect():
    if not admin_deletion_configured():
        raise AdminDeletionUnavailable(
            "Удаление старых постов не настроено: требуется сессия администратора канала."
        )
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = int(os.environ["POST_ADMIN_API_ID"].strip())
    api_hash = os.environ["POST_ADMIN_API_HASH"].strip()
    session = os.environ["POST_ADMIN_SESSION"].strip()
    client = TelegramClient(
        StringSession(session), api_id, api_hash,
        receive_updates=False, request_retries=2, connection_retries=2,
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise AdminDeletionUnavailable("Сессия администратора Telegram недействительна.")
    return client


async def _authorized_channel(client, chat_id: int):
    """Never resolve an arbitrary user-supplied channel."""
    from telethon import utils
    from telethon.tl.types import PeerChannel

    if not str(chat_id).startswith("-100"):
        raise AdminDeletionUnavailable("Операция разрешена только для настроенного канала.")
    peer_id = int(str(chat_id)[4:])
    try:
        entity = await client.get_entity(PeerChannel(peer_id))
    except (ValueError, TypeError):
        entity = None
    except Exception as exc:
        raise AdminDeletionUnavailable("Не удалось проверить канал администратора.") from exc

    if entity is None:
        # A StringSession may have no cache of the channel; look among joined dialogs.
        async for dialog in client.iter_dialogs():
            if utils.get_peer_id(dialog.entity) == chat_id:
                entity = dialog.entity
                break
    if entity is None or utils.get_peer_id(entity) != chat_id:
        raise AdminDeletionUnavailable("Аккаунт администратора не имеет доступа к этому каналу.")

    try:
        permissions = await client.get_permissions(entity, "me")
    except Exception as exc:
        raise AdminDeletionUnavailable("Не удалось проверить права администратора.") from exc
    can_delete = bool(
        getattr(permissions, "is_creator", False)
        or (
            getattr(permissions, "is_admin", False)
            and getattr(permissions, "delete_messages", False)
        )
    )
    if not can_delete:
        raise AdminDeletionUnavailable(
            "Аккаунту администратора нужны права удаления сообщений в канале."
        )
    return entity


async def find_deletion_target(chat_id: int, message_id: int) -> DeletionTarget:
    """Verify a post exists and collect other members of the same album."""
    if message_id <= 0:
        raise AdminDeletionUnavailable("Некорректный ID сообщения.")
    client = await _connect()
    try:
        entity = await _authorized_channel(client, chat_id)
        message = await client.get_messages(entity, ids=message_id)
        if message is None or getattr(message, "action", None) is not None:
            raise AdminDeletionUnavailable("Пост не найден или не подлежит удалению.")

        grouped_id = getattr(message, "grouped_id", None)
        ids = {message_id}
        if grouped_id is not None:
            nearby = await client.get_messages(
                entity,
                ids=list(range(max(1, message_id - 15), message_id + 16)),
            )
            for item in nearby or []:
                if item is not None and getattr(item, "grouped_id", None) == grouped_id:
                    ids.add(int(item.id))
        return DeletionTarget(chat_id=int(chat_id), message_ids=tuple(sorted(ids)))
    except AdminDeletionUnavailable:
        raise
    except Exception as exc:
        raise AdminDeletionUnavailable("Не удалось проверить пост через Telegram.") from exc
    finally:
        await client.disconnect()


async def delete_verified_target(chat_id: int, message_ids: tuple[int, ...]) -> bool:
    """Delete selected messages, then verify they are absent in Telegram."""
    if not message_ids or any(x <= 0 for x in message_ids):
        raise AdminDeletionUnavailable("Нельзя удалить публикацию без проверенных ID.")
    client = await _connect()
    try:
        entity = await _authorized_channel(client, chat_id)
        existing = await client.get_messages(entity, ids=list(message_ids))
        existing_ids = {
            int(item.id) for item in (existing or []) if item is not None
        }
        if existing_ids:
            await client.delete_messages(entity, sorted(existing_ids), revoke=True)
        remaining = await client.get_messages(entity, ids=list(message_ids))
        return all(item is None for item in (remaining or []))
    except AdminDeletionUnavailable:
        raise
    except Exception as exc:
        raise AdminDeletionUnavailable("Telegram не подтвердил удаление поста.") from exc
    finally:
        await client.disconnect()
