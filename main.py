"""College channel suggestion bot. Python 3.11+, aiogram 3, SQLite."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (BotCommand, BotCommandScopeDefault, BotCommandScopeAllPrivateChats,
                           BotCommandScopeAllGroupChats, BotCommandScopeAllChatAdministrators,
                           BotCommandScopeChat, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, MenuButtonCommands, Message)
from dotenv import load_dotenv

from storage import Storage

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "6341678542"))
DB_PATH = os.getenv("DB_PATH", str(ROOT / "suggestions.sqlite3"))
DEFAULT_CHANNEL_ID = os.getenv("CHANNEL_ID", "-1004422721133").strip() or "-1004422721133"
STICKER_PACK = os.getenv("EMOJI_PACK", "vector_icons_by_fStikBot").strip()
TIMEZONE = os.getenv("TIMEZONE", "Asia/Yekaterinburg")
CONFIRM_EMOJI_ID = "5219899949281453881"
ALBUM_DELAY = 2.0

logger = logging.getLogger("college_suggest")
router = Router()
db = Storage(DB_PATH)
album_jobs: dict[tuple[int, str], asyncio.Task] = {}
# Exact confirmation icon provided by the customer; the rest are resolved
# at startup from the specified public Telegram custom-emoji pack.
icons = {"ok": CONFIRM_EMOJI_ID}
ALT = {"sender": "🤖", "id": "🪪", "name": "👤", "time": "🕓", "ok": "✅", "reject": "❌"}


def glyph(role: str, *, premium: bool = True) -> str:
    fallback = ALT[role]
    code = icons.get(role) if premium else None
    return f'<tg-emoji emoji-id="{code}">{fallback}</tg-emoji>' if code else fallback


def simple_html(text: str) -> str:
    """Keep readable Unicode fallback if Telegram does not allow custom emoji."""
    return re.sub(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', r'\1', text)


async def load_pack(bot: Bot) -> None:
    if not STICKER_PACK:
        return
    try:
        sticker_set = await bot.get_sticker_set(STICKER_PACK)
        matches = {s.emoji.replace("\ufe0f", ""): s.custom_emoji_id
                   for s in sticker_set.stickers
                   if s.emoji and s.custom_emoji_id}
        for role, alt in ALT.items():
            if role == "ok":
                continue  # supplied exact ID always wins
            custom_id = matches.get(alt.replace("\ufe0f", ""))
            if custom_id:
                icons[role] = custom_id
        logger.info("Custom emoji matches found: %s", ", ".join(sorted(icons)))
    except TelegramAPIError as exc:
        logger.warning("Emoji pack unavailable, using Unicode fallback: %s", exc)


def channel_id() -> str | int | None:
    value = db.get_setting("channel_id") or DEFAULT_CHANNEL_ID
    if not value:
        return None
    return int(value) if value.lstrip("-").isdigit() else value


def buttons(sub_id: int, *, premium: bool = True) -> InlineKeyboardMarkup:
    def button(label: str, action: str, role: str, style: str) -> InlineKeyboardButton:
        kwargs = {"text": label, "callback_data": f"mod:{action}:{sub_id}"}
        if premium:
            kwargs["style"] = style
            if icons.get(role):
                kwargs["icon_custom_emoji_id"] = icons[role]
        else:
            kwargs["text"] = f"{ALT[role]} {label}"
        return InlineKeyboardButton(**kwargs)

    return InlineKeyboardMarkup(inline_keyboard=[[
        button("Принять", "yes", "ok", "success"),
        button("Отклонить", "no", "reject", "danger"),
    ]])


def sent_at(utc_value: str) -> str:
    try:
        tz = ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    dt = datetime.fromisoformat(utc_value.replace(" ", "T")).replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def card_text(sub, status: str = "pending", *, premium: bool = True) -> str:
    name = html.escape(sub["full_name"] or "Не указано")
    username = sub["username"]
    sender = ("@" + html.escape(username)) if username else "Без username"
    header = "<b>Новое сообщение</b>" if status == "pending" else "<b>Сообщение обработано</b>"
    text = (f"{header}\n━━━━━━━━━━━━━━\n"
            f"{glyph('sender', premium=premium)} <b>Отправитель:</b> {sender}\n"
            f"{glyph('id', premium=premium)} <b>ID:</b> <code>{sub['user_id']}</code>\n"
            f"{glyph('name', premium=premium)} <b>Имя:</b> {name}\n"
            f"{glyph('time', premium=premium)} <b>Отправлено:</b> {sent_at(sub['created_at'])}")
    if status == "published":
        text += f"\n\n{glyph('ok', premium=premium)} <b>Опубликовано</b>"
    if status == "rejected":
        text += f"\n\n{glyph('reject', premium=premium)} <b>Отклонено</b>"
    return text


async def safe_notify(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramBadRequest:
        try:
            await bot.send_message(chat_id, simple_html(text))
        except TelegramAPIError as exc:
            logger.info("Cannot notify user %s: %s", chat_id, exc)
    except TelegramAPIError as exc:
        logger.info("Cannot notify user %s: %s", chat_id, exc)


async def copy_submission(bot: Bot, destination: int | str, sub_id: int) -> None:
    sub = db.get(sub_id)
    message_ids = db.message_ids(sub_id)
    if not sub or not message_ids:
        raise ValueError("Submission missing")
    if len(message_ids) == 1:
        await bot.copy_message(chat_id=destination, from_chat_id=sub["user_id"], message_id=message_ids[0])
    else:
        result = await bot.copy_messages(chat_id=destination, from_chat_id=sub["user_id"], message_ids=message_ids)
        if len(result) != len(message_ids):
            raise ValueError("Partial album copy; review destination before retry")


async def send_to_admin(bot: Bot, sub_id: int) -> None:
    if not db.move(sub_id, "collecting", "notifying"):
        return
    sub = db.get(sub_id)
    try:
        await copy_submission(bot, ADMIN_ID, sub_id)
        try:
            card = await bot.send_message(ADMIN_ID, card_text(sub), reply_markup=buttons(sub_id))
        except TelegramBadRequest:
            card = await bot.send_message(ADMIN_ID, card_text(sub, premium=False), reply_markup=buttons(sub_id, premium=False))
        db.set_card_pending(sub_id, card.message_id)
    except (TelegramAPIError, ValueError) as exc:
        logger.exception("Admin delivery failed for %s: %s", sub_id, exc)
        db.move(sub_id, "notifying", "delivery_failed")
        await safe_notify(bot, sub["user_id"], "Не удалось передать предложение на проверку. Попробуйте позже.")
        return
    await safe_notify(bot, sub["user_id"], f"{glyph('ok')} Предложение отправлено на проверку.")


async def finish_album(bot: Bot, sub_id: int, key: tuple[int, str]) -> None:
    try:
        await asyncio.sleep(ALBUM_DELAY)
        await send_to_admin(bot, sub_id)
    finally:
        if album_jobs.get(key) is asyncio.current_task():
            album_jobs.pop(key, None)


async def configure_commands(bot: Bot) -> None:
    """Clear commands left in Telegram by the previous shop bot.

    Commands are stored on Telegram's servers independently of source code.
    Old scope/language entries can override a freshly configured default list.
    """
    scopes = (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
        BotCommandScopeChat(chat_id=ADMIN_ID),
    )
    for scope in scopes:
        for lang in ("", "ru", "en"):
            try:
                await bot.delete_my_commands(scope=scope, language_code=lang)
            except TelegramAPIError as exc:
                logger.warning("Failed to clear previous bot commands (%s, %s): %s", type(scope).__name__, lang, exc)

    user_commands = [BotCommand(command="start", description="Предложить сообщение")]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    await bot.set_my_commands(user_commands, scope=BotCommandScopeAllPrivateChats())
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramAPIError as exc:
        logger.warning("Could not reset old bot menu button: %s", exc)
    await ensure_admin_commands(bot)


async def ensure_admin_commands(bot: Bot) -> None:
    """Admin's private-chat menu includes /channel; public users see /start only."""
    admin_commands = [
        BotCommand(command="start", description="Предложить сообщение"),
        BotCommand(command="channel", description="Канал для публикаций"),
    ]
    try:
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
        await bot.set_chat_menu_button(chat_id=ADMIN_ID, menu_button=MenuButtonCommands())
    except TelegramAPIError as exc:
        # Chat-specific configuration becomes possible once admin presses /start.
        logger.warning("Cannot set admin chat command menu yet: %s", exc)


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, bot: Bot) -> None:
    if message.from_user and message.from_user.id == ADMIN_ID:
        await ensure_admin_commands(bot)
    await message.answer(
        "<b>Предложка</b>\n\n"
        "Отправь сюда текст, фото, видео, кружок, голосовое или файл."
    )


