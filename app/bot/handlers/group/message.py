import asyncio
import contextlib
import logging
import html
from typing import Optional

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import MagicData
from aiogram.types import Message
from aiogram.utils.markdown import hlink

from app.bot.manager import Manager
from app.bot.types.album import Album
from app.bot.utils.redis import RedisStorage
from app.bot.utils.create_forum_topic import (
    update_forum_topic_icon_cached,
    update_forum_topic_icon_state_cached,
)
from app.bot.db.dialogue_store import dialogue_store, utc_now_iso
from app.bot.utils.reactions import ack_message, pin_message_safe

logger = logging.getLogger(__name__)

router = Router()
router.message.filter(
    MagicData(F.event_chat.id == F.config.bot.GROUP_ID),  # type: ignore
    F.chat.type.in_(["group", "supergroup"]),
    F.message_thread_id.is_not(None),
)

SENDER_FILTER = F.from_user[F.is_bot.is_(False)] | F.sender_chat.is_not(None)


def _is_command_message(message: Message) -> bool:
    """Return True if the message starts with a bot command entity."""
    if not message.text:
        return False
    if not message.entities:
        return message.text.lstrip().startswith("/")
    for entity in message.entities:
        if entity.type == "bot_command" and entity.offset == 0:
            return True
    return message.text.lstrip().startswith("/")


@router.message(F.forum_topic_created)
async def handle_forum_topic_created(message: Message, manager: Manager, redis: RedisStorage) -> None:
    """Handle the system event fired when a new forum topic is created for a user.

    Sends a pinned info card inside the topic containing the user's basic details
    and the list of available support commands.
    """
    await asyncio.sleep(2)

    logger.info(
        "forum_topic_created | chat=%s thread_id=%s",
        message.chat.id, message.message_thread_id,
    )

    if not message.message_thread_id:
        return

    user_data = await redis.get_by_message_thread_id(message.message_thread_id)
    if not user_data:
        logger.warning(
            "forum_topic_created but no user_data | thread_id=%s",
            message.message_thread_id,
        )
        return

    if user_data.language_code:
        manager.text_message.language_code = user_data.language_code
    else:
        manager.text_message.language_code = manager.config.bot.DEFAULT_LANGUAGE

    if (
        not manager.config.bot.TOPIC_STATUS_IN_TITLE
        and manager.config.bot.START_EMOJI_ID
        and user_data.state != "kicked"
    ):
        should_set_start_icon = (not user_data.last_activity_at) and (
            user_data.topic_icon_state in (None, "start")
        )
        if should_set_start_icon:
            try:
                await message.bot.edit_forum_topic(
                    chat_id=manager.config.bot.GROUP_ID,
                    message_thread_id=message.message_thread_id,
                    icon_custom_emoji_id=manager.config.bot.START_EMOJI_ID,
                )
                if user_data.topic_icon_state != "start":
                    user_data.topic_icon_state = "start"
                    with contextlib.suppress(Exception):
                        await redis.update_user(user_data.id, user_data)
            except (TelegramBadRequest, TelegramRetryAfter, TelegramAPIError) as ex:
                logger.warning(
                    "Failed to set START icon | thread_id=%s err=%s",
                    message.message_thread_id, ex,
                )

    tg_id = user_data.id
    is_ru = manager.text_message.language_code == "ru"

    if user_data.username and user_data.username != "-":
        user_url = f"https://t.me/{user_data.username.lstrip('@')}"
    else:
        user_url = f"tg://user?id={tg_id}"

    name_with_link = hlink(user_data.full_name, user_url)
    username_display = (
        f"@{user_data.username.lstrip('@')}"
        if user_data.username and user_data.username != "-"
        else ("No username" if not is_ru else "Нет юзернейма")
    )

    tg_id_line = f"<code>{tg_id}</code>"
    commands_block = manager.text_message.get("support_commands")

    text_template = manager.text_message.get("user_started_bot")
    formatted_text = text_template.format(
        name=name_with_link,
        tg_id=tg_id,
        tg_id_line=tg_id_line,
        username=html.escape(username_display),
        commands_block=commands_block,
    )

    msg = await message.bot.send_message(
        chat_id=manager.config.bot.GROUP_ID,
        message_thread_id=message.message_thread_id,
        text=formatted_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        disable_notification=True,
    )

    pinned = await pin_message_safe(msg)
    if not pinned:
        logger.warning(
            "Failed to pin start message | chat=%s thread_id=%s message_id=%s",
            message.chat.id, message.message_thread_id, msg.message_id,
        )

    try:
        fresh = await redis.get_user(user_data.id)
        target = fresh or user_data
        target.topic_start_message_id = msg.message_id
        await redis.update_user(target.id, target)
    except Exception:
        logger.exception("Failed to persist topic_start_message_id | user=%s", tg_id)


