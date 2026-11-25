"""Telegram suggestion bot with group-based moderation. Python 3.11+, aiogram 3."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
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
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    MenuButtonCommands,
    Message,
    MessageEntity,
    ReplyParameters,
    ReplyKeyboardRemove,
)
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs) -> bool:
        return False

from storage import Storage, resolve_database_path
from payments import (
    RollyPayError,
    create_payment,
    get_balance,
    get_payment,
    get_rate,
    list_payments as list_rollypay_payments,
)
from post_lookup import InvalidPostLink, parse_post_link, publication_text_fingerprint

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = resolve_database_path(
    ROOT,
    configured_path=os.getenv("DB_PATH", ""),
    data_dir=os.getenv("DATA_DIR", "/app/data" if ROOT == Path("/app") else ""),
)
ROLLYPAY_API_BASE = os.getenv("ROLLYPAY_API_BASE", "https://rollypay.io").rstrip("/")
ROLLYPAY_API_KEY = os.getenv("ROLLYPAY_API_KEY", "").strip()
ROLLYPAY_TERMINAL_ID = os.getenv("ROLLYPAY_TERMINAL_ID", "").strip()
ROLLYPAY_TEST_MODE = os.getenv("ROLLYPAY_TEST_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
PUBLIC_SITE_BASE_URL = os.getenv("PUBLIC_SITE_BASE_URL", "https://mgnvpn.ru").rstrip("/")
PUBLIC_BOT_USERNAME = os.getenv("PUBLIC_BOT_USERNAME", "PozorMgnBot").strip().lstrip("@")
DEFAULT_REMOVAL_PRICE_RUB = 100
DEFAULT_AUTHOR_INFO_PRICE_RUB = 100
OWNER_USER_IDS = {8464597898}

POST_ACTION_BUTTONS = {"Удалить пост": "post_removal", "Узнать, кто выложил": "author_lookup"}
POST_ACTION_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Удалить пост")],
        [KeyboardButton(text="Узнать, кто выложил")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

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

PUBLICATION_LINKS = (
    ("Удалить пост", "https://t.me/PozorMgnBot?start=remove_unavailable"),
    ("Предложить пост", "https://t.me/PozorMgnBot"),
)
CHANNEL_PROMOS = {
    "promo_wait": "жду посты @PozorMgnBot жду посты @PozorMgnBot",
    "promo_subscribe": (
        "ПОДПИШИТЕСЬ НА НАШ ПЕРЕХОДНИК в случае сноса тгк туда опубликуем новую "
        "https://t.me/perehodnikpozora"
    ),
}

logger = logging.getLogger("college_suggest")
router = Router()
db = Storage(DB_PATH)
album_jobs: dict[tuple[int, str], asyncio.Task] = {}
reply_sessions: dict[int, dict[str, object]] = {}
reply_jobs: dict[int, asyncio.Task] = {}
broadcast_sessions: dict[int, dict[str, object]] = {}
admin_sessions: dict[int, tuple[object, ...]] = {}
USERS_PAGE_SIZE = 10
PAYMENTS_PAGE_SIZE = 8
resolved_destination_chat_id: int | None = None
resolved_moderation_chat_id: int | None = None


def positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


DB_BACKUP_INTERVAL_SECONDS = positive_int_env("DB_BACKUP_INTERVAL_SECONDS", 6 * 60 * 60)
DB_BACKUP_KEEP = positive_int_env("DB_BACKUP_KEEP", 30)


async def database_backup_loop() -> None:
    """Keep rotating SQLite snapshots on the persistent data volume."""
    while True:
        await asyncio.sleep(DB_BACKUP_INTERVAL_SECONDS)
        try:
            backup = db.backup(keep=DB_BACKUP_KEEP)
            if backup:
                logger.info("Database backup created: %s", backup)
        except (OSError, sqlite3.DatabaseError) as exc:
            logger.error("Database backup failed: %s", exc)


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


async def moderation_group_membership(bot: Bot, user_id: int) -> bool | None:
    """Return True/False for group membership, or None if Telegram cannot verify it."""
    group_id = moderation_chat_id()
    if group_id is None:
        logger.warning("Cannot verify moderation-group membership: group ID is not configured")
        return None
    try:
        member = await bot.get_chat_member(chat_id=group_id, user_id=user_id)
    except TelegramAPIError as exc:
        logger.warning(
            "Could not verify moderation-group membership for user %s: %s",
            user_id, exc,
        )
        return None

    status = getattr(member.status, "value", member.status)
    if status in {"creator", "administrator", "member"}:
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def is_blocked_from_suggestions(bot: Bot, user_id: int) -> bool:
    """Never keep a confirmed moderation-group member blocked from the suggestion inbox."""
    if not db.is_blocked(user_id):
        return False
    membership = await moderation_group_membership(bot, user_id)
    if membership is True:
        db.unblock_user(user_id)
        logger.warning(
            "Automatically unblocked moderation-group member %s from the suggestion inbox",
            user_id,
        )
        return False
    return True


async def is_moderation_admin(bot: Bot, user_id: int) -> bool:
    """Only Telegram administrators of the configured moderation group can use admin tools."""
    if user_id in OWNER_USER_IDS:
        return True
    group_id = moderation_chat_id()
    if group_id is None:
        return False
    try:
        member = await bot.get_chat_member(chat_id=group_id, user_id=user_id)
    except TelegramAPIError as exc:
        logger.warning("Could not verify moderator permissions for user %s: %s", user_id, exc)
        return False
    status = getattr(member.status, "value", member.status)
    return status in {"creator", "administrator"}


def publication_window_starts() -> dict[str, str]:
    """Return local-day, rolling-seven-day and local-month starts as UTC SQLite timestamps."""
    try:
        tz = ZoneInfo(TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown TIMEZONE %r; publication statistics will use UTC", TIMEZONE)
        tz = timezone.utc
    now_local = datetime.now(tz)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now_local - timedelta(days=7)
    month_start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def utc_sql(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "today_start": utc_sql(today_start),
        "week_start": utc_sql(week_start),
        "month_start": utc_sql(month_start),
        "now": utc_sql(now_local),
    }


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


def plain_keyboard(
    markup: InlineKeyboardMarkup | ReplyKeyboardRemove | None,
) -> InlineKeyboardMarkup | ReplyKeyboardRemove | None:
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
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


def has_custom_button_icons(
    markup: InlineKeyboardMarkup | ReplyKeyboardRemove | None,
) -> bool:
    return bool(
        isinstance(markup, InlineKeyboardMarkup)
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
    bot: Bot, chat_id: int | str, message_id: int, markup: InlineKeyboardMarkup | None
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

    success = True
    if card_mode in {"control", "album_control"}:
        # Keep buttons on all content items; the control card guarantees an accessible action surface.
        targets = [int(card_id)]
        targets.extend(content_ids)
    else:
        targets = list(content_ids)
        if int(card_id) not in targets:
            targets.insert(0, int(card_id))

    # A processed suggestion can still receive a private moderator reply.
    markup = buttons(submission_id) if status == "pending" else status_buttons(submission_id, status)
    for message_id in dict.fromkeys(targets):
        try:
            await safe_edit_reply_markup(bot, target, int(message_id), markup)
        except TelegramAPIError as exc:
            success = False
            logger.warning(
                "Could not refresh moderation keyboard for submission %s / message %s: %s",
                submission_id, message_id, exc,
            )
    return success


def channel_promo_rows(sub_id: int) -> list[list[InlineKeyboardButton]]:
    """Announcements are confirmed before publication to the channel."""
    return [
        [InlineKeyboardButton(text="Жду посты", callback_data=f"mod:promo_wait:{sub_id}")],
        [InlineKeyboardButton(text="Подпишитесь на нас", callback_data=f"mod:promo_subscribe:{sub_id}")],
    ]


def buttons(sub_id: int, *, custom_icons: bool = True) -> InlineKeyboardMarkup:
    if custom_icons:
        rows = [
            [
                InlineKeyboardButton(
                    text="Отправить",
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
                InlineKeyboardButton(text="✅ Отправить", callback_data=f"mod:yes:{sub_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod:no:{sub_id}"),
            ],
            [
                InlineKeyboardButton(text="💬 Ответить", callback_data=f"mod:reply:{sub_id}"),
                InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"mod:block:{sub_id}"),
            ],
        ]
    rows.extend(channel_promo_rows(sub_id))
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
    "pending": "",
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
        reply_button = InlineKeyboardButton(
            text="Ответить",
            callback_data=f"mod:reply:{sub_id}",
            icon_custom_emoji_id=CUSTOM_EMOJIS["chat"][0],
        )
    else:
        button = InlineKeyboardButton(
            text=f"{CUSTOM_EMOJIS[icon_name][1]} {label}",
            callback_data=f"mod:status:{sub_id}",
        )
        reply_button = InlineKeyboardButton(
            text="💬 Ответить",
            callback_data=f"mod:reply:{sub_id}",
        )
    return InlineKeyboardMarkup(inline_keyboard=[[button], [reply_button], *channel_promo_rows(sub_id)])


def moderation_footer(sub, status: str, *, html_safe: bool = False) -> str:
    username = f"@{sub['username']}" if sub["username"] else "без username"
    name = sub["full_name"] or "Не указано"
    if html_safe:
        username = html.escape(username)
        name = html.escape(name)
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
        if html_safe:
            reviewer_name = html.escape(reviewer_name)
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
        return ("text", combined, entities) if utf16_length(combined) <= 4096 else None
    if content_type in {"photo", "video", "animation", "document", "audio", "voice"}:
        return ("caption", combined, entities) if utf16_length(combined) <= 1024 else None
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


def content_identity(message: Message) -> str:
    """Stable identity for a post item, independent of Telegram message IDs."""
    def normalized(value: str) -> str:
        return " ".join(value.split()).casefold()

    if message.text is not None:
        return json.dumps(
            {"type": "text", "text": normalized(message.text)},
            ensure_ascii=False, sort_keys=True,
        )

    kind = str(message.content_type)
    media = getattr(message, kind, None)
    if isinstance(media, (list, tuple)):
        media = media[-1] if media else None
    unique_id = getattr(media, "file_unique_id", None)
    if unique_id:
        identity = str(unique_id)
    elif media is not None and hasattr(media, "model_dump"):
        # Stable fields for contacts, polls, locations and other non-file media.
        identity = media.model_dump(
            exclude_none=True,
            exclude={"id", "file_id", "file_size", "thumbnail", "thumb"},
        )
    else:
        identity = str(media) if media is not None else ""
    return json.dumps(
        {"type": kind, "media": identity, "caption": normalized(message.caption or "")},
        ensure_ascii=False, sort_keys=True, default=str,
    )


async def safe_notify(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await safe_bot_send_message(
            bot,
            chat_id,
            text,
            reply_markup=POST_ACTION_KEYBOARD,
        )
    except TelegramBadRequest:
        try:
            await bot.send_message(
                chat_id,
                html.escape(strip_custom_emoji_markup(text)),
                reply_markup=POST_ACTION_KEYBOARD,
            )
        except TelegramAPIError as exc:
            logger.info("Cannot notify user %s: %s", chat_id, exc)
    except TelegramAPIError as exc:
        logger.info("Cannot notify user %s: %s", chat_id, exc)


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def publication_content(
    original: str,
    original_entities: list[MessageEntity],
    removal_url: str | None = None,
) -> tuple[str, list[MessageEntity]]:
    """Preserve source formatting; append quoted disclaimer and two clickable text links."""
    separator = "\n\n" if original else ""
    content = original + separator + PUBLICATION_DISCLAIMER
    entities = list(original_entities)
    entities.append(
        MessageEntity(
            type="blockquote",
            offset=utf16_length(original + separator),
            length=utf16_length(PUBLICATION_DISCLAIMER),
        )
    )
    content += "\n\n"
    links = ((PUBLICATION_LINKS[0][0], removal_url or PUBLICATION_LINKS[0][1]), PUBLICATION_LINKS[1])
    for index, (label, url) in enumerate(links):
        if index:
            content += "\n"
        offset = utf16_length(content)
        content += label
        entities.append(
            MessageEntity(
                type="text_link",
                offset=offset,
                length=utf16_length(label),
                url=url,
            )
        )
    return content, entities


async def add_publication_disclaimer(
    bot: Bot,
    destination: int | str,
    copied_ids: list[int],
    items,
    removal_url: str,
) -> int | None:
    """Append attribution links beneath each publication, with reply fallback for unsupported types."""
    caption_types = {"photo", "video", "animation", "document", "audio", "voice"}
    candidates = [
        (index, item)
        for index, item in enumerate(items)
        if str(item["content_type"] or "") == "text"
        or str(item["content_type"] or "") in caption_types
    ]
    candidates.sort(
        key=lambda pair: 0 if str(pair[1]["content_text"] or "").strip() else 1
    )

    for index, item in candidates:
        content_type = str(item["content_type"] or "")
        combined, entities = publication_content(
            str(item["content_text"] or ""), stored_entities(item), removal_url
        )
        limit = 4096 if content_type == "text" else 1024
        if utf16_length(combined) > limit:
            continue

        try:
            if content_type == "text":
                await bot.edit_message_text(
                    chat_id=destination,
                    message_id=copied_ids[index],
                    text=combined,
                    entities=entities,
                    parse_mode=None,
                )
            else:
                await bot.edit_message_caption(
                    chat_id=destination,
                    message_id=copied_ids[index],
                    caption=combined,
                    caption_entities=entities,
                    parse_mode=None,
                )
            return None
        except TelegramAPIError as exc:
            logger.warning(
                "Could not append publication footer to message %s: %s",
                copied_ids[index],
                exc,
            )

    # Sticker, video note, oversized captions and non-editable content use a reply.
    # No original submission content is discarded to make room for the footer.
    footer, entities = publication_content("", [], removal_url)
    try:
        sent = await bot.send_message(
            chat_id=destination,
            text=footer,
            entities=entities,
            parse_mode=None,
            reply_parameters=ReplyParameters(
                message_id=copied_ids[0],
                allow_sending_without_reply=True,
            ),
        )
        return sent.message_id
    except TelegramAPIError as exc:
        logger.warning(
            "Could not attach footer as a reply to post %s; publishing separately: %s",
            copied_ids[0],
            exc,
        )
        sent = await bot.send_message(
            chat_id=destination,
            text=footer,
            entities=entities,
            parse_mode=None,
        )
        return sent.message_id

async def copy_submission(
    bot: Bot,
    destination: int | str,
    sub_id: int,
    *,
    moderation_controls: bool = False,
    removal_url: str | None = None,
) -> tuple[list[int], int | None]:
    """Copy an album or post, ensuring every public post gets its attribution footer."""
    # Validate before copying to avoid leaving an untracked, unformatted post.
    if not moderation_controls and not removal_url:
        raise ValueError("A published post requires its personal removal URL")
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
        failed_message_ids: list[int] = []
        for message_id in copied_ids:
            try:
                await safe_edit_reply_markup(
                    bot,
                    chat_id=destination,
                    message_id=message_id,
                    markup=buttons(sub_id),
                )
            except TelegramAPIError as exc:
                attached = False
                failed_message_ids.append(message_id)
                logger.warning(
                    "Could not attach moderation buttons to submission %s / message %s: %s",
                    sub_id, message_id, exc,
                )

        # If Telegram's text/caption limit leaves no room for sender details,
        # preserve the original content and put those details on a reply card.
        sender_details_need_card = (
            len(items) == 1 and moderation_body(sub, items[0], "pending") is None
        )
        if not attached or len(message_ids) > 1 or sender_details_need_card:
            # Keep any buttons that worked. Albums also get a dedicated control card because
            # Telegram clients do not render inline keyboards consistently across media groups.
            preview = html.escape(str(sub["quote_text"] or "[Сообщение]")[:700])
            control_text = (
                f"{moderation_footer(sub, 'pending', html_safe=True)}\n\n"
                f"{ce('info')} Контент заявки: {preview}"
            )
            control_card = await safe_bot_send_message(
                bot,
                chat_id=destination,
                text=control_text,
                reply_markup=buttons(sub_id),
                reply_parameters=ReplyParameters(
                    message_id=(failed_message_ids[0] if failed_message_ids else copied_ids[0]),
                    allow_sending_without_reply=True,
                ),
            )
            return copied_ids, control_card.message_id
        return copied_ids, None

    footer_id = await add_publication_disclaimer(
        bot, destination, copied_ids, items, removal_url
    )
    return copied_ids, footer_id

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

    # Check once per complete post (including an album), before moderators see it.
    # Three identical posts are allowed in five minutes; the fourth starts a
    # persistent five-minute cooldown for the sender.
    spam_delay = db.submission_spam_delay(sub_id)
    if spam_delay > 0:
        db.move(sub_id, "notifying", "spam_rejected")
        await safe_notify(
            bot, int(sub["user_id"]),
            "Без спама, подождите 5 минут и снова отправляйте пост.",
        )
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

    # Single messages use their own buttons. Albums and keyboard failures get a separate
    # control card so moderation actions remain accessible in every Telegram client.
    card_id = fallback_card_id or copied_ids[0]
    card_mode = (
        "album_control" if len(db.items_for_submission(sub_id)) > 1
        else "control" if fallback_card_id
        else "content"
    )
    db.set_card_pending(sub_id, card_id, card_mode)
    current_sub = db.get(sub_id) or sub
    await update_moderation_card(bot, current_sub, "pending")
    await safe_notify(bot, sub["user_id"], f"{ce('check')} Предложение отправлено.")


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

    start_command = [
        BotCommand(command="start", description="Отправить предложение"),
        BotCommand(command="deletepost", description="Удалить пост по ссылке или пересылке"),
        BotCommand(command="who", description="Узнать автора по ссылке"),
        BotCommand(command="cancel", description="Отменить выбор действия"),
        BotCommand(command="help", description="Помощь"),
    ]
    await bot.set_my_commands(start_command, scope=BotCommandScopeDefault())
    await bot.set_my_commands(start_command, scope=BotCommandScopeAllPrivateChats())

    if isinstance(moderation_id, int):
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="admin", description="Админ-меню"),
                ],
                scope=BotCommandScopeChat(chat_id=moderation_id),
            )
        except TelegramAPIError as exc:
            logger.warning("Could not set moderation-group commands: %s", exc)

    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramAPIError as exc:
        logger.warning("Could not reset bot menu button: %s", exc)


def users_page_keyboard(offset: int, total: int) -> InlineKeyboardMarkup | None:
    buttons_row = []
    if offset > 0:
        buttons_row.append(
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"users:page:{max(0, offset - USERS_PAGE_SIZE)}")
        )
    if offset + USERS_PAGE_SIZE < total:
        buttons_row.append(
            InlineKeyboardButton(text="Далее ➡️", callback_data=f"users:page:{offset + USERS_PAGE_SIZE}")
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons_row]) if buttons_row else None


def render_users_page(offset: int) -> str:
    total = db.user_count()
    offset = max(0, min(offset, max(0, total - 1)))
    users = db.list_users(offset, USERS_PAGE_SIZE)
    pages = max(1, (total + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
    page = offset // USERS_PAGE_SIZE + 1
    lines = []
    for index, user in enumerate(users, start=offset + 1):
        full_name = html.escape(str(user["full_name"] or "Пользователь"))
        username = (
            f"@{html.escape(str(user['username']))}"
            if user["username"]
            else "<i>без username</i>"
        )
        lines.append(
            f"{index}. {ce('profile')} <b>{full_name}</b>\n"
            f"   {username} · ID <code>{int(user['user_id'])}</code>"
        )
    if lines:
        body = "\n\n".join(lines)
    else:
        known_submissions = db.known_submission_user_count()
        body = (
            "В текущей базе нет сохранённых пользователей. "
            f"Пользователей из старых заявок найдено: <b>{known_submissions}</b>.\n\n"
            "Telegram не позволяет боту автоматически выгрузить всех, кто когда-либо нажимал /start. "
            "Если людей в боте тысячи, проверь DB_PATH в BotHost: возможно, бот открыл новую пустую SQLite-базу "
            "вместо прежней."
        )
    return (
        f"{ce('info')} <b>Пользователи бота</b>\n"
        f"Всего: <b>{total}</b> · Страница <b>{page}/{pages}</b>\n\n{body}"
    )


def broadcast_confirmation_keyboard(*, channel: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Опубликовать в канал" if channel else "Отправить всем",
                    callback_data="broadcast:confirm",
                    icon_custom_emoji_id=CUSTOM_EMOJIS["check"][0],
                ),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data="broadcast:cancel",
                    icon_custom_emoji_id=CUSTOM_EMOJIS["cross"][0],
                ),
            ]
        ]
    )


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
    """Reply in private to the original author, falling back from stale reply IDs."""
    target_user_id = int(session["user_id"])
    source_ids = db.message_ids(int(session["sub_id"]))
    reply_parameters = (
        ReplyParameters(message_id=source_ids[0], allow_sending_without_reply=True)
        if source_ids else None
    )

    async def send_with_optional_reply(send, **kwargs):
        if reply_parameters is None:
            return await send(**kwargs)
        try:
            return await send(reply_parameters=reply_parameters, **kwargs)
        except TelegramBadRequest as exc:
            # Some old user messages are no longer valid Telegram reply targets.
            # Do not lose the moderator's response because of the quote.
            logger.info(
                "Reply reference for submission %s is unavailable; trying without it: %s",
                session["sub_id"], exc,
            )
            return await send(**kwargs)

    if message.text is not None:
        combined = f"{ce('chat')} <b>Ответ на твоё предложение</b>\n\n{html.escape(message.text)}"
        if len(combined) <= 3900:
            await send_with_optional_reply(
                lambda **kwargs: safe_bot_send_message(bot, target_user_id, combined, **kwargs)
            )
            return

    await send_with_optional_reply(
        lambda **kwargs: bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            **kwargs,
        )
    )

def removal_price_rub() -> int:
    raw = db.get_setting("post_removal_price_rub") or os.getenv(
        "POST_REMOVAL_PRICE_RUB", str(DEFAULT_REMOVAL_PRICE_RUB)
    )
    try:
        return max(1, min(100000, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_REMOVAL_PRICE_RUB


def removal_deep_link(submission_id: int, token: str) -> str:
    return f"https://t.me/{PUBLIC_BOT_USERNAME}?start=remove_{submission_id}_{token}"


def published_message_ids(sub) -> list[int]:
    try:
        values = json.loads(str(sub["published_message_ids"] or "[]"))
        return [int(value) for value in values]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def author_info_price_rub() -> int:
    raw = db.get_setting("post_author_info_price_rub") or os.getenv(
        "POST_AUTHOR_INFO_PRICE_RUB", str(DEFAULT_AUTHOR_INFO_PRICE_RUB)
    )
    try:
        return max(1, min(100000, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_AUTHOR_INFO_PRICE_RUB


def post_action_title(action: str, submission_id: int) -> str:
    return f"{'Удаление поста' if action == 'post_removal' else 'Инфа'} #{submission_id}"


def forwarded_channel_post(message: Message) -> tuple[int, int] | None:
    """Only trust Telegram's channel origin, not copied text or sender names."""
    origin = getattr(message, "forward_origin", None)
    if origin is None or getattr(origin, "type", None) != "channel":
        return None
    chat = getattr(origin, "chat", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(origin, "message_id", None)
    if not isinstance(chat_id, int) or not isinstance(message_id, int) or message_id <= 0:
        return None
    return chat_id, message_id


async def start_post_action_payment(message: Message, sub, action: str, *, buyer=None, from_forward: bool = False) -> bool:
    """Create a fresh invoice for a verified tracked post."""
    buyer = buyer or message.from_user
    if not buyer or buyer.is_bot:
        return False
    if not sub:
        await message.answer(
            "Не нашёл эту публикацию в базе. Ссылки из других каналов не принимаются."
        )
        return False
    if sub["removed_at"]:
        await message.answer("Этот пост уже удалён.")
        return False
    message_ids = published_message_ids(sub)
    if not sub["published_chat_id"] or not message_ids:
        await message.answer(
            "Этот пост пока не готов к оплате: нет данных публикации. Обратись к администратору."
        )
        return False
    if action not in {"post_removal", "author_lookup"}:
        await message.answer("Неизвестное действие. Выбери кнопку ещё раз.")
        return False
    if not ROLLYPAY_API_KEY or not ROLLYPAY_TERMINAL_ID:
        await message.answer("Оплата RollyPay пока не настроена. Обратись к администратору.")
        return False

    amount = removal_price_rub() if action == "post_removal" else author_info_price_rub()
    prefix = "postrm" if action == "post_removal" else ("postinfofwd" if from_forward else "postinfo")
    db.register_user(
        buyer.id,
        getattr(buyer, "username", None),
        getattr(buyer, "full_name", "Пользователь"),
    )
    order_id = f"{prefix}-{int(sub['id'])}-{uuid4().hex[:16]}"
    title = post_action_title(action, int(sub["id"]))
    metadata = {
        "action": action,
        "submission_id": str(sub["id"]),
        "chat_id": str(sub["published_chat_id"]),
        "message_ids": message_ids,
        "buyer_user_id": str(buyer.id),
    }
    try:
        payment = await create_payment(
            api_base=ROLLYPAY_API_BASE, api_key=ROLLYPAY_API_KEY,
            terminal_id=ROLLYPAY_TERMINAL_ID, order_id=order_id,
            amount=Decimal(amount), description=title,
            customer_id=str(buyer.id), metadata=metadata,
            test=ROLLYPAY_TEST_MODE,
            # Payment fulfillment is polled by the bot independently of the VPN site.
            success_url=f"https://t.me/{PUBLIC_BOT_USERNAME}",
            fail_url=f"https://t.me/{PUBLIC_BOT_USERNAME}",
        )
        db.create_removal_payment(
            str(payment["payment_id"]), order_id, int(sub["id"]),
            buyer.id, amount, action=action,
        )
    except (RollyPayError, KeyError, ValueError) as exc:
        logger.warning("Could not create paid post action: %s", exc)
        await message.answer("Не удалось создать оплату. Попробуй ещё раз.")
        return False

    text = f"<b>{title}</b>\n\nСтоимость: <b>{amount} ₽</b>"
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"Оплатить {amount} ₽", url=str(payment["pay_url"]))
        ]]),
    )
    return True


