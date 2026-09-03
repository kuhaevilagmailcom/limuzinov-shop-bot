from __future__ import annotations

from collections import defaultdict

from aiogram import Bot


class CleanBot(Bot):
    """Удаляет прошлое сообщение бота перед отправкой нового."""

    _history = defaultdict(list)

    async def _cleanup(self, chat_id: int):
        for message_id in self._history[chat_id]:
            try:
                await super().delete_message(chat_id, message_id)
            except Exception:
                pass
        self._history[chat_id].clear()

    async def send_message(self, chat_id, *args, **kwargs):
        await self._cleanup(chat_id)
        message = await super().send_message(chat_id, *args, **kwargs)
        self._history[chat_id].append(message.message_id)
        return message

    async def send_photo(self, chat_id, *args, **kwargs):
        await self._cleanup(chat_id)
        message = await super().send_photo(chat_id, *args, **kwargs)
        self._history[chat_id].append(message.message_id)
        return message
