"""Telegram suggestion bot with group-based moderation. Python 3.11+, aiogram 3."""
from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
    ReplyParameters,
)
from dotenv import load_dotenv

from storage import Storage

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", str(ROOT / "suggestions.sqlite3"))

REQUESTED_DEFAULT_CHAT_ID = "-5106639548"
LEGACY_DEFAULT_CHAT_IDS = {"-1004422721133", "-1001358443115"}
# These IDs are used only to remove obsolete private command menus from older versions.
LEGACY_COMMAND_CHAT_IDS = (8798455134, 438672098)
DESTINATION_ENV = os.getenv("DESTINATION_CHAT_ID", os.getenv("CHANNEL_ID", "")).strip()
MODERATION_ENV = os.getenv("MODERATION_CHAT_ID", "").strip()
DEFAULT_CHAT_ID = (
    REQUESTED_DEFAULT_CHAT_ID
    if not DESTINATION_ENV or DESTINATION_ENV in LEGACY_DEFAULT_CHAT_IDS
    else DESTINATION_ENV
)
CHANNEL_DISCLAIMER = (
    "This channel fully complies with all platform rules and community guidelines. "
    "Content is intended for legal and acceptable use only.\n\n"
    "Attention: We strictly comply with laws and ethical standards. "
    "All information is provided solely for research and educational purposes. "
    "We do not encourage or carry out unauthorized collection or dissemination of personal data."
)
TIMEZONE = os.getenv("TIMEZONE", "Europe/Riga")
ALBUM_DELAY = 2.0
REPLY_TIMEOUT = 15 * 60

logger = logging.getLogger("college_suggest")
router = Router()
db = Storage(DB_PATH)
album_jobs: dict[tuple[int, str], asyncio.Task] = {}
reply_sessions: dict[int, dict[str, object]] = {}
reply_jobs: dict[int, asyncio.Task] = {}
resolved_destination_chat_id: int | None = None
resolved_moderation_chat_id: int | None = None


def parse_chat_id(value: str | int | None) -> str | int | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    return int(value) if value.lstrip("-").isdigit() else value


def destination_chat_value() -> str | int | None:
    # An explicit environment value wins over an old value persisted in SQLite.
    value = DESTINATION_ENV or db.get_setting("channel_id") or DEFAULT_CHAT_ID
    if value in LEGACY_DEFAULT_CHAT_IDS and DEFAULT_CHAT_ID == REQUESTED_DEFAULT_CHAT_ID:
        value = REQUESTED_DEFAULT_CHAT_ID
        db.set_setting("channel_id", value)
    return parse_chat_id(value)


def destination_chat_id() -> str | int | None:
    return resolved_destination_chat_id or destination_chat_value()


def moderation_chat_value() -> str | int | None:
    # If no separate moderation group is configured, use the destination chat.
    return parse_chat_id(MODERATION_ENV) or destination_chat_value()


def moderation_chat_id() -> str | int | None:
    return resolved_moderation_chat_id or moderation_chat_value()


def same_chat(first: str | int | None, second: str | int | None) -> bool:
    return first is not None and second is not None and str(first) == str(second)


async def resolve_configured_chats(bot: Bot) -> None:
    global resolved_destination_chat_id, resolved_moderation_chat_id

    destination = destination_chat_value()
    moderation = moderation_chat_value()
    if destination is not None:
        try:
            resolved_destination_chat_id = (await bot.get_chat(destination)).id
        except TelegramAPIError as exc:
            logger.warning("Could not resolve publication chat %s: %s", destination, exc)
    if moderation is not None:
        try:
            resolved_moderation_chat_id = (await bot.get_chat(moderation)).id
        except TelegramAPIError as exc:
            logger.warning("Could not resolve moderation group %s: %s", moderation, exc)


def buttons(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"mod:yes:{sub_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod:no:{sub_id}"),
            ],
            [
                InlineKeyboardButton(text="💬 Ответить", callback_data=f"mod:reply:{sub_id}"),
                InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"mod:block:{sub_id}"),
            ],
        ]
    )


def reply_cancel_buttons(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отменить ответ", callback_data=f"mod:cancel:{sub_id}")]
        ]
    )