async def start_removal_payment(message: Message, submission_id: int, token: str) -> None:
    """Preserve previously published per-post deep links."""
    sub = db.removal_target(submission_id, token)
    if not sub:
        await message.answer(
            "Нет записи для этой старой ссылки. Воспользуйся кнопкой «Удалить пост» "
            "и пришли ссылку на сообщение из канала."
        )
        return
    await start_post_action_payment(message, sub, "post_removal")


async def resolve_post_from_link(bot: Bot, raw: str):
    channel, message_id = parse_post_link(raw)
    return await resolve_post_target(bot, channel, message_id)


def historical_post_fingerprint(message: Message) -> str:
    return publication_text_fingerprint(
        getattr(message, "text", None) or getattr(message, "caption", None) or ""
    )


def bind_historical_forward(message: Message, channel: int, message_id: int):
    """Match an exported post by content and remember its real Telegram id."""
    fingerprint = historical_post_fingerprint(message)
    sub = db.find_published_submission_by_fingerprint(fingerprint)
    if sub:
        db.attach_publication_message(int(sub["id"]), channel, message_id)
    return sub


async def recover_historical_link_target(
    request: Message, bot: Bot, channel: str, message_id: int
):
    """Resolve a link-only old post through a short-lived verified forward.

    Bot API has no get-message endpoint.  Forwarding to the requesting member,
    fingerprinting the returned Message, and deleting it immediately is the only
    Bot API-only way to inspect an old channel post without MTProto credentials.
    """
    configured = destination_chat_id()
    if configured is None or not request.from_user or not request.chat:
        return None
    is_public = isinstance(destination_chat_value(), str) and str(destination_chat_value()).startswith("@")
    if not is_public:
        try:
            member = await bot.get_chat_member(configured, request.from_user.id)
            status = str(getattr(member, "status", "")).split(".")[-1].lower()
            if status in {"left", "kicked", "banned"}:
                return None
        except TelegramAPIError:
            return None
    temporary = None
    try:
        temporary = await bot.forward_message(
            chat_id=request.chat.id,
            from_chat_id=configured,
            message_id=message_id,
            disable_notification=True,
        )
        if isinstance(configured, int):
            source_chat_id = configured
        else:
            source_chat_id = int((await bot.get_chat(configured)).id)
        return bind_historical_forward(temporary, source_chat_id, message_id)
    except (AttributeError, TelegramAPIError) as exc:
        logger.info("Could not inspect historical publication %s/%s: %s", channel, message_id, exc)
        return None
    finally:
        if temporary is not None:
            try:
                await bot.delete_message(request.chat.id, temporary.message_id)
            except TelegramAPIError:
                pass


