from __future__ import annotations

import logging
from collections import defaultdict

from aiogram import BaseMiddleware
from aiogram.types import Message

logger = logging.getLogger(__name__)


class DeletePreviousMessagesMiddleware(BaseMiddleware):
    """Удаляет предыдущее сообщение бота в чате перед отправкой нового."""

    def __init__(self):
        self.last_messages = defaultdict(list)

    async def __call__(self, handler, event, data):
        result = await handler(event, data)

        if isinstance(event, Message):
            sent = data.get("bot")
            if sent and event.chat:
                try:
                    old_ids = self.last_messages[event.chat.id]
                    for message_id in old_ids:
                        try:
                            await sent.delete_message(event.chat.id, message_id)
                        except Exception:
                            pass
                    if event.message_id:
                        self.last_messages[event.chat.id] = [event.message_id]
                except Exception:
                    logger.exception("Message cleanup failed")

        return result