@router.message(F.pinned_message | F.forum_topic_edited | F.forum_topic_closed | F.forum_topic_reopened)
async def handle_service_message(message: Message) -> None:
    """Delete Telegram service messages that clutter the topic (pin notices, topic edits, etc.)."""
    await message.delete()


@router.message(F.media_group_id, SENDER_FILTER)
@router.message(F.media_group_id.is_(None), SENDER_FILTER)
async def handle_manager_message(
    message: Message,
    manager: Manager,
    redis: RedisStorage,
    album: Optional[Album] = None,
) -> None:
    """Forward a manager's message (or album) from the support topic to the corresponding user.

    Skips the message if:
    - the sender is a bot;
    - the message is a bot command;
    - silent mode is active for this topic.

    Also updates the topic icon to reflect the "manager replied" state and
    stores the message in the local dialogue archive.
    """
    if message.from_user and message.from_user.is_bot:
        return

    if _is_command_message(message):
        return

    user_data = await redis.get_by_message_thread_id(message.message_thread_id)
    if not user_data:
        return

    if user_data.message_silent_mode:
        return

    user_data.last_activity_at = utc_now_iso()
    await redis.update_user(user_data.id, user_data)

    try:
        if album:
            caption = album.caption or (album.messages[0].caption if album.messages else "") or ""
            await dialogue_store.add(
                user_id=user_data.id,
                thread_id=message.message_thread_id,
                direction="manager",
                kind="album",
                text=f"[album:{len(album.messages)}] {caption}".strip(),
                author_tg_id=message.from_user.id if message.from_user else None,
                author_username=message.from_user.username if message.from_user else None,
            )
        else:
            if message.text:
                kind, text_to_store = "text", message.text
            else:
                media_type = (
                    "photo" if message.photo else
                    "video" if message.video else
                    "document" if message.document else
                    "audio" if message.audio else
                    "voice" if message.voice else
                    "video_note" if message.video_note else
                    "unknown"
                )
                kind = "media"
                text_to_store = f"[{media_type}] {message.caption or ''}".strip()
            await dialogue_store.add(
                user_id=user_data.id,
                thread_id=message.message_thread_id,
                direction="manager",
                kind=kind,
                text=text_to_store,
                author_tg_id=message.from_user.id if message.from_user else None,
                author_username=message.from_user.username if message.from_user else None,
            )
    except Exception as ex:
        logger.warning("Failed to store manager dialogue | user=%s err=%s", user_data.id, ex)

    delivered = False
    fallback_fail_text = manager.text_message.get("message_not_sent")

    try:
        if not album:
            await message.copy_to(chat_id=user_data.id)
        else:
            await album.copy_to(chat_id=user_data.id)
        delivered = True
    except TelegramAPIError as ex:
        if "blocked" in str(ex).lower():
            fallback_fail_text = manager.text_message.get("blocked_by_user")
    except Exception:
        pass

    if user_data.state == "kicked" and manager.config.bot.BAN_EMOJI_ID:
        await update_forum_topic_icon_state_cached(
            bot=message.bot,
            redis=redis,
            config=manager.config,
            user_data=user_data,
            message_thread_id=message.message_thread_id,
            desired_state="ban",
        )
    else:
        await update_forum_topic_icon_cached(
            bot=message.bot,
            redis=redis,
            config=manager.config,
            user_data=user_data,
            message_thread_id=message.message_thread_id,
            is_manager_response=True,
        )

    await ack_message(
        message,
        ok=delivered,
        fallback_ok_text=manager.text_message.get("message_sent_to_user"),
        fallback_fail_text=fallback_fail_text,
        disable_notification=True,
    )