async def resolve_post_target(bot: Bot, channel: str, message_id: int):
    configured = destination_chat_id()
    if configured is None:
        raise InvalidPostLink("Администратор ещё не настроил канал для публикаций.")
    if isinstance(configured, int):
        expected_chat_id = configured
    else:
        try:
            expected_chat_id = (await bot.get_chat(configured)).id
        except TelegramAPIError as exc:
            logger.warning("Could not resolve configured publication chat: %s", exc)
            raise InvalidPostLink("Не удалось проверить канал публикаций.") from exc

    if channel.startswith("@"):
        try:
            actual_chat_id = (await bot.get_chat(channel)).id
        except TelegramAPIError as exc:
            raise InvalidPostLink("Этот публичный канал не удалось проверить.") from exc
    else:
        actual_chat_id = int(channel)
    if int(actual_chat_id) != int(expected_chat_id):
        raise InvalidPostLink("Пришли ссылку или перешли пост именно из нашего канала.")

    original_config = destination_chat_value()
    stored_alias = str(original_config) if original_config is not None else ""
    return db.find_published_submission(
        int(expected_chat_id), message_id, channel_alias=stored_alias
    )


async def process_post_link(message: Message, bot: Bot, action: str) -> bool:
    forwarded = forwarded_channel_post(message)
    if not forwarded and not message.text:
        await message.answer("Пришли ссылку или перешли оригинальный пост из канала.")
        return False
    try:
        if forwarded:
            channel, message_id = forwarded
            sub = await resolve_post_target(bot, str(channel), message_id)
            if sub is None:
                sub = bind_historical_forward(message, channel, message_id)
        elif getattr(message, "forward_origin", None) is not None:
            raise InvalidPostLink("Нужна оригинальная пересылка из канала.")
        else:
            channel, message_id = parse_post_link(message.text.strip())
            sub = await resolve_post_target(bot, channel, message_id)
            if sub is None:
                sub = await recover_historical_link_target(message, bot, channel, message_id)
    except InvalidPostLink as exc:
        await message.answer(str(exc) + "\n\nНапример: <code>https://t.me/c/1234567890/123</code>")
        return False
    if sub is None:
        await message.answer("Не нашёл пост.")
        return False
    return await start_post_action_payment(message, sub, action, from_forward=bool(forwarded))


