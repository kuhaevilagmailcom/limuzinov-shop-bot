"""Telegram suggestion bot with group-based moderation. Python 3.11+, aiogram 3."""
from __future__ import annotations

import asyncio
import html
import json
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
    MessageEntity,
    ReplyParameters,
)
from dotenv import load_dotenv

from storage import Storage

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", str(ROOT / "suggestions.sqlite3"))

# Known moderation group ID from the existing deployment. Publication must be configured separately.
REQUESTED_DEFAULT_CHAT_ID = "-5106639548"
LEGACY_DEFAULT_CHAT_IDS = {"-1004422721133", "-1001358443115"}
# These IDs are used only to remove obsolete private command menus from older versions.
LEGACY_COMMAND_CHAT_IDS = (8798455134, 438672098)
# An empty DESTINATION_CHAT_ID must still allow the legacy CHANNEL_ID variable to work.
DESTINATION_ENV = (
    os.getenv("DESTINATION_CHAT_ID", "").strip()
    or os.getenv("CHANNEL_ID", "").strip()
)
MODERATION_ENV = os.getenv("MODERATION_CHAT_ID", "").strip()
TIMEZONE = os.getenv("TIMEZONE", "Europe/Riga")
ALBUM_DELAY = 2.0
REPLY_TIMEOUT = 15 * 60

PUBLICATION_DISCLAIMER = (
    "This channel fully complies with all platform rules and community guidelines. "
    "Content is intended for legal and acceptable use only Attention: We strictly comply with laws and ethical standards. "
    "All information is provided solely for research and educational purposes. We do not encourage or carry out unauthorized collection "
    "or dissemination of personal data"
)

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
    # Explicit environment settings win. Never silently default publication to the moderation group.
    value = DESTINATION_ENV or db.get_setting("channel_id")
    if not value:
        return None
    # Ignore known old defaults from earlier versions when they came from SQLite.
    if not DESTINATION_ENV and value in (LEGACY_DEFAULT_CHAT_IDS | {REQUESTED_DEFAULT_CHAT_ID}):
        return None
    return parse_chat_id(value)


def destination_chat_id() -> str | int | None:
    return (
        resolved_destination_chat_id
        if resolved_destination_chat_id is not None
        else destination_chat_value()
    )


def moderation_chat_value() -> str | int | None:
    # Keep the known moderation group as the fallback; never derive moderation from publication.
    configured = parse_chat_id(MODERATION_ENV)
    default_group = parse_chat_id(REQUESTED_DEFAULT_CHAT_ID)
    destination = destination_chat_value()
    # Recover automatically if a stale environment value points moderation at the publication chat.
    if configured is not None and same_chat(configured, destination) and not same_chat(default_group, destination):
        logger.warning(
            "MODERATION_CHAT_ID matches the publication chat; using the known moderation group ID %s. "
            "Set MODERATION_CHAT_ID explicitly to override this fallback.",
            default_group,
        )
        return default_group
    return configured if configured is not None else default_group


def moderation_chat_id() -> str | int | None:
    return (
        resolved_moderation_chat_id
        if resolved_moderation_chat_id is not None
        else moderation_chat_value()
    )


def destination_config_source() -> str:
    if os.getenv("DESTINATION_CHAT_ID", "").strip():
        return "DESTINATION_CHAT_ID"
    if os.getenv("CHANNEL_ID", "").strip():
        return "CHANNEL_ID"
    saved = db.get_setting("channel_id")
    if saved:
        return "SQLite (устаревшее сохранённое значение)" if saved in (LEGACY_DEFAULT_CHAT_IDS | {REQUESTED_DEFAULT_CHAT_ID}) else "SQLite"
    return "не задан"


def same_chat(first: str | int | None, second: str | int | None) -> bool:
    return first is not None and second is not None and str(first) == str(second)


async def resolve_configured_chats(bot: Bot) -> None:
    global resolved_destination_chat_id, resolved_moderation_chat_id

    # Reset first so failed lookups cannot leave stale IDs during reconfiguration/tests.
    resolved_destination_chat_id = None
    resolved_moderation_chat_id = None
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

    # Compare Telegram's canonical IDs too: a channel username and its numeric ID can
    # refer to the same chat even when the raw environment strings look different.
    if same_chat(resolved_destination_chat_id, resolved_moderation_chat_id):
        default_group = parse_chat_id(REQUESTED_DEFAULT_CHAT_ID)
        if not same_chat(default_group, resolved_destination_chat_id):
            try:
                resolved_moderation_chat_id = (await bot.get_chat(default_group)).id
                logger.warning(
                    "Resolved moderation and publication chats were identical. "
                    "Falling back to known moderation group %s; set MODERATION_CHAT_ID explicitly.",
                    resolved_moderation_chat_id,
                )
            except TelegramAPIError as exc:
                logger.error("Could not resolve fallback moderation group %s: %s", default_group, exc)


