from __future__ import annotations

import asyncio
from collections import defaultdict

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError


class CleanBot(Bot):
    """Keeps one active bot screen per chat.

    Removes previous bot messages before creating a new one.
    Works with messages, photos and invoices.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._history: dict[int | str, list[int]] = defaultdict(list)
        self._locks: dict[int | str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _cleanup(self, chat_id: int | str) -> None:
        old_messages = list(self._history.get(chat_id, []))
        self._history[chat_id].clear()

        for message_id in old_messages:
            try:
                await super().delete_message(chat_id, message_id)
            except TelegramAPIError:
                pass

    def _remember(self, chat_id: int | str, message) -> None:
        if message:
            self._history[chat_id].append(message.message_id)
            self._history[chat_id] = self._history[chat_id][-20:]

    async def _replace(self, chat_id: int | str, sender, *args, **kwargs):
        async with self._locks[chat_id]:
            await self._cleanup(chat_id)
            message = await sender(chat_id, *args, **kwargs)
            self._remember(chat_id, message)
            return message

    async def send_message(self, chat_id, *args, **kwargs):
        return await self._replace(chat_id, super().send_message, *args, **kwargs)

    async def send_photo(self, chat_id, *args, **kwargs):
        return await self._replace(chat_id, super().send_photo, *args, **kwargs)

    async def send_invoice(self, chat_id, *args, **kwargs):
        return await self._replace(chat_id, super().send_invoice, *args, **kwargs)

    async def edit_message_text(self, *args, **kwargs):
        return await super().edit_message_text(*args, **kwargs)