async def offer_forwarded_post_actions(message: Message, bot: Bot) -> bool:
    forwarded = forwarded_channel_post(message)
    if not forwarded:
        return False
    channel, message_id = forwarded
    try:
        sub = await resolve_post_target(bot, str(channel), message_id)
        if sub is None:
            sub = bind_historical_forward(message, channel, message_id)
    except InvalidPostLink:
        # Forwards from other channels remain ordinary suggestions.
        return False
    if not sub or sub["removed_at"]:
        await message.answer("Публикация не найдена или уже удалена.")
        return True
    sid = int(sub["id"])
    rows = [[InlineKeyboardButton(
        text=post_action_title("post_removal", sid),
        callback_data=f"postforward:post_removal:{channel}:{message_id}",
    )]]
    rows.append([InlineKeyboardButton(
        text=post_action_title("author_lookup", sid),
        callback_data=f"postforward:author_lookup:{channel}:{message_id}",
    )])
    await message.answer(
        "Выбери действие:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    return True


@router.callback_query(F.data.startswith("postforward:"), F.message.chat.type == "private")
async def forwarded_post_action(callback: CallbackQuery, bot: Bot) -> None:
    match = re.fullmatch(r"postforward:(post_removal|author_lookup):(-[0-9]+):([1-9][0-9]*)", callback.data or "")
    if not match or not isinstance(callback.message, Message):
        await callback.answer("Некорректная публикация.", show_alert=True)
        return
    action, channel, message_id = match.groups()
    try:
        sub = await resolve_post_target(bot, channel, int(message_id))
    except InvalidPostLink as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    if not sub or sub["removed_at"]:
        await callback.answer("Публикация не найдена или уже удалена.", show_alert=True)
        return
    await callback.answer()
    if await start_post_action_payment(callback.message, sub, action, buyer=callback.from_user, from_forward=True):
        db.clear_post_action(callback.from_user.id)


async def delete_published_submission(bot: Bot, sub, message_ids: list[int]) -> bool:
    chat_id = parse_chat_id(sub["published_chat_id"])
    if chat_id is None or not message_ids:
        return False
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if "message to delete not found" not in text and "message identifier is not specified" not in text:
                logger.warning("Could not delete paid post message %s: %s", message_id, exc)
                return False
        except TelegramAPIError as exc:
            logger.warning("Could not delete paid post message %s: %s", message_id, exc)
            return False
    return True


async def deliver_author_info(bot: Bot, payment) -> None:
    """Send verified paid identity details to the buyer."""
    full_name = html.escape(str(payment["full_name"] or "Без имени"))
    username = str(payment["username"] or "").strip()
    display_username = "@" + html.escape(username.lstrip("@")) if username else "не указан"
    await bot.send_message(
        chat_id=int(payment["buyer_user_id"]),
        text=(
            "<b>Информация об авторе публикации</b>\n\n"
            f"Ник: <b>{full_name}</b>\n"
            f"Username: <b>{display_username}</b>\n"
            f"Telegram ID: <code>{int(payment['user_id'])}</code>"
        ),
        reply_markup=POST_ACTION_KEYBOARD,
    )



def verified_rollypay_payment(remote: dict, local) -> bool:
    """Do not credit an unrelated payment or a test payment on production."""
    is_test = str(remote.get("test", False)).strip().lower() in {"1", "true", "yes"}
    return (
        str(remote.get("payment_id")) == str(local["payment_id"])
        and str(remote.get("order_id")) == str(local["order_id"])
        and str(remote.get("currency") or remote.get("payment_currency") or "").upper() == "RUB"
        and Decimal(str(remote.get("amount"))) == Decimal(int(local["amount_rub"]))
        and (ROLLYPAY_TEST_MODE or not is_test)
    )


def payment_group_text(payment) -> str:
    kind = {
        "post_action": "Платная услуга",
        "invoice": "Счёт",
        "giveaway": "Билеты в розыгрыш",
    }.get(str(payment["payment_type"]), "Оплата")
    username = str(payment["buyer_username"] or "").lstrip("@")
    username_text = "@" + html.escape(username) if username else "не указан"
    return (
        f"✅ <b>Оплата подтверждена — {html.escape(kind)}</b>\n\n"
        f"👤 Имя: <b>{html.escape(str(payment['buyer_name']))}</b>\n"
        f"🔗 Username: <b>{username_text}</b>\n"
        f"🆔 Telegram ID: <code>{int(payment['buyer_id'])}</code>\n"
        f"💰 Сумма: <b>{int(payment['amount_rub'])} ₽</b>\n"
        f"📦 Назначение: {html.escape(str(payment['description']))}\n"
        f"🧾 Платёж: <code>{html.escape(str(payment['payment_id'])[:120])}</code>"
    )


async def flush_payment_group_notifications(bot: Bot) -> None:
    """Persistent outbox: a failed group delivery stays pending for the next poll."""
    chat_id = moderation_chat_id()
    if chat_id is None:
        logger.error("Paid-payment alerts pending: MODERATION_CHAT_ID is not configured")
        return
    for payment in db.pending_payment_notifications():
        try:
            await safe_bot_send_message(bot, chat_id, payment_group_text(payment))
        except TelegramAPIError as exc:
            logger.warning(
                "Payment alert not delivered to moderation group (will retry) %s/%s: %s",
                payment["payment_type"], payment["payment_id"], exc,
            )
            continue
        db.mark_payment_notified(str(payment["payment_type"]), str(payment["payment_id"]))
        await asyncio.sleep(0.05)


async def reconcile_removal_payments(bot: Bot) -> None:
    """Poll RollyPay independently of website webhook; fulfill both paid actions."""
    while True:
        if ROLLYPAY_API_KEY:
            for payment in db.pending_removal_payments():
                payment_id = str(payment["payment_id"])
                try:
                    remote = await get_payment(
                        api_base=ROLLYPAY_API_BASE, api_key=ROLLYPAY_API_KEY,
                        payment_id=payment_id,
                    )
                    status = str(remote.get("status") or "").lower()
                    if status == "paid":
                        if not verified_rollypay_payment(remote, payment):
                            logger.error("Paid post action failed exact validation: %s", payment_id)
                            continue
                        db.set_removal_payment_status(payment_id, "processing")
                        action = str(payment["action"] or "post_removal")
                        if action == "author_lookup":
                            await deliver_author_info(bot, payment)
                            db.set_removal_payment_status(payment_id, "paid")
                        elif action == "post_removal":
                            if payment["removed_at"]:
                                db.set_removal_payment_status(payment_id, "paid_duplicate")
                                await safe_notify(
                                    bot, int(payment["buyer_user_id"]),
                                    "Платёж подтверждён, но этот пост уже удалён. "
                                    "Обратитесь к администратору за возвратом повторной оплаты.",
                                )
                                continue
                            if await delete_published_submission(
                                bot, payment, published_message_ids(payment)
                            ):
                                db.mark_removed(int(payment["submission_id"]))
                                db.set_removal_payment_status(payment_id, "paid")
                                await safe_notify(
                                    bot, int(payment["buyer_user_id"]),
                                    f"{ce('check')} Оплата подтверждена. Пост удалён.",
                                )
                        else:
                            logger.error("Unknown paid post action %r in %s", action, payment_id)
                    elif status in {"expired", "canceled", "cancelled", "failed"}:
                        db.set_removal_payment_status(payment_id, status)
                except (RollyPayError, InvalidOperation, TypeError, ValueError) as exc:
                    logger.warning("Paid action reconciliation failed for %s: %s", payment_id, exc)
                except TelegramAPIError as exc:
                    logger.warning(
                        "Could not deliver paid post action to buyer (will retry) %s: %s",
                        payment_id, exc,
                    )
                await asyncio.sleep(0.05)
            for ticket in db.pending_giveaway_tickets():
                try:
                    remote = await get_payment(api_base=ROLLYPAY_API_BASE, api_key=ROLLYPAY_API_KEY, payment_id=str(ticket['payment_id']))
                    status = str(remote.get('status') or '').lower()
                    if status == 'paid' and verified_rollypay_payment(remote, ticket):
                        db.set_giveaway_ticket_status(str(ticket['payment_id']), 'paid')
                        await safe_notify(bot, int(ticket['user_id']), f"Оплата подтверждена. Билет в розыгрыш «{html.escape(str(ticket['title']))}» получен.")
                    elif status in {'expired', 'canceled', 'cancelled', 'failed'}: db.set_giveaway_ticket_status(str(ticket['payment_id']), status)
                except (RollyPayError, InvalidOperation, TypeError, ValueError) as exc: logger.warning('Ticket reconciliation failed: %s', exc)
            for invoice in db.pending_invoices():
                try:
                    remote = await get_payment(api_base=ROLLYPAY_API_BASE, api_key=ROLLYPAY_API_KEY, payment_id=str(invoice['payment_id']))
                    status = str(remote.get('status') or '').lower()
                    if status == 'paid' and verified_rollypay_payment(remote, invoice):
                        db.set_invoice_status(str(invoice['payment_id']), 'paid')
                        await safe_notify(bot, int(invoice['buyer_user_id']), 'Оплата по счёту подтверждена.')
                        await safe_notify(bot, int(invoice['created_by_id']), f"Счёт для <code>{invoice['buyer_user_id']}</code> оплачен.")
                    elif status in {'expired', 'canceled', 'cancelled', 'failed'}: db.set_invoice_status(str(invoice['payment_id']), status)
                except (RollyPayError, InvalidOperation, TypeError, ValueError) as exc: logger.warning('Invoice reconciliation failed: %s', exc)
            for giveaway in db.due_giveaways(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")):
                giveaway_id = int(giveaway['id'])
                winner = db.draw_giveaway(giveaway_id)
                final = db.giveaway(giveaway_id)
                winner_text = f"<code>{winner}</code>" if winner else "нет оплаченных билетов"
                result = f"🏁 <b>Розыгрыш #{giveaway_id} завершён</b>\n{html.escape(str(giveaway['title']))}\n\nПобедитель: {winner_text}"
                await safe_bot_send_message(bot, moderation_chat_id(), result)
                channel_id, channel_message_id = final['channel_chat_id'], final['channel_message_id']
                if channel_id and channel_message_id:
                    try:
                        await bot.edit_message_text(
                            chat_id=channel_id, message_id=int(channel_message_id), text=result, reply_markup=None,
                        )
                    except TelegramAPIError as exc:
                        logger.warning("Could not close giveaway card %s: %s", giveaway_id, exc)
        await flush_payment_group_notifications(bot)
        await asyncio.sleep(12)

async def consume_admin_session(message: Message, bot: Bot) -> bool:
    """Process the value requested by the admin panel in the chat that opened it."""
    if not message.from_user:
        return False
    sender_id = message.from_user.id
    session = admin_sessions.get(sender_id)
    message_chat_id = getattr(getattr(message, "chat", None), "id", None)
    if not session or len(session) < 2 or session[1] != message_chat_id:
        return False
    if not await is_moderation_admin(bot, sender_id):
        admin_sessions.pop(sender_id, None)
        await message.answer("Команда доступна только администратору.")
        return True

    admin_mode = str(session[0])
    session_data = session[2] if len(session) > 2 and isinstance(session[2], dict) else {}
    if admin_mode:
        value = (message.text or "").strip()
        if admin_mode == "invoice":
            parts = value.split(maxsplit=1)
            if len(parts) != 2 or not parts[0].isdigit() or not 1 <= int(parts[0]) <= 100000:
                await message.answer("Формат: <code>500 Оплата услуги</code>"); return True
            if not ROLLYPAY_API_KEY or not ROLLYPAY_TERMINAL_ID:
                await message.answer("Оплата RollyPay пока не настроена."); return True
            amount, description = int(parts[0]), parts[1]
            db.register_user(
                message.from_user.id,
                getattr(message.from_user, "username", None),
                getattr(message.from_user, "full_name", "Пользователь"),
            )
            order_id = f"invoice-{uuid4().hex[:18]}"
            try:
                payment = await create_payment(api_base=ROLLYPAY_API_BASE, api_key=ROLLYPAY_API_KEY, terminal_id=ROLLYPAY_TERMINAL_ID, order_id=order_id, amount=Decimal(amount), description=description, customer_id=str(sender_id), metadata={"action":"invoice","buyer_user_id":str(sender_id),"created_by":str(sender_id)}, test=ROLLYPAY_TEST_MODE, success_url=f"https://t.me/{PUBLIC_BOT_USERNAME}", fail_url=f"https://t.me/{PUBLIC_BOT_USERNAME}")
                db.create_invoice(str(payment['payment_id']), order_id, sender_id, sender_id, amount, description)
            except (RollyPayError, KeyError, ValueError):
                await message.answer("Не удалось создать счёт."); return True
            admin_sessions.pop(sender_id, None)
            await message.answer(f"<b>Счёт на оплату</b>\n{html.escape(description)}\nСумма: <b>{amount} ₽</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть оплату", url=str(payment['pay_url']))]])); return True
        if admin_mode == "service_price":
            parts = value.casefold().split()
            if len(parts) != 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 100000:
                await message.answer("Формат: <code>удаление 100</code> или <code>автор 100</code>"); return True
            if parts[0] in {"удаление", "delete"}: key, label = "post_removal_price_rub", "удаления"
            elif parts[0] in {"автор", "author"}: key, label = "post_author_info_price_rub", "сведений об авторе"
            else: await message.answer("Укажи «удаление» или «автор»."); return True
            db.set_setting(key, parts[1]); admin_sessions.pop(sender_id, None)
            await message.answer(f"Цена {label}: <b>{parts[1]} ₽</b>."); return True
        if admin_mode == "ticket_price":
            if not value.isdigit() or not 1 <= int(value) <= 100000:
                await message.answer("Пришли цену целым числом от 1 до 100000."); return True
            db.set_setting("giveaway_ticket_price_rub", value)
            db.set_active_giveaway_price(int(value))
            admin_sessions.pop(sender_id, None)
            await message.answer(f"Цена билета: <b>{value} ₽</b>."); return True
        if admin_mode == "giveaway_title":
            if not value or len(value) > 180:
                await message.answer("Пришли название приза до 180 символов."); return True
            admin_sessions[sender_id] = ("giveaway_price", message.chat.id, {"title": value})
            await message.answer(
                "Укажи цену одного билета в рублях.",
                reply_markup=ForceReply(force_reply=True, selective=True),
            )
            return True
        if admin_mode == "giveaway_price":
            if not value.isdigit() or not 1 <= int(value) <= 100000:
                await message.answer("Цена — целое число от 1 до 100000."); return True
            admin_sessions[sender_id] = ("giveaway_ends_at", message.chat.id, {**session_data, "price": int(value)})
            await message.answer(
                "Укажи окончание: <code>24ч</code>, <code>7д</code> или дату <code>25.11.2026 21:00</code>.",
                reply_markup=ForceReply(force_reply=True, selective=True),
            )
            return True
        if admin_mode == "giveaway_ends_at":
            ends_at = parse_giveaway_end(value)
            if not ends_at:
                await message.answer("Не понял дату. Пример: <code>24ч</code>, <code>7д</code> или <code>25.11.2026 21:00</code>."); return True
            giveaway_id = db.create_giveaway(
                str(session_data["title"]), int(session_data["price"]), sender_id, message.chat.id, ends_at,
            )
            admin_sessions.pop(sender_id, None)
            card = await message.answer(giveaway_card(giveaway_id), reply_markup=giveaway_keyboard(giveaway_id))
            db.set_giveaway_message(giveaway_id, card.message_id)
            return True
    return True


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    if not message.from_user.is_bot:
        db.register_user(
            message.from_user.id,
            message.from_user.username,
            message.from_user.full_name,
        )
    payload = (message.text or "").split(maxsplit=1)
    if len(payload) == 2:
        match = re.fullmatch(r"remove_(\d+)_([A-Za-z0-9_-]{12,40})", payload[1].strip())
        if match:
            await message.answer(
                "В боте доступно получение информации об авторе публикации.",
                reply_markup=POST_ACTION_KEYBOARD,
            )
            await start_removal_payment(message, int(match.group(1)), match.group(2))
            return
    if await is_blocked_from_suggestions(bot, message.from_user.id):
        await safe_answer(
            message,
            f"{ce('ban')} Доступ к отправке предложений заблокирован. "
            "Платные действия с опубликованными постами по-прежнему доступны.",
            reply_markup=POST_ACTION_KEYBOARD,
        )
        return
    await safe_answer(
        message,
        f"{ce('wave')} <b>Предложка</b>\n\n"
        "Отправь текст или медиа для публикации, либо выбери действие ниже.",
        reply_markup=POST_ACTION_KEYBOARD,
    )


@router.message(Command("help"))
async def help_command(message: Message, bot: Bot) -> None:
    if message.chat.type == "private":
        if message.from_user and not message.from_user.is_bot:
            db.register_user(
                message.from_user.id,
                message.from_user.username,
                message.from_user.full_name,
            )
        await safe_answer(
            message,
            "ℹ️ <b>Доступные команды</b>\n"
            "/start — отправить предложение\n"
            "/deletepost — удалить пост по ссылке или пересылке после оплаты\n"
            "/who — узнать автора публикации после оплаты\n"
            "/cancel — отменить ожидание публикации\n"
            "/help — показать эту справку",
            reply_markup=POST_ACTION_KEYBOARD,
        )
        return
    if message.chat.type not in {"group", "supergroup"} or message.chat.id != moderation_chat_id():
        return
    await safe_answer(
        message,
        "ℹ️ <b>Команды группы модерации</b>\n\n"
        "/top — рейтинг модераторов и статистика публикаций\n"
        "/users — список пользователей с ником и ID\n"
        "/done ТЕКСТ — рассылка всем пользователям; также можно ответить командой /done на сообщение "
        "и разослать его (после подтверждения)\n"
        "/unblock USER_ID — снять блокировку\n"
        "/setdeleteprice СУММА — изменить цену удаления поста в рублях\n"
        "/setinfoprice СУММА — изменить цену сведений об авторе в рублях\n"
        "/cleanbuttons — убрать кнопки с сообщения, на которое ответили; "
        "/cleanbuttons POST_ID — убрать кнопки в канале\n"
        "/help — список команд",
    )


@router.message(Command("deletepost", "who"), F.chat.type == "private")
async def post_action_command(message: Message) -> None:
    if not message.from_user:
        return
    action = "author_lookup" if (message.text or "").split()[0].split("@")[0] == "/who" else "post_removal"
    db.set_post_action(message.from_user.id, action)
    if action == "post_removal":
        await message.answer(
            "Пришлите ссылку или перешлите публикацию из канала для удаления.\n"
            "Пример: <code>https://t.me/c/1234567890/456</code>",
            reply_markup=POST_ACTION_KEYBOARD,
        )
        return
    await message.answer(
        "Пришлите ссылку или перешлите публикацию из канала.\n"
        "Пример: <code>https://t.me/c/1234567890/456</code>",
        reply_markup=POST_ACTION_KEYBOARD,
    )


@router.message(Command("cancel"), F.chat.type == "private")
async def post_action_cancel(message: Message) -> None:
    if message.from_user:
        db.clear_post_action(message.from_user.id)
    await message.answer(
        "Ожидание публикации отменено. Можешь отправить новый пост или выбрать кнопку.",
        reply_markup=POST_ACTION_KEYBOARD,
    )


