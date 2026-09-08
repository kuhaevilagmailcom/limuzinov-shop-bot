from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message, TelegramObject


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
            try:
                message = await sender(chat_id, *args, **kwargs)
            except TelegramBadRequest:
                clean_args, clean_kwargs, changed = self._strip_premium_emoji(
                    args, kwargs
                )
                if not changed:
                    raise
                message = await sender(chat_id, *clean_args, **clean_kwargs)
            self._remember(chat_id, message)
            return message

    @staticmethod
    def _strip_premium_emoji(args, kwargs):
        pattern = re.compile(r'<tg-emoji\s+emoji-id="\d+">(.*?)</tg-emoji>')
        args = list(args)
        kwargs = dict(kwargs)
        changed = False
        if args and isinstance(args[0], str):
            value, count = pattern.subn(r"\1", args[0])
            args[0], changed = value, bool(count)
        for key in ("text", "caption"):
            if isinstance(kwargs.get(key), str):
                value, count = pattern.subn(r"\1", kwargs[key])
                kwargs[key] = value
                changed = changed or bool(count)
        markup = kwargs.get("reply_markup")
        if isinstance(markup, InlineKeyboardMarkup):
            rows = []
            markup_changed = False
            for row in markup.inline_keyboard:
                clean_row = []
                for button in row:
                    if button.icon_custom_emoji_id:
                        button = button.model_copy(
                            update={"icon_custom_emoji_id": None}
                        )
                        markup_changed = True
                    clean_row.append(button)
                rows.append(clean_row)
            if markup_changed:
                kwargs["reply_markup"] = InlineKeyboardMarkup(inline_keyboard=rows)
                changed = True
        return tuple(args), kwargs, changed

    async def send_message(self, chat_id, *args, **kwargs):
        return await self._replace(chat_id, super().send_message, *args, **kwargs)

    async def send_photo(self, chat_id, *args, **kwargs):
        return await self._replace(chat_id, super().send_photo, *args, **kwargs)

    async def send_invoice(self, chat_id, *args, **kwargs):
        return await self._replace(chat_id, super().send_invoice, *args, **kwargs)

    async def edit_message_text(self, *args, **kwargs):
        try:
            return await super().edit_message_text(*args, **kwargs)
        except TelegramBadRequest:
            clean_args, clean_kwargs, changed = self._strip_premium_emoji(args, kwargs)
            if not changed:
                raise
            return await super().edit_message_text(*clean_args, **clean_kwargs)


class DeleteIncomingMessageMiddleware(BaseMiddleware):
    """Removes processed user messages so the private chat stays like one clean screen."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        result = await handler(event, data)
        if isinstance(event, Message) and not event.successful_payment:
            try:
                await event.bot.delete_message(event.chat.id, event.message_id)
            except TelegramAPIError:
                pass
        return result
