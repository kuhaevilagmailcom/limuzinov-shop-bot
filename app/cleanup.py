from __future__ import annotations

from collections import defaultdict
from aiogram import Bot


class CleanBot(Bot):
    """Удаляет предыдущие сообщения бота перед показом нового экрана."""

    _history = defaultdict(list)

    async def _cleanup(self, chat_id: int):
        for message_id in self._history[chat_id][-10:]:
            try:
                await super().delete_message(chat_id, message_id)
            except Exception:
                pass
        self._history[chat_id].clear()

    def _save(self, chat_id, message):
        self._history[chat_id].append(message.message_id)

    async def send_message(self, chat_id, *args, **kwargs):
        await self._cleanup(chat_id)
        msg = await super().send_message(chat_id, *args, **kwargs)
        self._save(chat_id, msg)
        return msg

    async def send_photo(self, chat_id, *args, **kwargs):
        await self._cleanup(chat_id)
        msg = await super().send_photo(chat_id, *args, **kwargs)
        self._save(chat_id, msg)
        return msg

    async def send_invoice(self, chat_id, *args, **kwargs):
        await self._cleanup(chat_id)
        msg = await super().send_invoice(chat_id, *args, **kwargs)
        self._save(chat_id, msg)
        return msg