def ticket_price_rub() -> int:
    raw = db.get_setting("giveaway_ticket_price_rub") or "100"
    try: return max(1, min(100000, int(raw)))
    except (TypeError, ValueError): return 100


def giveaway_timezone() -> ZoneInfo | timezone:
    try:
        return ZoneInfo(TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def parse_giveaway_end(value: str) -> str | None:
    """Convert an admin's duration/date into a UTC SQLite timestamp."""
    raw = value.strip().casefold()
    match = re.fullmatch(r"(\d{1,3})\s*(ч|час(?:а|ов)?|h|д|дн(?:я|ей)?|d)", raw)
    if match:
        count, unit = int(match.group(1)), match.group(2)
        if not 1 <= count <= 365:
            return None
        delta = timedelta(hours=count) if unit.startswith(("ч", "h")) else timedelta(days=count)
        return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%d %H:%M:%S")
    for pattern in ("%d.%m.%Y %H:%M", "%d.%m %H:%M"):
        try:
            parsed = datetime.strptime(value.strip(), pattern)
            if pattern == "%d.%m %H:%M":
                parsed = parsed.replace(year=datetime.now(giveaway_timezone()).year)
            local = parsed.replace(tzinfo=giveaway_timezone())
            if local <= datetime.now(giveaway_timezone()):
                return None
            return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def giveaway_end_text(giveaway) -> str:
    return sent_at(str(giveaway["ends_at"])) if giveaway["ends_at"] else "не задано"


def giveaway_keyboard(giveaway_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 билет", callback_data=f"giveaway:buy:{giveaway_id}:1"), InlineKeyboardButton(text="3 билета", callback_data=f"giveaway:buy:{giveaway_id}:3")],
        [InlineKeyboardButton(text="5 билетов", callback_data=f"giveaway:buy:{giveaway_id}:5"), InlineKeyboardButton(text="10 билетов", callback_data=f"giveaway:buy:{giveaway_id}:10")],
        [InlineKeyboardButton(text="Мои билеты", callback_data=f"giveaway:mine:{giveaway_id}"), InlineKeyboardButton(text="Статистика", callback_data=f"giveaway:stats:{giveaway_id}")],
    ])


def giveaway_card(giveaway_id: int) -> str:
    giveaway = db.giveaway(giveaway_id)
    stats = db.giveaway_stats(giveaway_id)
    return (
        f"<b>🎁 Розыгрыш #{giveaway_id}</b>\n{html.escape(str(giveaway['title']))}\n\n"
        f"🎟 Билет: <b>{giveaway['ticket_price_rub']} ₽</b>\n"
        f"⏳ Окончание: <b>{giveaway_end_text(giveaway)}</b>\n"
        f"👥 Участников: <b>{stats['buyers']}</b>\n"
        f"🎫 Билетов: <b>{stats['tickets']}</b>"
    )



def payment_status_label(status: str) -> str:
    return {
        "paid": "✅ Оплачен",
        "paid_duplicate": "⚠️ Оплачен повторно",
        "created": "⏳ Ожидает оплаты",
        "processing": "✅ Оплачен · выполняется",
        "expired": "⌛ Истёк",
        "canceled": "🚫 Отменён",
        "cancelled": "🚫 Отменён",
        "failed": "❌ Ошибка",
    }.get(status, html.escape(status))


def render_payments_page(status_filter: str = "all", page: int = 0) -> str:
    counts = db.payment_counts()
    total = int(counts[{"all": "total", "paid": "paid", "unpaid": "unpaid"}[status_filter]])
    page = min(max(0, page), max(0, (total - 1) // PAYMENTS_PAGE_SIZE))
    records = db.list_payments(status_filter, page * PAYMENTS_PAGE_SIZE, PAYMENTS_PAGE_SIZE)
    heading = {"all": "Все", "paid": "Оплаченные", "unpaid": "Неоплаченные"}[status_filter]
    lines = [
        "💳 <b>Платежи</b>",
        f"Всего: <b>{counts['total']}</b> · Оплачено: <b>{counts['paid']}</b> · "
        f"Не оплачено: <b>{counts['unpaid']}</b>",
        f"Выручка: <b>{counts['revenue']} ₽</b>",
        f"\n<b>{heading}</b> · страница {page + 1}/{max(1, (total + PAYMENTS_PAGE_SIZE - 1) // PAYMENTS_PAGE_SIZE)}",
    ]
    for index, payment in enumerate(records, page * PAYMENTS_PAGE_SIZE + 1):
        username = str(payment["buyer_username"] or "").lstrip("@")
        username_display = "@" + html.escape(username) if username else "не указан"
        lines.append(
            f"\n<b>{index}. {payment_status_label(str(payment['status']))}</b>"
            f"\n{html.escape(str(payment['description']))}"
            f"\n👤 {html.escape(str(payment['buyer_name']))} · {username_display}"
            f"\n🆔 <code>{int(payment['buyer_id'])}</code> · 💰 <b>{int(payment['amount_rub'])} ₽</b>"
            f"\n🕒 {html.escape(str(payment['paid_at'] or payment['created_at']))}"
            f"\n<code>{html.escape(str(payment['payment_id'])[:72])}</code>"
        )
    if not records:
        lines.append("\nПлатежей в этом разделе пока нет.")
    return "\n".join(lines)


def payments_page_keyboard(status_filter: str = "all", page: int = 0) -> InlineKeyboardMarkup:
    counts = db.payment_counts()
    total = int(counts[{"all": "total", "paid": "paid", "unpaid": "unpaid"}[status_filter]])
    last_page = max(0, (total - 1) // PAYMENTS_PAGE_SIZE)
    page = min(max(0, page), last_page)
    filters = [
        InlineKeyboardButton(
            text=("● " if status_filter == key else "") + label,
            callback_data=f"gpay:{key}:0",
        )
        for key, label in (("all", "Все"), ("paid", "Оплаченные"), ("unpaid", "Неоплаченные"))
    ]
    rows = [filters]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"gpay:{status_filter}:{page - 1}"))
    if page < last_page:
        navigation.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"gpay:{status_filter}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="↩️ Админ-меню", callback_data="gadmin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rollypay_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обновить", callback_data="gadmin:rollypay")],
        [InlineKeyboardButton(text="↩️ Админ-меню", callback_data="gadmin:menu")],
    ])


async def rollypay_admin_card() -> str:
    if not ROLLYPAY_API_KEY:
        return "💰 <b>RollyPay</b>\nAPI-ключ не настроен."
    try:
        balance, rate_data, all_payments, paid_payments = await asyncio.gather(
            get_balance(
                api_base=ROLLYPAY_API_BASE,
                api_key=ROLLYPAY_API_KEY,
                terminal_id=ROLLYPAY_TERMINAL_ID,
            ),
            get_rate(
                api_base=ROLLYPAY_API_BASE,
                api_key=ROLLYPAY_API_KEY,
                terminal_id=ROLLYPAY_TERMINAL_ID,
            ),
            list_rollypay_payments(
                api_base=ROLLYPAY_API_BASE,
                api_key=ROLLYPAY_API_KEY,
                terminal_id=ROLLYPAY_TERMINAL_ID,
            ),
            list_rollypay_payments(
                api_base=ROLLYPAY_API_BASE,
                api_key=ROLLYPAY_API_KEY,
                terminal_id=ROLLYPAY_TERMINAL_ID,
                status="paid",
            ),
        )
        available = Decimal(str(balance.get("available_usdt", "0")))
        hold = Decimal(str(balance.get("hold_usdt", "0")))
        rate = Decimal(str(rate_data.get("rate", "0")))
        total = int(all_payments.get("total", len(all_payments.get("items", []))))
        paid = int(paid_payments.get("total", len(paid_payments.get("items", []))))
    except (RollyPayError, InvalidOperation, TypeError, ValueError) as exc:
        logger.warning("RollyPay admin summary failed: %s", exc)
        return "💰 <b>RollyPay</b>\nНе удалось получить данные кассы. Попробуй обновить позже."
    terminal = str(balance.get("terminal_id") or ROLLYPAY_TERMINAL_ID or "не указан")
    if len(terminal) > 18:
        terminal = terminal[:8] + "…" + terminal[-6:]
    rub_estimate = available * rate
    return (
        "💰 <b>RollyPay</b>\n"
        f"Доступно: <b>{available:.2f} USDT</b> ≈ <b>{rub_estimate:.2f} ₽</b>\n"
        f"В удержании: <b>{hold:.2f} USDT</b>\n"
        f"Курс: <b>{rate:.2f} ₽/USDT</b>\n"
        f"Платежей в RollyPay: <b>{total}</b> · оплачено: <b>{paid}</b>\n"
        f"Касса: <code>{html.escape(terminal)}</code>\n"
        f"Режим: <b>{'тестовый' if ROLLYPAY_TEST_MODE else 'боевой'}</b>"
    )