def sent_at(utc_value: str) -> str:
    try:
        tz = ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    dt = datetime.fromisoformat(utc_value.replace(" ", "T")).replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def card_text(sub, status: str = "pending") -> str:
    name = html.escape(sub["full_name"] or "Не указано")
    username = ("@" + html.escape(sub["username"])) if sub["username"] else "Без username"
    header = "🆕 <b>Новая заявка</b>" if status == "pending" else "<b>Заявка обработана</b>"
    text = (
        f"{header}\n━━━━━━━━━━━━━━\n"
        f"👤 <b>Отправитель:</b> {username}\n"
        f"🪪 <b>ID:</b> <code>{sub['user_id']}</code>\n"
        f"📝 <b>Имя:</b> {name}\n"
        f"🕓 <b>Отправлено:</b> {sent_at(sub['created_at'])}"
    )
    statuses = {
        "published": "\n\n✅ <b>Принято</b>",
        "rejected": "\n\n❌ <b>Отклонено</b>",
        "blocked": "\n\n🚫 <b>Пользователь заблокирован</b>",
        "publish_uncertain": "\n\n⚠️ <b>Не удалось подтвердить публикацию — проверьте чат вручную</b>",
        "delivery_failed": "\n\n⚠️ <b>Не удалось передать заявку в группу</b>",
    }
    return text + statuses.get(status, "")


def quote_preview(message: Message) -> str:
    if message.text:
        value = message.text.strip()
    elif message.caption:
        value = message.caption.strip()
    else:
        labels = {
            "photo": "[Фото]",
            "video": "[Видео]",
            "video_note": "[Видеосообщение]",
            "voice": "[Голосовое сообщение]",
            "audio": "[Аудио]",
            "document": "[Файл]",
            "animation": "[GIF]",
            "sticker": "[Стикер]",
            "contact": "[Контакт]",
            "location": "[Геолокация]",
            "venue": "[Место]",
            "poll": "[Опрос]",
        }
        value = labels.get(message.content_type, "[Сообщение]")
    value = value or "[Сообщение]"
    return value[:700] + ("…" if len(value) > 700 else "")


async def safe_notify(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramBadRequest:
        try:
            await bot.send_message(chat_id, html.escape(text))
        except TelegramAPIError as exc:
            logger.info("Cannot notify user %s: %s", chat_id, exc)
    except TelegramAPIError as exc:
        logger.info("Cannot notify user %s: %s", chat_id, exc)


async def copy_submission(bot: Bot, destination: int | str, sub_id: int) -> list[int]:
    """Copy one message or a complete album from the user's private chat."""
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


async def send_to_moderation_group(bot: Bot, sub_id: int) -> None:
    if not db.move(sub_id, "collecting", "notifying"):
        return
    sub = db.get(sub_id)
    if not sub:
        logger.error("Submission %s disappeared before moderation delivery", sub_id)
        return

    target = moderation_chat_id()
    if not target:
        db.move(sub_id, "notifying", "delivery_failed")
        await safe_notify(bot, sub["user_id"], "Не настроена группа для проверки предложений.")
        return

    try:
        await copy_submission(bot, target, sub_id)
        card = await bot.send_message(
            chat_id=target,
            text=card_text(sub),
            reply_markup=buttons(sub_id),
        )
    except (TelegramAPIError, ValueError) as exc:
        db.move(sub_id, "notifying", "delivery_failed")
        logger.exception("Could not deliver submission %s to moderation group: %s", sub_id, exc)
        await safe_notify(
            bot,
            sub["user_id"],
            "Не удалось передать предложение на проверку. Проверь, что бот добавлен в группу.",
        )
        return

    db.set_card_pending(sub_id, card.message_id)
    await safe_notify(bot, sub["user_id"], "✅ Предложение отправлено на проверку.")


async def finish_album(bot: Bot, sub_id: int, key: tuple[int, str]) -> None:
    try:
        # Each album item arrives as a separate Telegram update.
        await asyncio.sleep(ALBUM_DELAY)
        await send_to_moderation_group(bot, sub_id)
    finally:
        if album_jobs.get(key) is asyncio.current_task():
            album_jobs.pop(key, None)


async def configure_commands(bot: Bot) -> None:
    scopes = [
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
        *(BotCommandScopeChat(chat_id=chat_id) for chat_id in LEGACY_COMMAND_CHAT_IDS),
    ]
    moderation_id = moderation_chat_id()
    if isinstance(moderation_id, int):
        scopes.append(BotCommandScopeChat(chat_id=moderation_id))

    for scope in scopes:
        for language in (None, "ru", "en"):
            try:
                kwargs = {"scope": scope}
                if language:
                    kwargs["language_code"] = language
                await bot.delete_my_commands(**kwargs)
            except TelegramAPIError as exc:
                logger.debug("Could not clear old bot commands (%s): %s", type(scope).__name__, exc)

    start_command = [BotCommand(command="start", description="Отправить предложение")]
    await bot.set_my_commands(start_command, scope=BotCommandScopeDefault())
    await bot.set_my_commands(start_command, scope=BotCommandScopeAllPrivateChats())

    if isinstance(moderation_id, int):
        try:
            await bot.set_my_commands(
                [BotCommand(command="unblock", description="Разблокировать пользователя")],
                scope=BotCommandScopeChat(chat_id=moderation_id),
            )
        except TelegramAPIError as exc:
            logger.warning("Could not set moderation-group commands: %s", exc)

    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramAPIError as exc:
        logger.warning("Could not reset bot menu button: %s", exc)


def clear_reply_session(user_id: int) -> dict[str, object] | None:
    session = reply_sessions.pop(user_id, None)
    task = reply_jobs.pop(user_id, None)
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
    return session


async def edit_reply_prompt(bot: Bot, chat_id: int, prompt_message_id: int, text: str) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=prompt_message_id,
            text=text,
            reply_markup=None,
        )
    except TelegramAPIError as exc:
        logger.info("Could not update reply prompt %s/%s: %s", chat_id, prompt_message_id, exc)