@router.message(Command("channel"), F.chat.type == "private")
async def set_channel(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        current = html.escape(str(channel_id() or "не подключён"))
        await message.answer(f"<b>Канал:</b> <code>{current}</code>\n\nУстановить: <code>/channel @username</code> или <code>/channel -100...</code>")
        return
    value = parts[1].strip()
    if not ((value.startswith("@") and re.fullmatch(r"@[a-zA-Z0-9_]{5,32}", value)) or
            (value.startswith("-100") and value[1:].isdigit())):
        await message.answer("Укажи @username канала либо числовой ID вида -100...")
        return
    target = int(value) if value.startswith("-100") else value
    try:
        ch = await bot.get_chat(target)
        member = await bot.get_chat_member(ch.id, (await bot.get_me()).id)
        if ch.type != "channel" or member.status not in ("administrator", "creator") or not getattr(member, "can_post_messages", False):
            await message.answer("Добавь бота администратором канала с правом публикации.")
            return
    except TelegramAPIError:
        await message.answer("Не могу получить доступ к каналу. Проверь права бота и адрес.")
        return
    db.set_setting("channel_id", str(ch.id))
    await message.answer(f"{glyph('ok')} Канал подключён: <b>{html.escape(ch.title or str(ch.id))}</b>.")


@router.message(F.chat.type == "private")
async def inbox(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    if message.text and message.text.startswith("/"):
        await message.answer("Пришли сюда текст или медиа — без команд.")
        return
    if message.content_type in {"new_chat_members", "left_chat_member", "pinned_message", "successful_payment", "invoice", "giveaway", "giveaway_winners", "paid_media", "forum_topic_created"}:
        await message.answer("Этот тип сообщения не поддерживается Telegram для публикации. Отправь текст или медиа.")
        return
    try:
        sub_id, fresh = db.add(user_id=message.from_user.id,
                               full_name=message.from_user.full_name,
                               username=message.from_user.username,
                               message_id=message.message_id,
                               album_key=message.media_group_id)
    except ValueError:
        await message.answer("Альбом был уже отправлен. Повтори отправку, пожалуйста.")
        return
    if message.media_group_id:
        key = (message.from_user.id, message.media_group_id)
        old = album_jobs.pop(key, None)
        if old:
            old.cancel()
        album_jobs[key] = asyncio.create_task(finish_album(bot, sub_id, key))
    elif fresh:
        await send_to_admin(bot, sub_id)


@router.callback_query(F.data.startswith("mod:"))
async def moderate(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        _, action, raw_id = (callback.data or "").split(":")
        sub_id = int(raw_id)
    except (ValueError, TypeError):
        await callback.answer("Неверная заявка.", show_alert=True)
        return
    sub = db.get(sub_id)
    if not sub:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if sub["status"] != "pending":
        await callback.answer("Уже обработано.")
        return
    if action == "yes":
        target = channel_id()
        if not target:
            await callback.answer("Сначала подключи канал: /channel", show_alert=True)
            return
        if not db.move(sub_id, "pending", "publishing"):
            await callback.answer("Уже обрабатывается.")
            return
        try:
            await copy_submission(bot, target, sub_id)
        except (TelegramAPIError, ValueError) as exc:
            # Never silently retry an ambiguous partial publish; avoid duplicates.
            db.move(sub_id, "publishing", "publish_uncertain")
            logger.exception("Publish status uncertain for %s: %s", sub_id, exc)
            await callback.answer("Ошибка публикации. Проверь канал вручную.", show_alert=True)
            return
        db.move(sub_id, "publishing", "published")
        new_status, notice, toast = "published", "✅ Твоё предложение опубликовано!", "Опубликовано"
    elif action == "no":
        if not db.move(sub_id, "pending", "rejected"):
            await callback.answer("Уже обрабатывается.")
            return
        new_status, notice, toast = "rejected", "❌ Твоё предложение отклонено.", "Отклонено"
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return
    try:
        if sub["admin_card_id"]:
            await bot.edit_message_text(chat_id=ADMIN_ID, message_id=sub["admin_card_id"],
                                        text=card_text(sub, new_status), reply_markup=None)
    except TelegramBadRequest:
        try:
            await bot.edit_message_text(chat_id=ADMIN_ID, message_id=sub["admin_card_id"],
                                        text=card_text(sub, new_status, premium=False), reply_markup=None)
        except TelegramAPIError as exc:
            logger.warning("Could not update admin card %s: %s", sub_id, exc)
    except TelegramAPIError as exc:
        logger.warning("Could not update admin card %s: %s", sub_id, exc)
    await callback.answer(toast)
    await safe_notify(bot, sub["user_id"], notice)


async def recover(bot: Bot) -> None:
    await asyncio.sleep(5)
    for sub_id in db.recoverable_ids():
        sub = db.get(sub_id)
        key = (sub["user_id"], sub["album_key"]) if sub and sub["album_key"] else None
        if key and key in album_jobs:
            continue
        await send_to_admin(bot, sub_id)


async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_BOT_TOKEN_HERE":
        raise SystemExit("Заполни BOT_TOKEN в .env или в переменных окружения.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await configure_commands(bot)
        await load_pack(bot)
        recovery_job = asyncio.create_task(recover(bot))
        logger.info("Started. Admin %s; channel %s", ADMIN_ID, channel_id())
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if "recovery_job" in locals():
            recovery_job.cancel()
        for job in album_jobs.values():
            job.cancel()
        db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