@router.callback_query(F.data.startswith("gpay:"))
async def payments_pagination_callback(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or not callback.message or not callback.data:
        return
    if not await is_moderation_admin(bot, callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[1] not in {"all", "paid", "unpaid"} or not parts[2].isdigit():
        await callback.answer("Некорректный фильтр.", show_alert=True)
        return
    status_filter = parts[1]
    page = min(int(parts[2]), 1_000_000)
    try:
        await callback.message.edit_text(
            render_payments_page(status_filter, page),
            reply_markup=payments_page_keyboard(status_filter, page),
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer()


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    def button(label: str, callback_data: str, icon: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=label,
            callback_data=callback_data,
            icon_custom_emoji_id=CUSTOM_EMOJIS[icon][0],
        )

    return InlineKeyboardMarkup(inline_keyboard=[
        [button("Создать розыгрыш", "gadmin:create", "check"), button("Цена билета", "gadmin:price", "info")],
        [button("Опубликовать в канал", "gadmin:publish", "check"), button("Разослать", "gadmin:broadcast", "chat")],
        [button("Статистика", "gadmin:stats", "info"), button("Подвести итоги", "gadmin:draw", "timer")],
        [button("Создать счёт", "gadmin:invoice", "check"), button("Цены услуг", "gadmin:prices", "info")],
        [button("RollyPay", "gadmin:rollypay", "info"), button("Платежи", "gadmin:payments", "info")],
        [button("Пользователи", "gadmin:users", "profile"), button("Топ модераторов", "gadmin:top", "info")],
    ])


@router.message(Command("admin"))
async def admin_menu_command(message: Message, bot: Bot) -> None:
    if not message.from_user or not await is_moderation_admin(bot, message.from_user.id):
        await message.answer("Команда доступна только администратору."); return
    await message.answer(
        f"{ce('info')} <b>Админ-меню</b>\nУправление оплатами и розыгрышами.",
        reply_markup=admin_menu_keyboard(),
    )


@router.callback_query(F.data.startswith("gadmin:"))
async def giveaway_admin_callback(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or not callback.message: return
    if not await is_moderation_admin(bot, callback.from_user.id): await callback.answer("Нет доступа.", show_alert=True); return
    action = callback.data.split(":", 1)[1]
    if action == "payments":
        await callback.answer()
        await callback.message.answer(render_payments_page(), reply_markup=payments_page_keyboard())
        return
    if action == "rollypay":
        await callback.answer()
        await safe_edit_text(
            callback.message,
            await rollypay_admin_card(),
            reply_markup=rollypay_admin_keyboard(),
        )
        return
    if action == "menu":
        await callback.answer()
        await safe_edit_text(callback.message, f"{ce('info')} <b>Админ-меню</b>\nУправление оплатами и розыгрышами.", reply_markup=admin_menu_keyboard())
        return
    active = db.active_giveaway()
    if action == "create":
        admin_sessions[callback.from_user.id] = ("giveaway_title", callback.message.chat.id); await callback.answer()
        await callback.message.answer(
            "Ответь на это сообщение названием приза для нового розыгрыша.",
            reply_markup=ForceReply(force_reply=True, selective=True),
        ); return
    if action == "price":
        admin_sessions[callback.from_user.id] = ("ticket_price", callback.message.chat.id); await callback.answer()
        await callback.message.answer(
            f"Текущая цена: <b>{ticket_price_rub()} ₽</b>\nОтветь на это сообщение новой ценой числом.",
            reply_markup=ForceReply(force_reply=True, selective=True),
        ); return
    if action == "invoice":
        admin_sessions[callback.from_user.id] = ("invoice", callback.message.chat.id); await callback.answer()
        await callback.message.answer(
            "Ответь на это сообщение суммой и описанием через пробел.\nПример: <code>500 Оплата услуги</code>",
            reply_markup=ForceReply(force_reply=True, selective=True),
        ); return
    if action == "prices":
        await callback.answer()
        await callback.message.answer(
            f"<b>Цены услуг</b>\nУдаление: <b>{removal_price_rub()} ₽</b>\nАвтор: <b>{author_info_price_rub()} ₽</b>\n\n"
            "Ответь на это сообщение: <code>удаление 100</code> или <code>автор 100</code>.",
            reply_markup=ForceReply(force_reply=True, selective=True),
        )
        admin_sessions[callback.from_user.id] = ("service_price", callback.message.chat.id); return
    if action == "users":
        total = db.user_count(); await callback.answer()
        await callback.message.answer(render_users_page(0), reply_markup=users_page_keyboard(0, total)); return
    if action == "top":
        rows = db.moderator_top(10); await callback.answer()
        lines = [f"{index}. <b>{html.escape(str(row['reviewed_by_name'] or 'Модератор'))}</b> · <b>{int(row['total'] or 0)}</b>" for index, row in enumerate(rows, 1)]
        await callback.message.answer("<b>Топ модераторов</b>\n" + ("\n".join(lines) or "Пока нет данных.")); return
    if not active:
        await callback.answer("Сначала создай розыгрыш.", show_alert=True); return
    giveaway_id = int(active['id'])
    if action == "stats":
        await callback.answer(); await callback.message.answer(giveaway_card(giveaway_id)); return
    if action == "draw":
        winner = db.draw_giveaway(giveaway_id); await callback.answer()
        await callback.message.answer(f"🏆 Победитель: <code>{winner}</code>" if winner else "Нет оплаченных билетов или розыгрыш уже завершён."); return
    if action == "publish":
        channel = destination_chat_id()
        if not channel: await callback.answer("Не настроен канал публикации.", show_alert=True); return
        try:
            published = await bot.send_message(channel, giveaway_card(giveaway_id), reply_markup=giveaway_keyboard(giveaway_id))
            db.set_giveaway_channel_message(giveaway_id, channel, published.message_id)
        except TelegramAPIError: await callback.answer("Не удалось опубликовать. Проверь права бота.", show_alert=True); return
        await callback.answer("Опубликовано."); return
    if action == "broadcast":
        sent = 0
        for user_id in db.all_user_ids():
            try:
                await bot.send_message(user_id, giveaway_card(giveaway_id), reply_markup=giveaway_keyboard(giveaway_id)); sent += 1
            except TelegramAPIError: pass
        await callback.answer(); await callback.message.answer(f"Рассылка завершена: <b>{sent}</b> получателей.")



@router.message(Command("invoice"))
async def invoice_command(message: Message, bot: Bot) -> None:
    if not message.from_user or not await is_moderation_admin(bot, message.from_user.id):
        await message.answer("Команда доступна только администраторам группы модерации."); return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 100000:
        await message.answer("Пример: <code>/invoice 500 Оплата услуги</code>"); return
    buyer_id, amount, description = message.from_user.id, int(parts[1]), parts[2]
    db.register_user(
        message.from_user.id,
        getattr(message.from_user, "username", None),
        getattr(message.from_user, "full_name", "Пользователь"),
    )
    if not ROLLYPAY_API_KEY or not ROLLYPAY_TERMINAL_ID: await message.answer("Оплата RollyPay пока не настроена."); return
    order_id = f"invoice-{uuid4().hex[:18]}"
    try:
        payment = await create_payment(api_base=ROLLYPAY_API_BASE, api_key=ROLLYPAY_API_KEY, terminal_id=ROLLYPAY_TERMINAL_ID, order_id=order_id, amount=Decimal(amount), description=description, customer_id=str(buyer_id), metadata={"action":"invoice","buyer_user_id":str(buyer_id),"created_by":str(message.from_user.id)}, test=ROLLYPAY_TEST_MODE, success_url=f"https://t.me/{PUBLIC_BOT_USERNAME}", fail_url=f"https://t.me/{PUBLIC_BOT_USERNAME}")
        db.create_invoice(str(payment['payment_id']), order_id, buyer_id, message.from_user.id, amount, description)
    except (RollyPayError, KeyError, ValueError): await message.answer("Не удалось создать счёт."); return
    await message.answer(f"<b>Счёт на оплату</b>\n{html.escape(description)}\nСумма: <b>{amount} ₽</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть оплату", url=str(payment['pay_url']))]]))


@router.message(Command("setticketprice"))
async def set_ticket_price_command(message: Message, bot: Bot) -> None:
    if not message.from_user or not await is_moderation_admin(bot, message.from_user.id):
        await message.answer("Команда доступна только администраторам группы модерации."); return
    value = (message.text or "").split(maxsplit=1)
    if len(value) != 2 or not value[1].isdigit() or not 1 <= int(value[1]) <= 100000:
        await message.answer(f"Текущая цена билета: <b>{ticket_price_rub()} ₽</b>\nПример: <code>/setticketprice 100</code>"); return
    db.set_setting("giveaway_ticket_price_rub", value[1])
    db.set_active_giveaway_price(int(value[1]))
    await message.answer(f"Цена билета: <b>{value[1]} ₽</b>.")


@router.message(Command("giveaway"))
async def giveaway_command(message: Message, bot: Bot) -> None:
    if not message.from_user or not await is_moderation_admin(bot, message.from_user.id):
        await message.answer("Команда доступна только администраторам группы модерации."); return
    title = (message.text or "").partition(" ")[2].strip()
    if not title:
        await message.answer("Пример: <code>/giveaway Розыгрыш Telegram Premium</code>"); return
    giveaway_id = db.create_giveaway(title, ticket_price_rub(), message.from_user.id, message.chat.id)
    card = await message.answer(f"<b>Розыгрыш #{giveaway_id}</b>\n{html.escape(title)}\n\nБилет: <b>{ticket_price_rub()} ₽</b>", reply_markup=giveaway_keyboard(giveaway_id))
    db.set_giveaway_message(giveaway_id, card.message_id)


@router.callback_query(F.data.startswith("giveaway:"))
async def giveaway_callback(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or not callback.data: return
    parts = callback.data.split(":"); _, action, raw_id = parts[:3]
    if not raw_id.isdigit(): return
    giveaway = db.giveaway(int(raw_id))
    if not giveaway: await callback.answer("Розыгрыш не найден.", show_alert=True); return
    if action == "stats":
        if not await is_moderation_admin(bot, callback.from_user.id): await callback.answer("Статистика только для админов.", show_alert=True); return
        stats = db.giveaway_stats(int(raw_id)); await callback.answer()
        await callback.message.answer(f"<b>Статистика розыгрыша #{raw_id}</b>\nБилетов: <b>{stats['tickets']}</b>\nУчастников: <b>{stats['buyers']}</b>\nСобрано: <b>{stats['revenue']} ₽</b>"); return
    if action == "mine":
        row = db.conn.execute("SELECT COALESCE(SUM(quantity),0) tickets FROM giveaway_tickets WHERE giveaway_id=? AND user_id=? AND status='paid'", (int(raw_id), callback.from_user.id)).fetchone()
        await callback.answer(f"Ваших подтверждённых билетов: {row['tickets']}", show_alert=True); return
    if action != "buy" or giveaway['status'] != 'active': await callback.answer("Розыгрыш завершён.", show_alert=True); return
    if giveaway['ends_at'] and str(giveaway['ends_at']) <= datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"):
        await callback.answer("Время розыгрыша уже истекло.", show_alert=True); return
    quantity = int(parts[3]) if len(parts) == 4 and parts[3].isdigit() and 1 <= int(parts[3]) <= 99 else 1
    if not ROLLYPAY_API_KEY or not ROLLYPAY_TERMINAL_ID: await callback.answer("Оплата пока не настроена.", show_alert=True); return
    order_id = f"ticket-{raw_id}-{uuid4().hex[:16]}"
    try:
        amount = int(giveaway['ticket_price_rub']) * quantity
        db.register_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
        payment = await create_payment(api_base=ROLLYPAY_API_BASE, api_key=ROLLYPAY_API_KEY, terminal_id=ROLLYPAY_TERMINAL_ID, order_id=order_id, amount=Decimal(amount), description=f"{quantity} билет(ов) в розыгрыш #{raw_id}", customer_id=str(callback.from_user.id), metadata={"action":"giveaway_ticket","giveaway_id":raw_id,"buyer_user_id":str(callback.from_user.id),"quantity":str(quantity)}, test=ROLLYPAY_TEST_MODE, success_url=f"https://t.me/{PUBLIC_BOT_USERNAME}", fail_url=f"https://t.me/{PUBLIC_BOT_USERNAME}")
        db.create_giveaway_ticket(str(payment['payment_id']), order_id, int(raw_id), callback.from_user.id, amount, quantity)
    except (RollyPayError, KeyError, ValueError): await callback.answer("Не удалось создать оплату.", show_alert=True); return
    await callback.answer(); await callback.message.answer(f"Билеты: <b>{quantity}</b> · к оплате: <b>{amount} ₽</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Оплатить билеты", url=str(payment['pay_url']))]]))


@router.message(Command("drawgiveaway"))
async def draw_giveaway_command(message: Message, bot: Bot) -> None:
    if not message.from_user or not await is_moderation_admin(bot, message.from_user.id):
        await message.answer("Команда доступна только администраторам группы модерации."); return
    raw = (message.text or "").partition(" ")[2].strip()
    if not raw.isdigit(): await message.answer("Пример: <code>/drawgiveaway 1</code>"); return
    winner = db.draw_giveaway(int(raw))
    await message.answer(f"Победитель розыгрыша #{raw}: <code>{winner}</code>" if winner else "Нет оплаченных билетов или розыгрыш уже завершён.")


async def can_view_moderation_stats(message: Message, bot: Bot) -> bool:
    """Allow moderation-group members and verified group admins in private chat."""
    if not message.from_user:
        return False
    if message.from_user.id in OWNER_USER_IDS:
        return True
    if message.chat.id == moderation_chat_id():
        return True
    if message.chat.type == "private" and await is_moderation_admin(bot, message.from_user.id):
        return True
    await safe_answer(
        message,
        "Команда доступна в группе модерации. В личке — только её администраторам. "
        "Если это группа модерации, проверь MODERATION_CHAT_ID на хостинге.",
    )
    return False


@router.message(Command("users"))
async def users_command(message: Message, bot: Bot) -> None:
    if not await can_view_moderation_stats(message, bot):
        return
    total = db.user_count()
    await safe_answer(
        message,
        render_users_page(0),
        reply_markup=users_page_keyboard(0, total),
    )


@router.message(Command("done"), F.chat.type.in_({"group", "supergroup"}))
async def done_command(message: Message, bot: Bot) -> None:
    if message.chat.id != moderation_chat_id():
        return
    if not message.from_user:
        return
    if not await is_moderation_admin(bot, message.from_user.id):
        await message.answer("Рассылка доступна только администраторам группы модерации.")
        return

    parts = (message.text or "").split(maxsplit=1)
    body = parts[1].strip() if len(parts) > 1 else ""
    if body:
        session: dict[str, object] = {
            "mode": "text",
            "text": body,
            "admin_id": message.from_user.id,
        }
        preview = html.escape(body[:700])
        if len(body) > 700:
            preview += "…"
        preview_text = f"<b>Текст рассылки:</b>\n<blockquote>{preview}</blockquote>"
    elif message.reply_to_message:
        session = {
            "mode": "copy",
            "source_chat_id": message.chat.id,
            "source_message_id": message.reply_to_message.message_id,
            "admin_id": message.from_user.id,
        }
        source = message.reply_to_message
        source_preview = html.escape(
            (source.text or source.caption or f"[{source.content_type}]")[:500]
        )
        preview_text = (
            "<b>Будет разослано сообщение, на которое ты ответил командой /done.</b>\n"
            f"<blockquote>{source_preview}</blockquote>"
        )
    else:
        await message.answer(
            "Использование:\n"
            "/done Текст рассылки\n"
            "или ответь командой /done на сообщение, которое нужно разослать."
        )
        return

    total = db.user_count()
    if total == 0:
        known_submissions = db.known_submission_user_count()
        await message.answer(
            "Пока нет пользователей в текущей базе для рассылки.\n"
            f"Пользователей из старых заявок найдено: {known_submissions}.\n\n"
            "Если ботом пользуются тысячи людей, проверь DB_PATH в BotHost: "
            "возможно, приложение запущено с новой пустой SQLite-базой. "
            "Telegram не позволяет автоматически получить полный список старых пользователей по /start."
        )
        return
    confirmation = await safe_answer(
        message,
        f"📣 <b>Подтверди рассылку</b>\n"
        f"Получателей в базе: <b>{total}</b>\n\n"
        f"{preview_text}\n\n"
        "Нажми «Отправить всем», чтобы начать.",
        reply_markup=broadcast_confirmation_keyboard(),
    )
    session["confirm_message_id"] = confirmation.message_id
    broadcast_sessions[message.from_user.id] = session