async def expire_reply_session(
    bot: Bot,
    user_id: int,
    sub_id: int,
    chat_id: int,
    prompt_message_id: int,
) -> None:
    await asyncio.sleep(REPLY_TIMEOUT)
    session = reply_sessions.get(user_id)
    if (
        session
        and session.get("sub_id") == sub_id
        and session.get("prompt_message_id") == prompt_message_id
    ):
        reply_sessions.pop(user_id, None)
        if reply_jobs.get(user_id) is asyncio.current_task():
            reply_jobs.pop(user_id, None)
        await edit_reply_prompt(
            bot,
            chat_id,
            prompt_message_id,
            "⌛ <b>Время ответа истекло.</b>\nНажми «Ответить» на карточке заявки, чтобы начать заново.",
        )


async def deliver_group_reply(bot: Bot, message: Message, session: dict[str, object]) -> None:
    target_user_id = int(session["user_id"])
    source_ids = db.message_ids(int(session["sub_id"]))
    reply_parameters = (
        ReplyParameters(message_id=source_ids[0], allow_sending_without_reply=True)
        if source_ids else None
    )

    if message.text is not None:
        combined = f"💬 <b>Ответ на твоё предложение</b>\n\n{html.escape(message.text)}"
        if len(combined) <= 3900:
            await bot.send_message(
                target_user_id,
                combined,
                reply_parameters=reply_parameters,
            )
            return

    await bot.copy_message(
        chat_id=target_user_id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        reply_parameters=reply_parameters,
    )


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message) -> None:
    if not message.from_user:
        return
    if db.is_blocked(message.from_user.id):
        await message.answer("🚫 Доступ к отправке предложений заблокирован.")
        return
    await message.answer(
        "<b>Предложка</b>\n\n"
        "Отправь сюда текст, фото, видео, кружок, голосовое, GIF, стикер или файл. "
        "Предложение появится в группе на проверке."
    )


@router.message(Command("unblock"), F.chat.type.in_({"group", "supergroup"}))
async def unblock_command(message: Message) -> None:
    if message.chat.id != moderation_chat_id():
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        await message.answer("Используй: <code>/unblock USER_ID</code>")
        return
    target_user_id = int(parts[1].strip())
    if db.unblock_user(target_user_id):
        await message.answer(f"✅ Пользователь <code>{target_user_id}</code> разблокирован.")
    else:
        await message.answer(f"Пользователь <code>{target_user_id}</code> не был заблокирован.")