# Custom emoji IDs from the Telegram custom-emoji pack TgAndroidIcons.
# Plain Unicode alternatives are retained for bots that cannot use custom emoji.
CUSTOM_EMOJIS = {
    "check": ("5825794181183836432", "✅"),
    "ban": ("5872829476143894491", "🚫"),
    "cross": ("5967355281057779430", "❌"),
    "warning": ("5879813604068298387", "❗"),
    "info": ("5879785854284599288", "ℹ️"),
    "profile": ("5879770735999717115", "👤"),
    "timer": ("5877613700344450910", "⏲"),
    "chat": ("5884510167986343350", "💬"),
    "wave": ("5870734657384877785", "👋"),
}
CUSTOM_EMOJI_BY_ID = {emoji_id: fallback for emoji_id, fallback in CUSTOM_EMOJIS.values()}
CUSTOM_EMOJI_PATTERN = re.compile(r'<tg-emoji emoji-id="(\d+)">(.*?)</tg-emoji>', re.DOTALL)


def ce(name: str) -> str:
    emoji_id, fallback = CUSTOM_EMOJIS[name]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def strip_custom_emoji_markup(value: str) -> str:
    return CUSTOM_EMOJI_PATTERN.sub(lambda match: match.group(2), value)


def custom_emoji_markup_to_entities(value: str) -> tuple[str, list[MessageEntity]]:
    parts: list[str] = []
    entities: list[MessageEntity] = []
    cursor = 0
    offset = 0

    def utf16_length(text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    for match in CUSTOM_EMOJI_PATTERN.finditer(value):
        prefix = value[cursor:match.start()]
        parts.append(prefix)
        offset += utf16_length(prefix)

        fallback = match.group(2)
        parts.append(fallback)
        length = utf16_length(fallback)
        entities.append(
            MessageEntity(
                type="custom_emoji",
                offset=offset,
                length=length,
                custom_emoji_id=match.group(1),
            )
        )
        offset += length
        cursor = match.end()

    parts.append(value[cursor:])
    return "".join(parts), entities


def plain_keyboard(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    if markup is None:
        return None
    rows = []
    for row in markup.inline_keyboard:
        plain_row = []
        for button in row:
            emoji = CUSTOM_EMOJI_BY_ID.get(button.icon_custom_emoji_id or "")
            if not emoji:
                plain_row.append(button)
                continue
            data = button.model_dump(exclude_none=True)
            data["text"] = f"{emoji} {button.text}"
            data.pop("icon_custom_emoji_id", None)
            plain_row.append(InlineKeyboardButton(**data))
        rows.append(plain_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def has_custom_button_icons(markup: InlineKeyboardMarkup | None) -> bool:
    return bool(
        markup
        and any(button.icon_custom_emoji_id for row in markup.inline_keyboard for button in row)
    )


async def safe_answer(message: Message, text: str, **kwargs):
    try:
        return await message.answer(text, **kwargs)
    except TelegramBadRequest:
        fallback_text = strip_custom_emoji_markup(text)
        fallback_markup = plain_keyboard(kwargs.get("reply_markup"))
        if fallback_text == text and not has_custom_button_icons(kwargs.get("reply_markup")):
            raise
        kwargs["reply_markup"] = fallback_markup
        return await message.answer(fallback_text, **kwargs)


async def safe_bot_send_message(bot: Bot, chat_id: int | str, text: str, **kwargs):
    try:
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except TelegramBadRequest:
        fallback_text = strip_custom_emoji_markup(text)
        fallback_markup = plain_keyboard(kwargs.get("reply_markup"))
        if fallback_text == text and not has_custom_button_icons(kwargs.get("reply_markup")):
            raise
        kwargs["reply_markup"] = fallback_markup
        return await bot.send_message(chat_id=chat_id, text=fallback_text, **kwargs)


async def safe_bot_edit_message_text(
    bot: Bot, chat_id: int, message_id: int, text: str, **kwargs
):
    try:
        return await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, **kwargs
        )
    except TelegramBadRequest:
        fallback_text = strip_custom_emoji_markup(text)
        fallback_markup = plain_keyboard(kwargs.get("reply_markup"))
        if fallback_text == text and not has_custom_button_icons(kwargs.get("reply_markup")):
            raise
        kwargs["reply_markup"] = fallback_markup
        return await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=fallback_text, **kwargs
        )


async def safe_edit_text(message: Message, text: str, **kwargs):
    try:
        return await message.edit_text(text, **kwargs)
    except TelegramBadRequest:
        fallback_text = strip_custom_emoji_markup(text)
        fallback_markup = plain_keyboard(kwargs.get("reply_markup"))
        if fallback_text == text and not has_custom_button_icons(kwargs.get("reply_markup")):
            raise
        kwargs["reply_markup"] = fallback_markup
        return await message.edit_text(fallback_text, **kwargs)


async def safe_edit_reply_markup(
    bot: Bot, chat_id: int, message_id: int, markup: InlineKeyboardMarkup | None
):
    try:
        return await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=markup
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        if not has_custom_button_icons(markup):
            raise
        try:
            return await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=plain_keyboard(markup)
            )
        except TelegramBadRequest as fallback_exc:
            if "message is not modified" in str(fallback_exc).lower():
                return
            raise


