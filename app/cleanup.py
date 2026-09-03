from __future__ import annotations

from collections import defaultdict
from aiogram import Bot


class CleanBot(Bot):
    """Keeps one active bot screen per chat.

    Removes previous bot messages before creating a new one.
    Works with messages, photos and invoices.
    """

    _history = defaultdict(list)

    async def _cleanup(self, chat_id: int):
        old_messages = list(self._history.get(chat_id, []))
        self._history[chat_id].clear()

        for message_id in old_messages:
            try:
                await super().delete_message(chat_id, message_id)
            except Exception:
                pass

    def _remember(self, chat_id: int, message):
        if message:
            self._history[chat_id].append(message.message_id)
            # keep only recent ids as protection from memory growth
            self._history[chat_id] = self._history[chat_id][-20:]

    async def send_message(self, chat_id, *args, **kwargs):
        await self._cleanup(chat_id)
        msg = await super().send_message(chat_id, *args, **kwargs)
        self._remember(chat_id, msg)
        return msg

    async def send_photo(self, chat_id, *args, **kwargs):
        await self._cleanup(chat_id)
        msg = await super().send_photo(chat_id, *args, **kwargs)
        self._remember(chat_id, msg)
        return msg

    async def send_invoice(self, chat_id, *args, **kwargs):
        await self._cleanup(chat_id)
        msg = await super().send_invoice(chat_id, *args, **kwargs)
        self._remember(chat_id, msg)
        return msg

    async def edit_message_text(self, *args, **kwargs):
        return await super().edit_message_text(*args, **kwargs)