@router.message(F.chat.type == "private")
async def inbox(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    sender_id = message.from_user.id

    if db.is_blocked(sender_id):
        await message.answer("🚫 Доступ к отправке предложений заблокирован.")
        return

    if message.text and message.text.startswith("/"):
        await message.answer("Нажми /start, чтобы начать, или отправь текст/медиа без команды.")
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
        await message.answer("Этот служебный тип сообщения нельзя отправить на проверку. Пришли текст или медиа.")
        return

    try:
        sub_id, fresh = db.add(
            user_id=sender_id,
            full_name=message.from_user.full_name,
            username=message.from_user.username,
            message_id=message.message_id,
            album_key=message.media_group_id,
            quote_text=quote_preview(message),
        )
    except ValueError:
        await message.answer("Этот альбом уже отправлен. Попробуй отправить его ещё раз.")
        return

    if message.media_group_id:
        key = (sender_id, message.media_group_id)
        old_job = album_jobs.pop(key, None)
        if old_job:
            old_job.cancel()
        album_jobs[key] = asyncio.create_task(finish_album(bot, sub_id, key))
    elif fresh:
        await send_to_moderation_group(bot, sub_id)


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def group_reply_handler(message: Message, bot: Bot) -> None:
    # Ignore all group traffic except a reply to a bot's active answer prompt.
    if message.chat.id != moderation_chat_id() or not message.from_user:
        return
    session = reply_sessions.get(message.from_user.id)
    if not session:
        return
    prompt_id = int(session["prompt_message_id"])
    replied_to = message.reply_to_message
    if not replied_to or replied_to.message_id != prompt_id:
        return

    if asyncio.get_running_loop().time() >= float(session["deadline"]):
        old_session = clear_reply_session(message.from_user.id)
        if old_session:
            await edit_reply_prompt(
                bot,
                message.chat.id,
                int(old_session["prompt_message_id"]),
                "⌛ <b>Время ответа истекло.</b>",
            )
        return

    try:
        await deliver_group_reply(bot, message, session)
    except TelegramAPIError as exc:
        logger.warning(
            "Could not send group reply from %s to user %s: %s",
            message.from_user.id,
            session.get("user_id"),
            exc,
        )
        await message.answer(
            "❌ Не удалось доставить ответ. Возможно, пользователь заблокировал бота. "
            "Режим ответа остаётся активным — попробуй ещё раз."
        )
        return

    old_session = clear_reply_session(message.from_user.id)
    if old_session:
        await edit_reply_prompt(
            bot,
            message.chat.id,
            int(old_session["prompt_message_id"]),
            "✅ <b>Ответ отправлен пользователю.</b>",
        )
    await message.answer("✅ Ответ отправлен пользователю.")


async def update_moderation_card(bot: Bot, sub, status: str) -> None:
    target = moderation_chat_id()
    card_id = sub["admin_card_id"]
    if not target or not card_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=target,
            message_id=card_id,
            text=card_text(sub, status),
            reply_markup=None,
        )
    except TelegramAPIError as exc:
        logger.warning("Could not update moderation card %s: %s", sub["id"], exc)