@router.message(Command("top"))
async def top_command(message: Message, bot: Bot) -> None:
    if not await can_view_moderation_stats(message, bot):
        return
    rows = db.moderator_top(10)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for index, row in enumerate(rows, start=1):
        medal = medals[index - 1] if index <= len(medals) else f"{index}."
        name = html.escape(str(row["reviewed_by_name"] or "Модератор"))
        user_id = int(row["reviewed_by_id"])
        total = int(row["total"] or 0)
        accepted = int(row["accepted"] or 0)
        rejected = int(row["rejected"] or 0)
        blocked = int(row["blocked"] or 0)
        lines.append(
            f"{medal} <b>{name}</b> · ID <code>{user_id}</code>\n"
            f"   Всего: <b>{total}</b> · Принято: {accepted} · "
            f"Отклонено: {rejected} · Блокировок: {blocked}"
        )
    if not lines:
        lines.append("Пока нет обработанных заявок для рейтинга.")

    periods = publication_window_starts()
    published = db.published_counts(**periods)
    lines.append(
        "📊 <b>Опубликовано постов</b>\n"
        f"Сегодня: <b>{published['today']}</b>\n"
        f"За последние 7 дней: <b>{published['week']}</b>\n"
        f"За месяц: <b>{published['month']}</b>"
    )
    await message.answer("🏆 <b>Топ модераторов</b>\n\n" + "\n\n".join(lines))


@router.message(Command("setdeleteprice"))
async def set_delete_price_command(message: Message, bot: Bot) -> None:
    if not message.from_user or not await is_moderation_admin(bot, message.from_user.id):
        await message.answer("Команда доступна только администраторам группы модерации.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        await message.answer(
            f"Текущая цена: <b>{removal_price_rub()} ₽</b>\n"
            "Чтобы изменить: <code>/setdeleteprice 150</code>"
        )
        return
    amount = int(parts[1].strip())
    if amount < 1 or amount > 100000:
        await message.answer("Цена должна быть от 1 до 100000 рублей.")
        return
    db.set_setting("post_removal_price_rub", str(amount))
    await message.answer(f"{ce('check')} Цена удаления поста изменена: <b>{amount} ₽</b>.")


@router.message(Command("setinfoprice"))
async def set_info_price_command(message: Message, bot: Bot) -> None:
    if not message.from_user or not await is_moderation_admin(bot, message.from_user.id):
        await message.answer("Команда доступна только администраторам группы модерации.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        await message.answer(
            f"Текущая цена сведений об авторе: <b>{author_info_price_rub()} ₽</b>\n"
            "Чтобы изменить: <code>/setinfoprice 150</code>"
        )
        return
    amount = int(parts[1].strip())
    if amount < 1 or amount > 100000:
        await message.answer("Цена должна быть от 1 до 100000 рублей.")
        return
    db.set_setting("post_author_info_price_rub", str(amount))
    await message.answer(f"{ce('check')} Цена сведений об авторе: <b>{amount} ₽</b>.")


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


@router.message(Command("cleanbuttons"), F.chat.type.in_({"group", "supergroup"}))
async def cleanbuttons_command(message: Message, bot: Bot) -> None:
    if message.chat.id != moderation_chat_id():
        return
    parts = (message.text or "").split(maxsplit=1)
    if message.reply_to_message:
        target = message.chat.id
        post_id = message.reply_to_message.message_id
    elif len(parts) == 2 and parts[1].strip().isdigit():
        target = destination_chat_id()
        if target is None:
            await message.answer("Не настроен канал публикации. Проверь DESTINATION_CHAT_ID.")
            return
        post_id = int(parts[1].strip())
    else:
        await message.answer(
            "Чтобы убрать кнопку с сообщения в этой группе, ответь на него командой /cleanbuttons.\n"
            "Для поста в канале используй <code>/cleanbuttons POST_ID</code>."
        )
        return
    try:
        await safe_edit_reply_markup(bot, target, post_id, None)
    except TelegramAPIError as exc:
        logger.warning("Could not remove old channel keyboard from post %s: %s", post_id, exc)
        await message.answer(
            f"Не получилось убрать кнопки у поста <code>{post_id}</code>. "
            "Проверь ID и права бота администратора в канале."
        )
        return
    await message.answer(f"{ce('check')} Кнопки у поста <code>{post_id}</code> убраны.")