async def refresh_submission_keyboards(bot: Bot, sub, status: str) -> bool:
    """Refresh every known moderation message; replace stale buttons on pending cards."""
    target = moderation_chat_id()
    card_id = sub["admin_card_id"]
    if target is None or not card_id:
        return False

    submission_id = int(sub["id"])
    try:
        card_mode = str(sub["admin_card_mode"] or "content")
    except (KeyError, IndexError):
        card_mode = "content"

    content_ids = db.moderation_message_ids(submission_id)
    if not content_ids:
        content_ids = [int(card_id)]

    if card_mode == "control":
        # The fallback control card owns the buttons; copied content messages stay clean.
        targets = [int(card_id)]
        if status == "pending":
            for message_id in content_ids:
                try:
                    await safe_edit_reply_markup(bot, int(target), message_id, None)
                except TelegramAPIError as exc:
                    logger.warning(
                        "Could not clear content keyboard for submission %s / message %s: %s",
                        submission_id, message_id, exc,
                    )
        else:
            targets.extend(message_id for message_id in content_ids if message_id != int(card_id))
    else:
        targets = list(content_ids)
        if int(card_id) not in targets:
            targets.insert(0, int(card_id))

    markup = buttons(submission_id) if status == "pending" else None
    success = True
    for message_id in dict.fromkeys(targets):
        try:
            await safe_edit_reply_markup(bot, int(target), int(message_id), markup)
        except TelegramAPIError as exc:
            success = False
            logger.warning(
                "Could not refresh moderation keyboard for submission %s / message %s: %s",
                submission_id, message_id, exc,
            )
    return success


def buttons(sub_id: int, *, custom_icons: bool = True) -> InlineKeyboardMarkup:
    if custom_icons:
        rows = [
            [
                InlineKeyboardButton(
                    text="Принять",
                    callback_data=f"mod:yes:{sub_id}",
                    icon_custom_emoji_id=CUSTOM_EMOJIS["check"][0],
                ),
                InlineKeyboardButton(
                    text="Отклонить",
                    callback_data=f"mod:no:{sub_id}",
                    icon_custom_emoji_id=CUSTOM_EMOJIS["cross"][0],
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Ответить",
                    callback_data=f"mod:reply:{sub_id}",
                    icon_custom_emoji_id=CUSTOM_EMOJIS["chat"][0],
                ),
                InlineKeyboardButton(
                    text="Заблокировать",
                    callback_data=f"mod:block:{sub_id}",
                    icon_custom_emoji_id=CUSTOM_EMOJIS["ban"][0],
                ),
            ],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"mod:yes:{sub_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod:no:{sub_id}"),
            ],
            [
                InlineKeyboardButton(text="💬 Ответить", callback_data=f"mod:reply:{sub_id}"),
                InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"mod:block:{sub_id}"),
            ],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reply_cancel_buttons(sub_id: int, *, custom_icons: bool = True) -> InlineKeyboardMarkup:
    text = "Отменить ответ" if custom_icons else "🚫 Отменить ответ"
    button_args = {"text": text, "callback_data": f"mod:cancel:{sub_id}"}
    if custom_icons:
        button_args["icon_custom_emoji_id"] = CUSTOM_EMOJIS["ban"][0]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(**button_args)]])




def sent_at(utc_value: str) -> str:
    try:
        tz = ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    dt = datetime.fromisoformat(utc_value.replace(" ", "T")).replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")


STATUS_TEXT = {
    "pending": f"{ce('info')} Статус: на проверке",
    "published": f"{ce('check')} Заявка принята и опубликована",
    "rejected": f"{ce('cross')} Заявка отклонена",
    "blocked": f"{ce('ban')} Пользователь заблокирован",
    "publish_uncertain": f"{ce('warning')} Публикация не подтверждена — проверь канал вручную",
    "delivery_failed": f"{ce('warning')} Не удалось передать заявку в группу",
}
STATUS_BUTTON = {
    "published": "Принята",
    "rejected": "Отклонена",
    "blocked": "Заблокирован",
    "publish_uncertain": "Проверь публикацию",
    "delivery_failed": "Ошибка доставки",
}
STATUS_ICON = {
    "published": "check",
    "rejected": "cross",
    "blocked": "ban",
    "publish_uncertain": "warning",
    "delivery_failed": "warning",
}