@router.callback_query(F.data.startswith("mod:"))
async def moderate(callback: CallbackQuery, bot: Bot) -> None:
    message = callback.message
    if not message or message.chat.id != moderation_chat_id():
        await callback.answer("Кнопка доступна только в группе модерации.", show_alert=True)
        return

    try:
        _, action, raw_id = (callback.data or "").split(":")
        sub_id = int(raw_id)
    except (ValueError, TypeError):
        await callback.answer("Неверная заявка.", show_alert=True)
        return

    # Cancel buttons live on the reply prompt, not on the submission card.
    if action == "cancel":
        session = reply_sessions.get(callback.from_user.id)
        if (
            not session
            or session.get("sub_id") != sub_id
            or session.get("prompt_message_id") != message.message_id
        ):
            await callback.answer("Отменить можно только собственный ответ.", show_alert=True)
            return
        clear_reply_session(callback.from_user.id)
        try:
            await message.edit_text("❌ <b>Ответ отменён.</b>", reply_markup=None)
        except TelegramAPIError:
            pass
        await callback.answer("Ответ отменён.")
        return

    sub = db.get(sub_id)
    if not sub:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if message.message_id != sub["admin_card_id"]:
        await callback.answer("Эта кнопка не относится к карточке заявки.", show_alert=True)
        return
    if sub["status"] != "pending":
        await callback.answer("Заявка уже обработана.")
        return

    if action == "reply":
        existing = reply_sessions.get(callback.from_user.id)
        if existing and asyncio.get_running_loop().time() < float(existing["deadline"]):
            await callback.answer(
                "У тебя уже открыт ответ. Ответь на сообщение-напоминание или отмени его.",
                show_alert=True,
            )
            return
        if existing:
            old_session = clear_reply_session(callback.from_user.id)
            if old_session:
                await edit_reply_prompt(
                    bot,
                    message.chat.id,
                    int(old_session["prompt_message_id"]),
                    "⌛ <b>Время ответа истекло.</b>",
                )

        prompt = await bot.send_message(
            chat_id=message.chat.id,
            text=(
                "✍️ <b>Ответь на это сообщение, чтобы отправить ответ пользователю.</b>\n"
                "У тебя есть <b>15 минут</b>. Ответ придёт пользователю в личку с цитатой его предложения.\n\n"
                f"<b>Исходное сообщение:</b>\n<blockquote>"
                f"{html.escape(str(sub['quote_text'] or '[Сообщение]'))}</blockquote>"
            ),
            reply_markup=reply_cancel_buttons(sub_id),
            reply_parameters=ReplyParameters(
                message_id=message.message_id,
                allow_sending_without_reply=True,
            ),
        )
        reply_sessions[callback.from_user.id] = {
            "sub_id": sub_id,
            "user_id": sub["user_id"],
            "quote_text": sub["quote_text"] or "[Сообщение]",
            "prompt_message_id": prompt.message_id,
            "deadline": asyncio.get_running_loop().time() + REPLY_TIMEOUT,
        }
        reply_jobs[callback.from_user.id] = asyncio.create_task(
            expire_reply_session(bot, callback.from_user.id, sub_id, message.chat.id, prompt.message_id)
        )
        await callback.answer("Ответ открыт. Ответь на сообщение бота в группе.")
        return

    if action == "yes":
        target = destination_chat_id()
        mod_target = moderation_chat_id()
        if not target:
            await callback.answer("Не настроен чат для публикации.", show_alert=True)
            return
        if not db.move(sub_id, "pending", "publishing"):
            await callback.answer("Заявка уже обрабатывается.")
            return

        # If both chats are the same, the submitted content is already in that group.
        # Do not copy it a second time.
        if not same_chat(target, mod_target):
            try:
                published_message_ids = await copy_submission(bot, target, sub_id)
            except (TelegramAPIError, ValueError) as exc:
                db.move(sub_id, "publishing", "publish_uncertain")
                logger.exception("Publication status uncertain for %s: %s", sub_id, exc)
                sub = db.get(sub_id)
                if sub:
                    await update_moderation_card(bot, sub, "publish_uncertain")
                await callback.answer("Ошибка публикации. Проверь чат вручную.", show_alert=True)
                return

            try:
                await bot.send_message(
                    chat_id=target,
                    text=f"<blockquote>{html.escape(CHANNEL_DISCLAIMER)}</blockquote>",
                    reply_parameters=ReplyParameters(
                        message_id=published_message_ids[0],
                        allow_sending_without_reply=True,
                    ),
                )
            except TelegramAPIError as exc:
                logger.warning("Published submission %s but could not send disclaimer: %s", sub_id, exc)

        db.move(sub_id, "publishing", "published")
        new_status = "published"
        notice = "✅ Твоё предложение принято и опубликовано!"
        toast = "Принято"
    elif action == "no":
        if not db.move(sub_id, "pending", "rejected"):
            await callback.answer("Заявка уже обрабатывается.")
            return
        new_status = "rejected"
        notice = "❌ Твоё предложение не принято."
        toast = "Отклонено"
    elif action == "block":
        if not db.move(sub_id, "pending", "blocked"):
            await callback.answer("Заявка уже обрабатывается.")
            return
        db.block_user(sub["user_id"], callback.from_user.id)
        new_status = "blocked"
        notice = "🚫 Доступ к отправке предложений заблокирован."
        toast = "Пользователь заблокирован"
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    current_sub = db.get(sub_id) or sub
    await update_moderation_card(bot, current_sub, new_status)
    await callback.answer(toast)
    await safe_notify(bot, sub["user_id"], notice)


async def recover(bot: Bot) -> None:
    await asyncio.sleep(5)
    for sub_id in db.recoverable_ids():
        sub = db.get(sub_id)
        key = (sub["user_id"], sub["album_key"]) if sub and sub["album_key"] else None
        if key and key in album_jobs:
            continue
        await send_to_moderation_group(bot, sub_id)


async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_BOT_TOKEN_HERE":
        raise SystemExit("Заполни BOT_TOKEN в переменных окружения хостинга или в .env.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        me = await bot.get_me()
        await resolve_configured_chats(bot)
        await configure_commands(bot)
        logger.info(
            "Started as @%s. Moderation group: %s. Publication chat: %s",
            me.username,
            moderation_chat_id(),
            destination_chat_id(),
        )
        recovery_job = asyncio.create_task(recover(bot))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if "recovery_job" in locals():
            recovery_job.cancel()
        for job in album_jobs.values():
            job.cancel()
        for job in reply_jobs.values():
            job.cancel()
        db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