@router.message(F.chat.type == "private")
async def inbox(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    if await consume_admin_session(message, bot):
        return
    sender_id = message.from_user.id
    if not message.from_user.is_bot:
        db.register_user(
            sender_id,
            message.from_user.username,
            message.from_user.full_name,
        )

    requested = (message.text or "").strip()
    if requested in POST_ACTION_BUTTONS and getattr(message, "forward_origin", None) is None:
        action = POST_ACTION_BUTTONS[requested]
        db.set_post_action(sender_id, action)
        if action == "post_removal":
            await message.answer(
                "Пришлите ссылку или перешлите публикацию из канала для удаления.\n"
                "Пример: <code>https://t.me/c/1234567890/456</code>",
                reply_markup=POST_ACTION_KEYBOARD,
            )
            return
        await message.answer(
            "Пришлите ссылку или перешлите публикацию из канала.\n"
            "Пример: <code>https://t.me/c/1234567890/456</code>",
            reply_markup=POST_ACTION_KEYBOARD,
        )
        return

    waiting = db.get_post_action(sender_id)
    if waiting:
        if getattr(message, "forward_origin", None) is not None or requested.startswith(("https://t.me/", "https://telegram.me/", "https://www.t.me/")):
            success = await process_post_link(message, bot, waiting)
            if success:
                db.clear_post_action(sender_id)
            return
        # A user may change their mind after selecting a paid action. Do not trap
        # ordinary text or media in link-entry mode: send it to moderation normally.
        db.clear_post_action(sender_id)

    if await offer_forwarded_post_actions(message, bot):
        return

    if requested.startswith(("https://t.me/", "https://telegram.me/", "https://www.t.me/")):
        await message.answer(
            "Нажмите «Удалить пост» или «Узнать, кто выложил», затем отправьте ссылку или перешлите пост.",
            reply_markup=POST_ACTION_KEYBOARD,
        )
        return

    if await is_blocked_from_suggestions(bot, sender_id):
        await safe_answer(
            message,
            f"{ce('ban')} Доступ к отправке предложений заблокирован.",
            reply_markup=POST_ACTION_KEYBOARD,
        )
        return

    if message.text and message.text.strip().casefold() == "связаться с владельцем":
        await safe_answer(
            message,
            "Старая кнопка удалена. Отправляй предложение прямо сюда.",
            reply_markup=ReplyKeyboardRemove(remove_keyboard=True),
        )
        return

    if message.text and message.text.startswith("/"):
        await safe_answer(
            message,
            "Нажми /start, чтобы начать, или отправь текст/медиа без команды.",
            reply_markup=ReplyKeyboardRemove(remove_keyboard=True),
        )
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
            content_fingerprint=content_identity(message),
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


def replied_moderation_message_id(message: Message) -> int | None:
    """Extract target message ID from normal and cross-topic Telegram replies."""
    original = getattr(message, "reply_to_message", None)
    if original is not None:
        return int(original.message_id)
    external = getattr(message, "external_reply", None)
    external_chat = getattr(external, "chat", None)
    if (
        external is not None
        and external_chat is not None
        and same_chat(getattr(external_chat, "id", None), message.chat.id)
        and getattr(external, "message_id", None) is not None
    ):
        return int(external.message_id)
    return None


def submission_for_reply_target(replied_id: int, replied_to_message=None):
    """Resolve a suggestion even if its older message-ID mapping is missing."""
    sub = db.submission_for_moderation_message(replied_id)
    if sub:
        return sub
    markup = getattr(replied_to_message, "reply_markup", None)
    if markup is None:
        return None
    for row in getattr(markup, "inline_keyboard", ()) or ():
        for button in row:
            data = getattr(button, "callback_data", "") or ""
            parts = data.split(":")
            if len(parts) == 3 and parts[0] == "mod" and parts[2].isdigit():
                found = db.get(int(parts[2]))
                if found:
                    logger.info("Recovered suggestion %s from reply keyboard", found["id"])
                    return found
    return None


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def group_reply_handler(message: Message, bot: Bot) -> None:
    """Send a swipe reply on a moderation post to its author without any button."""
    if await consume_admin_session(message, bot):
        return
    if not same_chat(message.chat.id, moderation_chat_id()):
        return

    # Telegram represents anonymous group admins with a synthetic bot user.
    # They should be able to reply too, but ignore actual other bots to avoid loops.
    sender = getattr(message, "from_user", None)
    sender_chat = getattr(message, "sender_chat", None)
    anonymous_admin = (
        sender_chat is not None
        and same_chat(getattr(sender_chat, "id", None), message.chat.id)
    )
    if (
        not anonymous_admin
        and (not sender or getattr(sender, "is_bot", False))
    ) or getattr(message, "is_automatic_forward", False):
        return

    incoming_text = getattr(message, "text", None)
    replied_id = replied_moderation_message_id(message)
    if replied_id is None or (incoming_text and incoming_text.startswith("/")):
        return

    moderator_id = getattr(sender, "id", 0) if not anonymous_admin else 0
    session = reply_sessions.get(moderator_id) if moderator_id else None
    reply_target = None
    used_session = False

    if session:
        prompt_id = int(session["prompt_message_id"])
        active_sub = db.get(int(session["sub_id"]))
        session_message_ids = {prompt_id}
        if active_sub and active_sub["admin_card_id"]:
            session_message_ids.add(int(active_sub["admin_card_id"]))
        if active_sub:
            session_message_ids.update(db.moderation_message_ids(int(active_sub["id"])))
        if replied_id in session_message_ids:
            if asyncio.get_running_loop().time() >= float(session["deadline"]):
                clear_reply_session(moderator_id)
                await edit_reply_prompt(
                    bot, message.chat.id, prompt_id,
                    f"{ce('timer')} <b>Время ответа истекло.</b>",
                )
                if replied_id == prompt_id:
                    return
            else:
                reply_target = session
                used_session = True

    if reply_target is None:
        sub = submission_for_reply_target(
            replied_id, getattr(message, "reply_to_message", None)
        )
        if not sub:
            logger.info(
                "Cannot route moderator reply: chat=%s reply_to=%s moderator=%s",
                message.chat.id, replied_id, moderator_id,
            )
            # Show an actionable error only if a user replied to this bot's
            # message; unrelated group conversations should be left alone.
            original_sender = getattr(
                getattr(message, "reply_to_message", None), "from_user", None
            )
            if original_sender and getattr(original_sender, "id", None) == getattr(bot, "id", None):
                await safe_answer(
                    message,
                    f"{ce('warning')} Не нашёл заявку для этого сообщения. "
                    "Попробуй ответить на сам пост с кнопками или отправь новую заявку.",
                )
            return
        reply_target = {"sub_id": int(sub["id"]), "user_id": int(sub["user_id"])}

    try:
        await deliver_group_reply(bot, message, reply_target)
    except TelegramAPIError as exc:
        logger.warning(
            "Could not send reply from moderator %s to user %s for submission %s: %s",
            moderator_id, reply_target["user_id"], reply_target["sub_id"], exc,
        )
        await safe_answer(
            message,
            f"{ce('warning')} Ответ пользователю не доставлен: "
            f"{html.escape(str(exc))[:250]}. Попробуй ещё раз.",
        )
        return

    if used_session:
        old_session = clear_reply_session(moderator_id)
        if old_session:
            await edit_reply_prompt(
                bot, message.chat.id, int(old_session["prompt_message_id"]),
                f"{ce('check')} <b>Ответ отправлен пользователю.</b>",
            )
    await safe_answer(message, f"{ce('check')} Ответ отправлен пользователю.")

async def update_moderation_card(bot: Bot, sub, status: str) -> None:
    """Update status while keeping the reply action available after a decision."""
    target = moderation_chat_id()
    card_id = sub["admin_card_id"]
    if target is None or not card_id:
        return

    submission_id = int(sub["id"])
    try:
        card_mode = str(sub["admin_card_mode"] or "content")
    except (KeyError, IndexError):
        card_mode = "content"

    markup = buttons(submission_id) if status == "pending" else status_buttons(submission_id, status)
    updated = False

    if card_mode in {"control", "album_control"}:
        preview = html.escape(str(sub["quote_text"] or "[Сообщение]")[:700])
        text = (
            f"{moderation_footer(sub, status, html_safe=True)}\n\n"
            f"{ce('info')} Контент заявки: {preview}"
        )
        try:
            await safe_bot_edit_message_text(
                bot,
                chat_id=target,
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
                        chat_id=target,
                        message_id=int(card_id),
                        text=combined,
                        entities=entities or None,
                        parse_mode=None,
                        reply_markup=markup,
                    )
                else:
                    await bot.edit_message_caption(
                        chat_id=target,
                        message_id=int(card_id),
                        caption=combined,
                        caption_entities=entities or None,
                        parse_mode=None,
                        reply_markup=markup,
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
                            chat_id=target,
                            message_id=int(card_id),
                            text=strip_custom_emoji_markup(combined),
                            entities=fallback_entities or None,
                            parse_mode=None,
                            reply_markup=markup,
                        )
                    else:
                        await bot.edit_message_caption(
                            chat_id=target,
                            message_id=int(card_id),
                            caption=strip_custom_emoji_markup(combined),
                            caption_entities=fallback_entities or None,
                            parse_mode=None,
                            reply_markup=markup,
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
                chat_id=target,
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


@router.callback_query(F.data.startswith("users:"))
async def users_pagination_callback(callback: CallbackQuery, bot: Bot) -> None:
    message = callback.message
    if not message or not (
        message.chat.id == moderation_chat_id()
        or (
            message.chat.type == "private"
            and await is_moderation_admin(bot, callback.from_user.id)
        )
    ):
        await callback.answer("Список доступен только участникам группы модерации.", show_alert=True)
        return
    try:
        _, action, raw_offset = (callback.data or "").split(":")
        if action != "page":
            await callback.answer()
            return
        offset = max(0, int(raw_offset))
    except (ValueError, TypeError):
        await callback.answer("Некорректная страница.", show_alert=True)
        return
    total = db.user_count()
    offset = min(offset, max(0, ((total - 1) // USERS_PAGE_SIZE) * USERS_PAGE_SIZE))
    try:
        await safe_edit_text(
            message,
            render_users_page(offset),
            reply_markup=users_page_keyboard(offset, total),
        )
    except TelegramAPIError as exc:
        logger.warning("Could not update users page: %s", exc)
        await callback.answer("Не удалось обновить страницу.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("broadcast:"))
async def broadcast_callback(callback: CallbackQuery, bot: Bot) -> None:
    message = callback.message
    if not message or message.chat.id != moderation_chat_id():
        await callback.answer("Рассылка доступна только в группе модерации.", show_alert=True)
        return
    if not await is_moderation_admin(bot, callback.from_user.id):
        await callback.answer("Только для администраторов группы.", show_alert=True)
        return
    session = broadcast_sessions.get(callback.from_user.id)
    if not session or int(session.get("confirm_message_id", 0)) != message.message_id:
        await callback.answer("Эта рассылка уже завершена или устарела.", show_alert=True)
        return

    action = (callback.data or "").split(":", maxsplit=1)[-1]
    if action == "cancel":
        broadcast_sessions.pop(callback.from_user.id, None)
        await callback.answer("Рассылка отменена.")
        await safe_edit_text(
            message,
            "❌ <b>Рассылка отменена.</b>",
            reply_markup=None,
        )
        return
    if action != "confirm":
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    if session.get("mode") == "channel":
        target = destination_chat_id()
        if target is None or same_chat(target, moderation_chat_id()):
            await callback.answer("Канал публикации не настроен.", show_alert=True)
            return
        try:
            await safe_bot_send_message(
                bot, target, html.escape(str(session.get("text", "")))
            )
        except TelegramAPIError as exc:
            logger.warning("Channel announcement failed for %s: %s", target, exc)
            await callback.answer("Не получилось отправить объявление. Проверь права бота.", show_alert=True)
            return

        broadcast_sessions.pop(callback.from_user.id, None)
        await callback.answer("Объявление опубликовано.")
        try:
            await safe_edit_text(message, "✅ <b>Объявление опубликовано в канале.</b>", reply_markup=None)
        except TelegramAPIError:
            await safe_answer(message, "Объявление опубликовано в канале.")
        return

    broadcast_sessions.pop(callback.from_user.id, None)
    await callback.answer("Рассылка запущена.")
    recipients = db.all_user_ids()
    sent = 0
    failed = 0
    mode = str(session.get("mode", ""))
    for user_id in recipients:
        try:
            if mode == "copy":
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=int(session["source_chat_id"]),
                    message_id=int(session["source_message_id"]),
                )
            else:
                await safe_bot_send_message(
                    bot,
                    user_id,
                    html.escape(str(session.get("text", ""))),
                )
            sent += 1
        except TelegramAPIError as exc:
            failed += 1
            logger.debug("Broadcast delivery failed for user %s: %s", user_id, exc)
        await asyncio.sleep(0.04)

    logger.info(
        "Broadcast finished: admin=%s sent=%s failed=%s total=%s",
        callback.from_user.id, sent, failed, len(recipients),
    )
    result = (
        "📣 <b>Рассылка завершена</b>\n"
        f"Получателей в базе: <b>{len(recipients)}</b>\n"
        f"Доставлено: <b>{sent}</b>\n"
        f"Не удалось отправить: <b>{failed}</b>"
    )
    try:
        await safe_edit_text(message, result, reply_markup=None)
    except TelegramAPIError:
        await safe_answer(message, result)


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
    allowed_message_ids = set(db.moderation_message_ids(sub_id))
    if sub["admin_card_id"]:
        allowed_message_ids.add(int(sub["admin_card_id"]))
    if message.message_id not in allowed_message_ids:
        await callback.answer("Эта кнопка не относится к карточке заявки.", show_alert=True)
        return
    if action in CHANNEL_PROMOS:
        if not await is_moderation_admin(bot, callback.from_user.id):
            await callback.answer("Отправлять объявления могут только администраторы.", show_alert=True)
            return
        target = destination_chat_id()
        if target is None or same_chat(target, moderation_chat_id()):
            await callback.answer("Настрой отдельный DESTINATION_CHAT_ID для канала.", show_alert=True)
            return
        announcement = CHANNEL_PROMOS[action]
        try:
            confirmation = await safe_bot_send_message(
                bot,
                chat_id=message.chat.id,
                text=(
                    "<b>Опубликовать объявление в канале?</b>\n\n"
                    f"<blockquote>{html.escape(announcement)}</blockquote>\n\n"
                    "Подтверди отправку. Сообщение появится в канале для его подписчиков."
                ),
                reply_markup=broadcast_confirmation_keyboard(channel=True),
                reply_parameters=ReplyParameters(
                    message_id=message.message_id,
                    allow_sending_without_reply=True,
                ),
            )
        except TelegramAPIError as exc:
            logger.warning("Could not create announcement confirmation: %s", exc)
            await callback.answer("Не удалось создать подтверждение.", show_alert=True)
            return
        broadcast_sessions[callback.from_user.id] = {
            "mode": "channel",
            "text": announcement,
            "confirm_message_id": confirmation.message_id,
            "admin_id": callback.from_user.id,
        }
        await callback.answer("Подтверди публикацию.")
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

    # Replying is independent of the publication/rejection decision.
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

    # Only the decision actions require a pending suggestion.
    if sub["status"] != "pending":
        await callback.answer("Заявка уже обработана.")
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
            removal_token = db.ensure_removal_token(sub_id)
            copied_ids, footer_id = await copy_submission(
                bot, target, sub_id,
                removal_url=removal_deep_link(sub_id, removal_token),
            )
            published_ids = copied_ids + ([footer_id] if footer_id else [])
            db.set_publication(sub_id, target, published_ids)
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
        membership = await moderation_group_membership(bot, int(sub["user_id"]))
        if membership is True:
            await callback.answer(
                "Нельзя заблокировать участника группы модерации.",
                show_alert=True,
            )
            return
        if membership is None:
            await callback.answer(
                "Не удалось проверить членство пользователя в группе. Бан отменён; "
                "проверь, что бот может проверять участников группы.",
                show_alert=True,
            )
            return
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
    """Restore action keyboards on pending and processed submissions."""
    target = moderation_chat_id()
    if target is None:
        return

    # Refresh processed cards once to expose the new channel-announcement buttons.
    db.prepare_keyboard_cleanup_version("6")
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
        card_mode = str(sub["admin_card_mode"] or "content")
        is_album = len(db.items_for_submission(submission_id)) > 1

        # Migrate older pending albums: create the reliable control card once even if
        # Telegram reports that some item-level keyboard edits succeeded.
        if status == "pending" and is_album and card_mode not in {"control", "album_control"}:
            try:
                preview = html.escape(str(sub["quote_text"] or "[Сообщение]")[:700])
                control_text = (
                    f"{moderation_footer(sub, 'pending', html_safe=True)}\n\n"
                    f"{ce('info')} Контент заявки: {preview}"
                )
                control_card = await safe_bot_send_message(
                    bot,
                    chat_id=target,
                    text=control_text,
                    reply_markup=buttons(submission_id),
                    reply_parameters=ReplyParameters(
                        message_id=int(sub["admin_card_id"]),
                        allow_sending_without_reply=True,
                    ),
                )
                if not db.moderation_message_ids(submission_id):
                    db.remember_primary_moderation_message_id(
                        submission_id, int(sub["admin_card_id"])
                    )
                db.set_card_pending(submission_id, control_card.message_id, "album_control")
                sub = db.get(submission_id) or sub
            except TelegramAPIError as exc:
                logger.warning(
                    "Could not migrate pending album %s to a control card: %s",
                    submission_id, exc,
                )

        success = await refresh_submission_keyboards(bot, sub, status)
        if (
            not success
            and status == "pending"
            and str(sub["admin_card_mode"] or "content") not in {"control", "album_control"}
        ):
            try:
                preview = html.escape(str(sub["quote_text"] or "[Сообщение]")[:700])
                control_text = (
                    f"{moderation_footer(sub, 'pending', html_safe=True)}\n\n"
                    f"{ce('info')} Контент заявки: {preview}"
                )
                control_card = await safe_bot_send_message(
                    bot,
                    chat_id=target,
                    text=control_text,
                    reply_markup=buttons(submission_id),
                    reply_parameters=ReplyParameters(
                        message_id=int(sub["admin_card_id"]),
                        allow_sending_without_reply=True,
                    ),
                )
                if not db.moderation_message_ids(submission_id):
                    db.remember_primary_moderation_message_id(
                        submission_id, int(sub["admin_card_id"])
                    )
                db.set_card_pending(submission_id, control_card.message_id, "control")
                refreshed_sub = db.get(submission_id)
                if refreshed_sub:
                    success = await refresh_submission_keyboards(bot, refreshed_sub, "pending")
            except TelegramAPIError as exc:
                logger.warning(
                    "Could not create fallback control card for old submission %s: %s",
                    submission_id, exc,
                )
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


def restart_notice() -> str:
    """Service notice shown to moderators after a successful bot restart."""
    magnitogorsk_time = datetime.now(ZoneInfo("Asia/Yekaterinburg")).strftime("%d.%m.%Y %H:%M")
    return (
        f"{ce('check')} <b>Бот перезапущен</b>\n"
        f"{ce('timer')} Магнитогорск: <b>{magnitogorsk_time}</b>\n\n"
        "Позор МГН крутой бот, йоу"
    )


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
        logger.info(
            "Database diagnostics: DB_PATH=%s, registered_users=%s, users_with_submissions=%s",
            DB_PATH,
            db.user_count(),
            db.known_submission_user_count(),
        )
        try:
            startup_backup = db.backup(keep=DB_BACKUP_KEEP)
            if startup_backup:
                logger.info("Startup database backup created: %s", startup_backup)
        except (OSError, sqlite3.DatabaseError) as exc:
            logger.error("Startup database backup failed: %s", exc)
        try:
            await safe_bot_send_message(bot, moderation_chat_id(), restart_notice())
        except TelegramAPIError as exc:
            logger.warning("Could not send restart notice to the moderation group: %s", exc)
        await cleanup_processed_inline_keyboards(bot)
        recovery_job = asyncio.create_task(recover(bot))
        removal_reconcile_job = asyncio.create_task(reconcile_removal_payments(bot))
        database_backup_job = asyncio.create_task(database_backup_loop())
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if "recovery_job" in locals():
            recovery_job.cancel()
        if "removal_reconcile_job" in locals():
            removal_reconcile_job.cancel()
        if "database_backup_job" in locals():
            database_backup_job.cancel()
        for job in album_jobs.values():
            job.cancel()
        for job in reply_jobs.values():
            job.cancel()
        db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
