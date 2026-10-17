"""Telegram direct-publication bot. Python 3.11+, aiogram 3."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeChat,
    MenuButtonCommands,
    Message,
)
from dotenv import load_dotenv

from storage import Storage

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", str(ROOT / "suggestions.sqlite3"))

REQUESTED_DEFAULT_CHAT_ID = "-5106639548"
LEGACY_DEFAULT_CHAT_IDS = {"-1004422721133", "-1001358443115"}
_configured_chat_id = os.getenv(
    "DESTINATION_CHAT_ID",
    os.getenv("CHANNEL_ID", REQUESTED_DEFAULT_CHAT_ID),
).strip()
DEFAULT_CHAT_ID = (
    REQUESTED_DEFAULT_CHAT_ID
    if not _configured_chat_id or _configured_chat_id in LEGACY_DEFAULT_CHAT_IDS
    else _configured_chat_id
)

ALBUM_DELAY = 2.0
# Clear private command menus created by older versions; these IDs have no special
# permissions in this version of the bot.
OLD_COMMAND_CHAT_IDS = (8798455134, 438672098)

logger = logging.getLogger("college_suggest")
router = Router()
db = Storage(DB_PATH)
album_jobs: dict[tuple[int, str], asyncio.Task] = {}


def destination_chat_id() -> str | int | None:
    value = db.get_setting("channel_id")
    # Migrate either previous built-in default to the requested group.
    if value in LEGACY_DEFAULT_CHAT_IDS and DEFAULT_CHAT_ID == REQUESTED_DEFAULT_CHAT_ID:
        value = DEFAULT_CHAT_ID
        db.set_setting("channel_id", value)
    value = value or DEFAULT_CHAT_ID
    if not value:
        return None
    return int(value) if value.lstrip("-").isdigit() else value


async def safe_notify(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError as exc:
        logger.info("Cannot notify user %s: %s", chat_id, exc)


async def copy_submission(bot: Bot, destination: int | str, sub_id: int) -> list[int]:
    """Copy a message or a complete media album into the destination chat."""
    sub = db.get(sub_id)
    message_ids = db.message_ids(sub_id)
    if not sub or not message_ids:
        raise ValueError("Submission missing")
    if len(message_ids) == 1:
        copied = await bot.copy_message(
            chat_id=destination,
            from_chat_id=sub["user_id"],
            message_id=message_ids[0],
        )
        return [copied.message_id]

    copied_messages = await bot.copy_messages(
        chat_id=destination,
        from_chat_id=sub["user_id"],
        message_ids=message_ids,
    )
    if len(copied_messages) != len(message_ids):
        raise ValueError("Partial album copy; check the destination manually")
    return [item.message_id for item in copied_messages]


async def publish_submission(bot: Bot, sub_id: int) -> None:
    """Publish immediately without moderation or administrator approval."""
    if not db.move(sub_id, "collecting", "publishing"):
        return

    sub = db.get(sub_id)
    if not sub:
        logger.error("Submission %s disappeared before publication", sub_id)
        return

    target = destination_chat_id()
    if not target:
        db.move(sub_id, "publishing", "delivery_failed")
        await safe_notify(
            bot,
            sub["user_id"],
            "Не настроен чат для публикаций. Сообщение не отправлено.",
        )
        return

    try:
        await copy_submission(bot, target, sub_id)
    except (TelegramAPIError, ValueError) as exc:
        # A timeout can happen after Telegram accepted a post, so don't auto-retry
        # and risk posting duplicates.
        db.move(sub_id, "publishing", "publish_uncertain")
        logger.exception("Publication status uncertain for %s: %s", sub_id, exc)
        await safe_notify(
            bot,
            sub["user_id"],
            "Не удалось подтвердить публикацию. Проверь, что бот добавлен в канал или группу.",
        )
        return

    db.move(sub_id, "publishing", "published")
    await safe_notify(bot, sub["user_id"], "✅ Сообщение отправлено в канал.")


async def finish_album(bot: Bot, sub_id: int, key: tuple[int, str]) -> None:
    try:
        # Telegram sends each album item as a separate update; wait for all of them.
        await asyncio.sleep(ALBUM_DELAY)
        await publish_submission(bot, sub_id)
    finally:
        if album_jobs.get(key) is asyncio.current_task():
            album_jobs.pop(key, None)


async def configure_commands(bot: Bot) -> None:
    """Remove moderation commands from older bot versions."""
    scopes = [
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
        *(BotCommandScopeChat(chat_id=chat_id) for chat_id in OLD_COMMAND_CHAT_IDS),
    ]
    for scope in scopes:
        try:
            await bot.delete_my_commands(scope=scope)
        except TelegramAPIError as exc:
            # A former private chat may not have been opened; this is harmless.
            if isinstance(scope, BotCommandScopeChat):
                logger.debug("Could not clear an old private command menu: %s", exc)
            else:
                logger.warning("Could not clear old bot commands (%s): %s", type(scope).__name__, exc)

    commands = [BotCommand(command="start", description="Отправить сообщение в канал")]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramAPIError as exc:
        logger.warning("Could not reset the bot menu button: %s", exc)


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message) -> None:
    await message.answer(
        "<b>Предложка</b>\n\n"
        "Отправь сюда текст, фото, видео, GIF, стикер, файл или альбом — "
        "сообщение сразу отправится в канал."
    )


@router.message(F.chat.type == "private")
async def inbox(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return

    if message.text and message.text.startswith("/"):
        await message.answer("Отправь текст или медиа без команды.")
        return

    if message.content_type in {
        "new_chat_members",
        "left_chat_member",
        "pinned_message",
        "successful_payment",
        "invoice",
        "giveaway",
        "giveaway_winners",
        "paid_media",
        "forum_topic_created",
    }:
        await message.answer(
            "Этот служебный тип сообщения нельзя опубликовать. Отправь текст или медиа."
        )
        return

    try:
        sub_id, fresh = db.add(
            user_id=message.from_user.id,
            full_name=message.from_user.full_name,
            username=message.from_user.username,
            message_id=message.message_id,
            album_key=message.media_group_id,
        )
    except ValueError:
        await message.answer("Этот альбом уже отправлен. Попробуй отправить его ещё раз.")
        return

    if message.media_group_id:
        key = (message.from_user.id, message.media_group_id)
        old_job = album_jobs.pop(key, None)
        if old_job:
            old_job.cancel()
        album_jobs[key] = asyncio.create_task(finish_album(bot, sub_id, key))
    elif fresh:
        await publish_submission(bot, sub_id)


async def recover(bot: Bot) -> None:
    await asyncio.sleep(5)
    for sub_id in db.recoverable_ids():
        sub = db.get(sub_id)
        key = (sub["user_id"], sub["album_key"]) if sub and sub["album_key"] else None
        if key and key in album_jobs:
            continue
        await publish_submission(bot, sub_id)


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
        recovery_job = asyncio.create_task(recover(bot))
        logger.info("Started. Publication destination %s", destination_chat_id())
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