def status_buttons(
    sub_id: int, status: str, *, custom_icons: bool = True
) -> InlineKeyboardMarkup:
    label = STATUS_BUTTON.get(status, "Заявка обработана")
    icon_name = STATUS_ICON.get(status, "info")
    if custom_icons:
        button = InlineKeyboardButton(
            text=label,
            callback_data=f"mod:status:{sub_id}",
            icon_custom_emoji_id=CUSTOM_EMOJIS[icon_name][0],
        )
    else:
        button = InlineKeyboardButton(
            text=f"{CUSTOM_EMOJIS[icon_name][1]} {label}",
            callback_data=f"mod:status:{sub_id}",
        )
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def moderation_footer(sub, status: str) -> str:
    username = f"@{sub['username']}" if sub["username"] else "без username"
    name = sub["full_name"] or "Не указано"
    status_text = STATUS_TEXT.get(status, f"{ce('info')} Заявка обработана")
    decision_labels = {
        "published": "Принял",
        "rejected": "Отклонил",
        "blocked": "Заблокировал",
    }
    decision_line = ""
    decision_label = decision_labels.get(status)
    reviewer_id = sub["reviewed_by_id"]
    reviewer_name = sub["reviewed_by_name"]
    if decision_label and reviewer_id and reviewer_name:
        decision_line = (
            f"\n{ce('profile')} {decision_label}: {reviewer_name} · ID {reviewer_id}"
        )
    return (
        "━━━━━━━━━━━━━━\n"
        f"{ce('profile')} Отправитель: {username} · ID {sub['user_id']}\n"
        f"{ce('profile')} Имя: {name}\n"
        f"{ce('timer')} Отправлено: {sent_at(sub['created_at'])}\n"
        f"{status_text}"
        f"{decision_line}"
    )


def stored_entities(item) -> list[MessageEntity]:
    try:
        values = json.loads(item["entities_json"] or "[]")
        return [MessageEntity(**value) for value in values]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return []


