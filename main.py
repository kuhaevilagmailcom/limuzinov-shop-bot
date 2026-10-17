"""Telegram submission/moderation bot. Python 3.11+, aiogram 3, SQLite."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
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
DEFAULT_ADMIN_IDS = "8798455134,438672098"
_raw_admin_ids = os.getenv("ADMIN_IDS", DEFAULT_ADMIN_IDS)
ADMIN_IDS = tuple(
    dict.fromkeys(
        int(part.strip())
        for part in _raw_admin_ids.split(",")
        if part.strip().isdigit()
    )
) or (8798455134, 438672098)
# Keep the old singular name as an alias for legacy records/configuration.
ADMIN_ID = ADMIN_IDS[0]
DB_PATH = os.getenv("DB_PATH", str(ROOT / "suggestions.sqlite3"))
DEFAULT_CHANNEL_ID = os.getenv("CHANNEL_ID", "-1004422721133").strip() or "-1004422721133"
STICKER_PACK = os.getenv("EMOJI_PACK", "vector_icons_by_fStikBot").strip()
TIMEZONE = os.getenv("TIMEZONE", "Asia/Yekaterinburg")
CONFIRM_EMOJI_ID = "5219899949281453881"
ALBUM_DELAY = 2.0
REPLY_TIMEOUT = 15 * 60

logger = logging.getLogger("college_suggest")
router = Router()
db = Storage(DB_PATH)
album_jobs: dict[tuple[int, str], asyncio.Task] = {}
reply_sessions: dict[int, dict[str, object]] = {}
reply_jobs: dict[int, asyncio.Task] = {}
expired_reply_admins: set[int] = set()

icons = {"ok": CONFIRM_EMOJI_ID}
ALT = {
    "sender": "🤖",
    "id": "🪪",
    "name": "👤",
    "time": "🕓",
    "ok": "✅",
    "reject": "❌",
    "reply": "💬",
    "block": "🚫",
}


def glyph(role: str, *, premium: bool = True) -> str:
    fallback = ALT[role]
    code = icons.get(role) if premium else None
    return f'<tg-emoji emoji-id="{code}">{fallback}</tg-emoji>' if code else fallback


def simple_html(text: str) -> str:
    """Keep readable Unicode fallback if Telegram does not allow custom emoji."""
    return re.sub(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', r"\1", text)


async def load_pack(bot: Bot) -> None:
    if not STICKER_PACK:
        return
    try:
        sticker_set = await bot.get_sticker_set(STICKER_PACK)
        matches = {
            s.emoji.replace("\ufe0f", ""): s.custom_emoji_id
            for s in sticker_set.stickers
            if s.emoji and s.custom_emoji_id
        }
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

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("Принять", "yes", "ok", "success"),
                button("Отклонить", "no", "reject", "danger"),
            ],
            [
                button("Ответить", "reply", "reply", "primary"),
                button("Заблокировать", "block", "block", "danger"),
            ],
        ]
    )


def reply_cancel_buttons(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Отменить ответ",
                callback_data=f"mod:cancel:{sub_id}",
            )
        ]]
    )


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
    text = (
        f"{header}\n━━━━━━━━━━━━━━\n"
        f"{glyph('sender', premium=premium)} <b>Отправитель:</b> {sender}\n"
        f"{glyph('id', premium=premium)} <b>ID:</b> <code>{sub['user_id']}</code>\n"
        f"{glyph('name', premium=premium)} <b>Имя:</b> {name}\n"
        f"{glyph('time', premium=premium)} <b>Отправлено:</b> {sent_at(sub['created_at'])}"
    )
    if status == "published":
        text += f"\n\n{glyph('ok', premium=premium)} <b>Принято</b>"
    elif status == "rejected":
        text += f"\n\n{glyph('reject', premium=premium)} <b>Не принято</b>"
    elif status == "blocked":
        text += f"\n\n{glyph('block', premium=premium)} <b>Пользователь заблокирован</b>"
    return text


def quote_preview(message: Message) -> str:
    """Plain-text representation saved so admin replies can quote the source."""
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
        await bot.copy_message(
            chat_id=destination,
            from_chat_id=sub["user_id"],
            message_id=message_ids[0],
        )
    else:
        result = await bot.copy_messages(
            chat_id=destination,
            from_chat_id=sub["user_id"],
            message_ids=message_ids,
        )
        if len(result) != len(message_ids):
            raise ValueError("Partial album copy; review destination before retry")


async def send_to_admin(bot: Bot, sub_id: int) -> None:
    if not db.move(sub_id, "collecting", "notifying"):
        return
    sub = db.get(sub_id)
    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            await copy_submission(bot, admin_id, sub_id)
            try:
                card = await bot.send_message(
                    admin_id,
                    card_text(sub),
                    reply_markup=buttons(sub_id),
                )
            except TelegramBadRequest:
                card = await bot.send_message(
                    admin_id,
                    card_text(sub, premium=False),
                    reply_markup=buttons(sub_id, premium=False),
                )
            db.set_card_pending(sub_id, card.message_id, admin_id=admin_id)
            delivered += 1
        except (TelegramAPIError, ValueError) as exc:
            logger.exception("Admin delivery failed for submission %s to %s: %s", sub_id, admin_id, exc)

    if delivered:
        if delivered < len(ADMIN_IDS):
            logger.warning(
                "Submission %s delivered to %s/%s admins. Check /start and bot access for each admin.",
                sub_id, delivered, len(ADMIN_IDS),
            )
        await safe_notify(bot, sub["user_id"], f"{glyph('ok')} Предложение отправлено на проверку.")
        return

    db.move(sub_id, "notifying", "delivery_failed")
    await safe_notify(
        bot,
        sub["user_id"],
        "Не удалось передать предложение на проверку. Попробуйте позже.",
    )


async def finish_album(bot: Bot, sub_id: int, key: tuple[int, str]) -> None:
    try:
        await asyncio.sleep(ALBUM_DELAY)
        await send_to_admin(bot, sub_id)
    finally:
        if album_jobs.get(key) is asyncio.current_task():
            album_jobs.pop(key, None)


async def configure_commands(bot: Bot) -> None:
    """Clear old Telegram command scopes left by previous bot versions."""
    scopes = [
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
        *(BotCommandScopeChat(chat_id=admin_id) for admin_id in ADMIN_IDS),
    ]
    for scope in scopes:
        for lang in ("", "ru", "en"):
            try:
                await bot.delete_my_commands(scope=scope, language_code=lang)
            except TelegramAPIError as exc:
                logger.warning(
                    "Failed to clear previous bot commands (%s, %s): %s",
                    type(scope).__name__, lang, exc,
                )

    user_commands = [BotCommand(command="start", description="Предложить сообщение")]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    await bot.set_my_commands(user_commands, scope=BotCommandScopeAllPrivateChats())
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramAPIError as exc:
        logger.warning("Could not reset old bot menu button: %s", exc)
    await ensure_admin_commands(bot)


async def ensure_admin_commands(bot: Bot) -> None:
    """Give all configured admins the channel command in their private-chat menu."""
    admin_commands = [
        BotCommand(command="start", description="Предложить сообщение"),
        BotCommand(command="channel", description="Канал для публикаций"),
        BotCommand(command="unblock", description="Разблокировать пользователя"),
    ]
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
            await bot.set_chat_menu_button(
                chat_id=admin_id,
                menu_button=MenuButtonCommands(),
            )
        except TelegramAPIError as exc:
            # Each admin must open the bot and press /start before the bot can DM them.
            logger.warning("Cannot configure admin %s yet: %s", admin_id, exc)


async def edit_reply_prompt(
    bot: Bot,
    admin_id: int,
    prompt_message_id: int,
    text: str,
) -> None:
    try:
        await bot.edit_message_text(
            chat_id=admin_id,
            message_id=prompt_message_id,
            text=text,
            reply_markup=None,
        )
    except TelegramAPIError as exc:
        logger.info("Could not update reply prompt for admin %s: %s", admin_id, exc)


def clear_reply_session(admin_id: int) -> dict[str, object] | None:
    session = reply_sessions.pop(admin_id, None)
    task = reply_jobs.pop(admin_id, None)
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
    expired_reply_admins.discard(admin_id)
    return session


async def expire_reply_session(
    bot: Bot,
    admin_id: int,
    sub_id: int,
    prompt_message_id: int,
) -> None:
    await asyncio.sleep(REPLY_TIMEOUT)
    session = reply_sessions.get(admin_id)
    if (
        session
        and session.get("sub_id") == sub_id
        and session.get("prompt_message_id") == prompt_message_id
    ):
        reply_sessions.pop(admin_id, None)
        expired_reply_admins.add(admin_id)
        if reply_jobs.get(admin_id) is asyncio.current_task():
            reply_jobs.pop(admin_id, None)
        await edit_reply_prompt(
            bot,
            admin_id,
            prompt_message_id,
            "⌛ <b>Время ответа истекло.</b>\nНажми «Ответить» на карточке заявки, чтобы начать заново.",
        )


async def deliver_admin_reply(bot: Bot, message: Message, session: dict[str, object]) -> None:
    target_user_id = int(session["user_id"])
    source_ids = db.message_ids(int(session["sub_id"]))
    # The original submission is still in this user's private bot chat, so Telegram can
    # display a native reply/quote automatically instead of sending a text imitation.
    reply_parameters = (
        ReplyParameters(message_id=source_ids[0], allow_sending_without_reply=True)
        if source_ids else None
    )

    if message.text is not None:
        escaped_reply = html.escape(message.text)
        combined = f"💬 <b>Ответ администратора</b>\n\n{escaped_reply}"
        # Copy very long text verbatim if adding the heading or escaping would exceed the limit.
        if len(combined) <= 3900:
            await bot.send_message(
                target_user_id, combined, reply_parameters=reply_parameters
            )
            return

    await bot.copy_message(
        chat_id=target_user_id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        reply_parameters=reply_parameters,
    )


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id if message.from_user else None
    if user_id in ADMIN_IDS:
        await ensure_admin_commands(bot)
    elif user_id is not None and db.is_blocked(user_id):
        await message.answer("🚫 Доступ к отправке предложений заблокирован администрацией.")
        return
    await message.answer(
        "<b>Предложка</b>\n\n"
        "Отправь сюда текст, фото, видео, кружок, голосовое или файл."
    )


@router.message(Command("channel"), F.chat.type == "private")
async def set_channel(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        current = html.escape(str(channel_id() or "не подключён"))
        await message.answer(
            f"<b>Канал:</b> <code>{current}</code>\n\n"
            "Установить: <code>/channel @username</code> или <code>/channel -100...</code>"
        )
        return
    value = parts[1].strip()
    if not (
        (value.startswith("@") and re.fullmatch(r"@[a-zA-Z0-9_]{5,32}", value))
        or (value.startswith("-100") and value[1:].isdigit())
    ):
        await message.answer("Укажи @username канала либо числовой ID вида -100...")
        return
    target = int(value) if value.startswith("-100") else value
    try:
        ch = await bot.get_chat(target)
        member = await bot.get_chat_member(ch.id, (await bot.get_me()).id)
        if (
            ch.type != "channel"
            or member.status not in ("administrator", "creator")
            or not getattr(member, "can_post_messages", False)
        ):
            await message.answer("Добавь бота администратором канала с правом публикации.")
            return
    except TelegramAPIError:
        await message.answer("Не могу получить доступ к каналу. Проверь права бота и адрес.")
        return
    db.set_setting("channel_id", str(ch.id))
    await message.answer(
        f"{glyph('ok')} Канал подключён: <b>{html.escape(ch.title or str(ch.id))}</b>."
    )


@router.message(Command("unblock"), F.chat.type == "private")
async def unblock_command(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
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
    if sender_id in ADMIN_IDS:
        session = reply_sessions.get(sender_id)
        if session and time.monotonic() >= float(session["deadline"]):
            old_session = clear_reply_session(sender_id)
            expired_reply_admins.add(sender_id)
            await edit_reply_prompt(
                bot,
                sender_id,
                int(old_session["prompt_message_id"]),
                "⌛ <b>Время ответа истекло.</b>\nНажми «Ответить» на карточке заявки, чтобы начать заново.",
            )
            await message.answer("⌛ Время истекло. Ответ не отправлен — нажми «Ответить» заново.")
            return

        if session:
            try:
                await deliver_admin_reply(bot, message, session)
            except TelegramAPIError as exc:
                logger.warning(
                    "Could not send admin reply from %s to user %s: %s",
                    sender_id, session.get("user_id"), exc,
                )
                await message.answer(
                    "❌ Не удалось доставить ответ. Возможно, пользователь заблокировал бота. "
                    "Режим ответа остаётся активным — попробуй ещё раз или отмени его."
                )
                return

            prompt_id = int(session["prompt_message_id"])
            clear_reply_session(sender_id)
            await edit_reply_prompt(
                bot,
                sender_id,
                prompt_id,
                "✅ <b>Ответ отправлен пользователю.</b>",
            )
            await message.answer("✅ Ответ отправлен пользователю.")
            return

        if sender_id in expired_reply_admins:
            expired_reply_admins.discard(sender_id)
            await message.answer(
                "⌛ Предыдущая сессия ответа истекла. Сообщение не отправлено. "
                "Нажми «Ответить» на карточке заявки, чтобы начать заново."
            )
            return

    if sender_id not in ADMIN_IDS and db.is_blocked(sender_id):
        await message.answer("🚫 Доступ к отправке предложений заблокирован администрацией.")
        return

    if message.text and message.text.startswith("/"):
        await message.answer("Пришли сюда текст или медиа — без команд.")
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
            "Этот тип сообщения не поддерживается Telegram для публикации. Отправь текст или медиа."
        )
        return
    try:
        sub_id, fresh = db.add(
            user_id=message.from_user.id,
            full_name=message.from_user.full_name,
            username=message.from_user.username,
            message_id=message.message_id,
            album_key=message.media_group_id,
            quote_text=quote_preview(message),
        )
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


async def update_moderation_cards(
    bot: Bot,
    sub_id: int,
    sub,
    status: str,
    clicked_message: Message | None,
) -> None:
    cards = set(db.admin_cards(sub_id))
    if sub["admin_card_id"] and not cards:
        # Compatibility fallback for cards created before the multi-admin migration.
        cards.add((ADMIN_ID, sub["admin_card_id"]))
    if clicked_message:
        cards.add((clicked_message.chat.id, clicked_message.message_id))

    for admin_id, card_id in cards:
        try:
            await bot.edit_message_text(
                chat_id=admin_id,
                message_id=card_id,
                text=card_text(sub, status),
                reply_markup=None,
            )
        except TelegramBadRequest:
            try:
                await bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=card_id,
                    text=card_text(sub, status, premium=False),
                    reply_markup=None,
                )
            except TelegramAPIError as exc:
                logger.warning("Could not update admin card %s/%s: %s", admin_id, sub_id, exc)
        except TelegramAPIError as exc:
            logger.warning("Could not update admin card %s/%s: %s", admin_id, sub_id, exc)


@router.callback_query(F.data.startswith("mod:"))
async def moderate(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id not in ADMIN_IDS:
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

    if action == "cancel":
        session = reply_sessions.get(callback.from_user.id)
        if not session or session.get("sub_id") != sub_id:
            await callback.answer("Сессия ответа уже завершена или истекла.")
            return
        clear_reply_session(callback.from_user.id)
        if callback.message:
            try:
                await callback.message.edit_text("❌ <b>Ответ отменён.</b>", reply_markup=None)
            except TelegramAPIError as exc:
                logger.info("Could not edit cancelled reply prompt: %s", exc)
        await callback.answer("Ответ отменён.")
        return

    if sub["status"] != "pending":
        await callback.answer("Уже обработано.")
        return

    if action == "reply":
        existing = reply_sessions.get(callback.from_user.id)
        if existing and time.monotonic() < float(existing["deadline"]):
            await callback.answer(
                "У тебя уже открыт ответ. Отправь сообщение или отмени текущий ответ.",
                show_alert=True,
            )
            return
        if existing:
            old_session = clear_reply_session(callback.from_user.id)
            await edit_reply_prompt(
                bot,
                callback.from_user.id,
                int(old_session["prompt_message_id"]),
                "⌛ <b>Время ответа истекло.</b>",
            )

        try:
            prompt = await bot.send_message(
                chat_id=callback.from_user.id,
                text=(
                    "✍️ <b>Напишите ответ пользователю</b>\n\n"
                    "⏳ У вас есть <b>15 минут</b>. Исходное сообщение будет автоматически "
                    "добавлено в ответ как цитата.\n\n"
                    f"<b>Исходное сообщение:</b>\n<blockquote>"
                    f"{html.escape(str(sub['quote_text'] or '[Сообщение]'))}</blockquote>"
                ),
                reply_markup=reply_cancel_buttons(sub_id),
            )
        except TelegramAPIError as exc:
            logger.warning("Could not open reply mode for admin %s: %s", callback.from_user.id, exc)
            await callback.answer("Не удалось открыть ответ. Проверь, что бот запущен в личке.", show_alert=True)
            return

        expired_reply_admins.discard(callback.from_user.id)
        reply_sessions[callback.from_user.id] = {
            "sub_id": sub_id,
            "user_id": sub["user_id"],
            "quote_text": sub["quote_text"] or "[Сообщение]",
            "prompt_message_id": prompt.message_id,
            "deadline": time.monotonic() + REPLY_TIMEOUT,
        }
        reply_jobs[callback.from_user.id] = asyncio.create_task(
            expire_reply_session(bot, callback.from_user.id, sub_id, prompt.message_id)
        )
        await callback.answer("Режим ответа включён.")
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
        new_status = "published"
        notice = "✅ Твоё предложение принято и опубликовано!"
        toast = "Принято"
    elif action == "no":
        if not db.move(sub_id, "pending", "rejected"):
            await callback.answer("Уже обрабатывается.")
            return
        new_status = "rejected"
        notice = "❌ Твоё предложение не принято."
        toast = "Не принято"
    elif action == "block":
        if sub["user_id"] in ADMIN_IDS:
            await callback.answer("Нельзя заблокировать администратора.", show_alert=True)
            return
        if not db.move(sub_id, "pending", "blocked"):
            await callback.answer("Уже обрабатывается.")
            return
        db.block_user(sub["user_id"], callback.from_user.id)
        new_status = "blocked"
        notice = "🚫 Администрация заблокировала возможность отправлять предложения."
        toast = "Пользователь заблокирован"
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    await update_moderation_cards(bot, sub_id, sub, new_status, callback.message)
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
        logger.info("Started. Admins %s; channel %s", ADMIN_IDS, channel_id())
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