def moderation_body(sub, item, status: str) -> tuple[str, str, list[MessageEntity]] | None:
    """Return the editable body with original formatting plus custom-emoji entities."""
    content_type = str(item["content_type"] or "")
    original = str(item["content_text"] or "")
    footer_markup = moderation_footer(sub, status)
    footer, footer_entities = custom_emoji_markup_to_entities(footer_markup)
    separator = "\n\n" if original else ""
    combined = f"{original}{separator}{footer}"
    entities = stored_entities(item)

    def utf16_length(text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    footer_offset = utf16_length(original + separator)
    entities.extend(
        MessageEntity(
            type="custom_emoji",
            offset=footer_offset + entity.offset,
            length=entity.length,
            custom_emoji_id=entity.custom_emoji_id,
        )
        for entity in footer_entities
    )

    if content_type == "text":
        return ("text", combined, entities) if len(combined) <= 4096 else None
    if content_type in {"photo", "video", "animation", "document", "audio", "voice"}:
        return ("caption", combined, entities) if len(combined) <= 1024 else None
    return None


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
        await safe_bot_send_message(bot, chat_id, text)
    except TelegramBadRequest:
        try:
            await bot.send_message(chat_id, html.escape(strip_custom_emoji_markup(text)))
        except TelegramAPIError as exc:
            logger.info("Cannot notify user %s: %s", chat_id, exc)
    except TelegramAPIError as exc:
        logger.info("Cannot notify user %s: %s", chat_id, exc)


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


async def add_publication_disclaimer(
    bot: Bot,
    destination: int | str,
    copied_ids: list[int],
    items,
) -> None:
    """Append the legal disclaimer as a Telegram blockquote to the published post."""
    caption_types = {"photo", "video", "animation", "document", "audio", "voice"}
    candidates = [
        (index, item)
        for index, item in enumerate(items)
        if str(item["content_type"] or "") == "text"
        or str(item["content_type"] or "") in caption_types
    ]
    # Prefer an existing caption/text if it has room; otherwise use an empty caption
    # slot in an album before falling back to a reply under the post.
    candidates.sort(
        key=lambda pair: 0 if str(pair[1]["content_text"] or "").strip() else 1
    )

    for index, item in candidates:
        content_type = str(item["content_type"] or "")
        original = str(item["content_text"] or "")
        separator = "\n\n" if original else ""
        combined = f"{original}{separator}{PUBLICATION_DISCLAIMER}"
        limit = 4096 if content_type == "text" else 1024
        if utf16_length(combined) > limit:
            continue

        entities = stored_entities(item)
        entities.append(
            MessageEntity(
                type="blockquote",
                offset=utf16_length(original + separator),
                length=utf16_length(PUBLICATION_DISCLAIMER),
            )
        )
        try:
            if content_type == "text":
                await bot.edit_message_text(
                    chat_id=destination,
                    message_id=copied_ids[index],
                    text=combined,
                    entities=entities or None,
                    parse_mode=None,
                )
            else:
                await bot.edit_message_caption(
                    chat_id=destination,
                    message_id=copied_ids[index],
                    caption=combined,
                    caption_entities=entities or None,
                    parse_mode=None,
                )
            return
        except TelegramAPIError as exc:
            logger.warning(
                "Could not append publication disclaimer to message %s: %s",
                copied_ids[index],
                exc,
            )

    # Stickers, round videos, oversized captions, or non-editable posts still get
    # the disclaimer as a quoted reply so it is never silently omitted.
    quoted_entities = [
        MessageEntity(
            type="blockquote",
            offset=0,
            length=utf16_length(PUBLICATION_DISCLAIMER),
        )
    ]
    try:
        await bot.send_message(
            chat_id=destination,
            text=PUBLICATION_DISCLAIMER,
            entities=quoted_entities,
            parse_mode=None,
            reply_parameters=ReplyParameters(
                message_id=copied_ids[0],
                allow_sending_without_reply=True,
            ),
        )
    except TelegramAPIError as exc:
        logger.warning(
            "Could not attach disclaimer as a reply to message %s; sending it as a separate quoted post: %s",
            copied_ids[0],
            exc,
        )
        await bot.send_message(
            chat_id=destination,
            text=PUBLICATION_DISCLAIMER,
            entities=quoted_entities,
            parse_mode=None,
        )


async def copy_submission(
    bot: Bot,
    destination: int | str,
    sub_id: int,
    *,
    moderation_controls: bool = False,
) -> tuple[list[int], int | None]:
    """Copy one message or an album; make moderation controls available on every item."""
    sub = db.get(sub_id)
    items = db.items_for_submission(sub_id)
    message_ids = [int(item["message_id"]) for item in items]
    if not sub or not message_ids:
        raise ValueError("Submission missing")

    if len(message_ids) == 1:
        copied = await bot.copy_message(
            chat_id=destination,
            from_chat_id=sub["user_id"],
            message_id=message_ids[0],
        )
        copied_ids = [copied.message_id]
    else:
        copied_messages = await bot.copy_messages(
            chat_id=destination,
            from_chat_id=sub["user_id"],
            message_ids=message_ids,
        )
        if len(copied_messages) != len(message_ids):
            raise ValueError("Partial album copy; check the destination manually")
        copied_ids = [item.message_id for item in copied_messages]

    if moderation_controls:
        db.set_moderation_message_ids(sub_id, copied_ids)
        attached = True
        for message_id in copied_ids:
            try:
                await safe_edit_reply_markup(
                    bot,
                    chat_id=int(destination),
                    message_id=message_id,
                    markup=buttons(sub_id),
                )
            except TelegramAPIError as exc:
                attached = False
                logger.warning(
                    "Could not attach moderation buttons to submission %s / message %s: %s",
                    sub_id, message_id, exc,
                )

        if not attached:
            # If even one album item rejects an inline keyboard, use one reliable text control card.
            for message_id in copied_ids:
                try:
                    await safe_edit_reply_markup(
                        bot, chat_id=int(destination), message_id=message_id, markup=None
                    )
                except TelegramAPIError as exc:
                    logger.warning(
                        "Could not clear partial moderation keyboard for submission %s / message %s: %s",
                        sub_id, message_id, exc,
                    )
            preview = html.escape(str(sub["quote_text"] or "[Сообщение]")[:700])
            control_text = (
                f"{moderation_footer(sub, 'pending')}\n\n"
                f"{ce('info')} Контент заявки: {preview}"
            )
            control_card = await safe_bot_send_message(
                bot,
                chat_id=destination,
                text=control_text,
                reply_markup=buttons(sub_id),
                reply_parameters=ReplyParameters(
                    message_id=copied_ids[0],
                    allow_sending_without_reply=True,
                ),
            )
            return copied_ids, control_card.message_id
        return copied_ids, None

    await add_publication_disclaimer(bot, destination, copied_ids, items)
    return copied_ids, None

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
        copied_ids, fallback_card_id = await copy_submission(
            bot, target, sub_id, moderation_controls=True
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

    # Normally each copied item is its own moderation surface. If Telegram rejects
    # a keyboard on any item, a separate text control card is used as a reliable fallback.
    card_id = fallback_card_id or copied_ids[0]
    card_mode = "control" if fallback_card_id else "content"
    db.set_card_pending(sub_id, card_id, card_mode)
    current_sub = db.get(sub_id) or sub
    await update_moderation_card(bot, current_sub, "pending")
    await safe_notify(bot, sub["user_id"], f"{ce('check')} Предложение отправлено на проверку.")


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
                [
                    BotCommand(command="unblock", description="Разблокировать пользователя"),
                    BotCommand(command="id", description="Показать ID этого чата"),
                ],
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
        await safe_bot_edit_message_text(
            bot,
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
            f"{ce('timer')} <b>Время ответа истекло.</b>\nНажми «Ответить» на карточке заявки, чтобы начать заново.",
        )


async def deliver_group_reply(bot: Bot, message: Message, session: dict[str, object]) -> None:
    target_user_id = int(session["user_id"])
    source_ids = db.message_ids(int(session["sub_id"]))
    reply_parameters = (
        ReplyParameters(message_id=source_ids[0], allow_sending_without_reply=True)
        if source_ids else None
    )

    if message.text is not None:
        combined = f"{ce('chat')} <b>Ответ на твоё предложение</b>\n\n{html.escape(message.text)}"
        if len(combined) <= 3900:
            await safe_bot_send_message(
                bot,
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
        await safe_answer(message, f"{ce('ban')} Доступ к отправке предложений заблокирован.")
        return
    await safe_answer(
        message,
        f"{ce('wave')} <b>Предложка</b>\n\n"
        "Отправь сюда текст, фото, видео, кружок, голосовое, GIF, стикер или файл. "
        "Предложение появится в группе на проверке."
    )


@router.message(Command("id"), F.chat.type.in_({"group", "supergroup"}))
async def chat_id_command(message: Message) -> None:
    moderation_value = moderation_chat_id()
    destination_value = destination_chat_id()
    await message.answer(
        f"ID этого чата: <code>{message.chat.id}</code>\n"
        f"MODERATION_CHAT_ID в окружении: <code>{html.escape(MODERATION_ENV or '(пусто)')}</code>\n"
        f"Эффективный ID модерации: <code>{html.escape(str(moderation_value) if moderation_value is not None else 'НЕ ЗАДАН')}</code>\n"
        f"DESTINATION_CHAT_ID / CHANNEL_ID: <code>{html.escape(DESTINATION_ENV or '(пусто)')}</code>\n"
        f"Эффективный ID публикации: <code>{html.escape(str(destination_value) if destination_value is not None else 'НЕ ЗАДАН')}</code>\n"
        f"Источник публикации: <code>{html.escape(destination_config_source())}</code>"
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
        await safe_answer(message, f"{ce('check')} Пользователь <code>{target_user_id}</code> разблокирован.")
    else:
        await message.answer(f"Пользователь <code>{target_user_id}</code> не был заблокирован.")


@router.message(F.chat.type == "private")
async def inbox(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    sender_id = message.from_user.id

    if db.is_blocked(sender_id):
        await safe_answer(message, f"{ce('ban')} Доступ к отправке предложений заблокирован.")
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
            content_type=message.content_type,
            content_text=message.text if message.text is not None else (message.caption or ""),
            entities_json=json.dumps(
                [
                    entity.model_dump(exclude_none=True)
                    for entity in (
                        message.entities if message.text is not None else message.caption_entities
                    ) or []
                ],
                ensure_ascii=False,
            ),
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
    # Handle only replies to the active answer prompt or its submission card.
    if message.chat.id != moderation_chat_id() or not message.from_user:
        return
    session = reply_sessions.get(message.from_user.id)
    if not session:
        return
    prompt_id = int(session["prompt_message_id"])
    replied_to = message.reply_to_message
    # Accept a reply either to the bot's prompt or directly to the submission card.
    active_sub = db.get(int(session["sub_id"]))
    allowed_reply_ids = {prompt_id}
    if active_sub and active_sub["admin_card_id"]:
        allowed_reply_ids.add(int(active_sub["admin_card_id"]))
    if not replied_to or replied_to.message_id not in allowed_reply_ids:
        return

    if asyncio.get_running_loop().time() >= float(session["deadline"]):
        old_session = clear_reply_session(message.from_user.id)
        if old_session:
            await edit_reply_prompt(
                bot,
                message.chat.id,
                int(old_session["prompt_message_id"]),
                f"{ce('timer')} <b>Время ответа истекло.</b>",
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
        await safe_answer(
            message,
            f"{ce('warning')} Не удалось доставить ответ. Возможно, пользователь заблокировал бота. "
            "Режим ответа остаётся активным — попробуй ещё раз."
        )
        return

    old_session = clear_reply_session(message.from_user.id)
    if old_session:
        await edit_reply_prompt(
            bot,
            message.chat.id,
            int(old_session["prompt_message_id"]),
            f"{ce('check')} <b>Ответ отправлен пользователю.</b>",
        )
    await safe_answer(message, f"{ce('check')} Ответ отправлен пользователю.")


async def update_moderation_card(bot: Bot, sub, status: str) -> None:
    """Update status and ensure all known moderation keyboards match the submission state."""
    target = moderation_chat_id()
    card_id = sub["admin_card_id"]
    if target is None or not card_id:
        return

    submission_id = int(sub["id"])
    try:
        card_mode = str(sub["admin_card_mode"] or "content")
    except (KeyError, IndexError):
        card_mode = "content"

    markup = buttons(submission_id) if status == "pending" else None
    updated = False

    if card_mode == "control":
        preview = html.escape(str(sub["quote_text"] or "[Сообщение]")[:700])
        text = (
            f"{moderation_footer(sub, status)}\n\n"
            f"{ce('info')} Контент заявки: {preview}"
        )
        try:
            await safe_bot_edit_message_text(
                bot,
                chat_id=int(target),
                message_id=int(card_id),
                text=text,
                reply_markup=markup,
            )
            updated = True
        except TelegramAPIError as exc:
            logger.warning(
                "Could not update fallback control card for submission %s: %s",
                submission_id, exc,
            )
    else:
        items = db.items_for_submission(submission_id)
        first_item = items[0] if items else None
        body = moderation_body(sub, first_item, status) if first_item else None
        if body:
            kind, combined, entities = body
            try:
                if kind == "text":
                    await bot.edit_message_text(
                        chat_id=int(target),
                        message_id=int(card_id),
                        text=combined,
                        entities=entities or None,
                        parse_mode=None,
                        reply_markup=None,
                    )
                else:
                    await bot.edit_message_caption(
                        chat_id=int(target),
                        message_id=int(card_id),
                        caption=combined,
                        caption_entities=entities or None,
                        parse_mode=None,
                        reply_markup=None,
                    )
                updated = True
            except TelegramBadRequest as exc:
                logger.info(
                    "Could not edit moderation content for submission %s; retrying without custom emoji: %s",
                    submission_id, exc,
                )
                fallback_entities = [
                    entity for entity in (entities or []) if entity.type != "custom_emoji"
                ]
                try:
                    if kind == "text":
                        await bot.edit_message_text(
                            chat_id=int(target),
                            message_id=int(card_id),
                            text=strip_custom_emoji_markup(combined),
                            entities=fallback_entities or None,
                            parse_mode=None,
                            reply_markup=None,
                        )
                    else:
                        await bot.edit_message_caption(
                            chat_id=int(target),
                            message_id=int(card_id),
                            caption=strip_custom_emoji_markup(combined),
                            caption_entities=fallback_entities or None,
                            parse_mode=None,
                            reply_markup=None,
                        )
                    updated = True
                except TelegramAPIError as fallback_exc:
                    logger.warning(
                        "Could not edit moderation content for submission %s even with fallbacks: %s",
                        submission_id, fallback_exc,
                    )
            except TelegramAPIError as exc:
                logger.warning(
                    "Could not edit moderation content for submission %s: %s",
                    submission_id, exc,
                )

    keyboards_ok = await refresh_submission_keyboards(bot, sub, status)
    if status != "pending" and not updated:
        decision_labels = {
            "published": "Принял",
            "rejected": "Отклонил",
            "blocked": "Заблокировал",
        }
        decision_label = decision_labels.get(status)
        reviewer_id = sub["reviewed_by_id"]
        reviewer_name = sub["reviewed_by_name"]
        decision_line = (
            f"\n{ce('profile')} {decision_label}: {reviewer_name} · ID {reviewer_id}"
            if decision_label and reviewer_id and reviewer_name
            else ""
        )
        status_text = STATUS_TEXT.get(status, f"{ce('info')} Заявка обработана")
        try:
            await safe_bot_send_message(
                bot,
                chat_id=int(target),
                text=f"{status_text}{decision_line}",
                reply_parameters=ReplyParameters(
                    message_id=int(card_id),
                    allow_sending_without_reply=True,
                ),
            )
        except TelegramAPIError as exc:
            logger.warning(
                "Could not post decision details for submission %s: %s",
                submission_id, exc,
            )

    if status != "pending" and keyboards_ok:
        db.mark_keyboard_cleaned(submission_id)


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
            await safe_edit_text(message, f"{ce('ban')} <b>Ответ отменён.</b>", reply_markup=None)
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
    if action == "status":
        labels = {
            "published": "Заявка принята и опубликована.",
            "rejected": "Заявка отклонена.",
            "blocked": "Пользователь заблокирован.",
            "publish_uncertain": "Публикация не подтверждена — проверь канал вручную.",
            "delivery_failed": "Не удалось доставить заявку.",
        }
        await callback.answer(labels.get(sub["status"], "Заявка обработана."), show_alert=True)
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
                    f"{ce('timer')} <b>Время ответа истекло.</b>",
                )

        prompt = await safe_bot_send_message(
            bot,
            chat_id=message.chat.id,
            text=(
                f"{ce('chat')} <b>Ответь на это сообщение или прямо на карточку заявки, чтобы отправить ответ пользователю.</b>\n"
                f"{ce('timer')} У тебя есть <b>15 минут</b>. Ответ придёт пользователю в личку с цитатой его предложения.\n\n"
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
        await callback.answer("Ответ открыт. Ответь на подсказку бота или карточку заявки в группе.")
        return

    if action == "yes":
        target = destination_chat_id()
        mod_target = moderation_chat_id()
        if not target:
            await callback.answer("Не настроен чат для публикации.", show_alert=True)
            return
        if same_chat(target, mod_target):
            await callback.answer(
                "Группа модерации и канал публикации имеют один ID. "
                "Укажи отдельный DESTINATION_CHAT_ID для канала в BotHost.",
                show_alert=True,
            )
            return
        if not db.move(sub_id, "pending", "publishing"):
            await callback.answer("Заявка уже обрабатывается.")
            return

        try:
            # Publish the original submission and append the quoted disclaimer.
            copied_ids, _ = await copy_submission(bot, target, sub_id)
        except (TelegramAPIError, ValueError) as exc:
            db.move(sub_id, "publishing", "publish_uncertain")
            logger.exception("Publication status uncertain for %s: %s", sub_id, exc)
            sub = db.get(sub_id)
            if sub:
                await update_moderation_card(bot, sub, "publish_uncertain")
            await callback.answer("Ошибка публикации. Проверь канал вручную.", show_alert=True)
            return

        db.move(
            sub_id,
            "publishing",
            "published",
            reviewed_by_id=callback.from_user.id,
            reviewed_by_name=callback.from_user.full_name,
        )
        new_status = "published"
        notice = f"{ce('check')} Твоё предложение принято и опубликовано!"
        toast = "Принято"
    elif action == "no":
        if not db.move(
            sub_id,
            "pending",
            "rejected",
            reviewed_by_id=callback.from_user.id,
            reviewed_by_name=callback.from_user.full_name,
        ):
            await callback.answer("Заявка уже обрабатывается.")
            return
        new_status = "rejected"
        notice = f"{ce('cross')} Твоё предложение не принято."
        toast = "Отклонено"
    elif action == "block":
        if not db.move(
            sub_id,
            "pending",
            "blocked",
            reviewed_by_id=callback.from_user.id,
            reviewed_by_name=callback.from_user.full_name,
        ):
            await callback.answer("Заявка уже обрабатывается.")
            return
        db.block_user(sub["user_id"], callback.from_user.id)
        new_status = "blocked"
        notice = f"{ce('ban')} Доступ к отправке предложений заблокирован."
        toast = "Пользователь заблокирован"
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    current_sub = db.get(sub_id) or sub
    await update_moderation_card(bot, current_sub, new_status)
    await callback.answer(toast)
    await safe_notify(bot, sub["user_id"], notice)


async def cleanup_processed_inline_keyboards(bot: Bot) -> None:
    """Replace stale keyboards on pending cards and remove them from processed submissions."""
    target = moderation_chat_id()
    if target is None:
        return

    cards = db.cards_needing_keyboard_cleanup()
    if not cards:
        logger.info("Inline keyboard cleanup: no cards need checking.")
        return

    checked = 0
    failures = 0
    for card in cards:
        submission_id = int(card["id"])
        sub = db.get(submission_id)
        if not sub:
            continue
        status = str(sub["status"])
        success = await refresh_submission_keyboards(bot, sub, status)
        if success and status != "pending":
            db.mark_keyboard_cleaned(submission_id)
        if success:
            checked += 1
        else:
            failures += 1
        await asyncio.sleep(0.04)

    logger.info(
        "Inline keyboard refresh finished: %s cards checked, %s with API failures.",
        checked, failures,
    )


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
        if destination_chat_id() is None:
            logger.error(
                "CONFIG ERROR: publication chat is not configured. Set DESTINATION_CHAT_ID "
                "(or CHANNEL_ID) to the separate channel ID or @username."
            )
        if same_chat(moderation_chat_id(), destination_chat_id()):
            logger.error(
                "CONFIG ERROR: moderation group and publication chat resolve to the same ID (%s). "
                "Runtime values: MODERATION_CHAT_ID=%r, DESTINATION_CHAT_ID/CHANNEL_ID=%r, "
                "publication source=%s.",
                moderation_chat_id(),
                MODERATION_ENV or "(empty; known group fallback)",
                DESTINATION_ENV or "(empty)",
                destination_config_source(),
            )
        logger.info(
            "Started as @%s. Moderation group: %s (env=%r). Publication chat: %s (source=%s).",
            me.username,
            moderation_chat_id(),
            MODERATION_ENV or f"fallback:{REQUESTED_DEFAULT_CHAT_ID}",
            destination_chat_id(),
            destination_config_source(),
        )
        await cleanup_processed_inline_keyboards(bot)
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
