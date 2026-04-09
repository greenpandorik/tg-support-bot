import asyncio
import logging
import contextlib
from typing import Optional

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.types import Message

from app.bot.manager import Manager
from app.bot.types.album import Album
from app.bot.utils.create_forum_topic import (
    build_topic_name,
    create_forum_topic,
    get_or_create_forum_topic,
    update_forum_topic_icon_cached,
)
from app.bot.utils.redis import RedisStorage
from app.bot.utils.redis.models import UserData
from app.bot.db.dialogue_store import dialogue_store, utc_now_iso

logger = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type == "private", StateFilter(None))


@router.edited_message()
async def handle_edited_message(message: Message, manager: Manager) -> None:
    """Notify the user that message editing is not supported — they should resend."""
    text = manager.text_message.get("message_edited")
    msg = await message.reply(text)
    await asyncio.sleep(5)
    await msg.delete()


@router.message(F.media_group_id)
@router.message(F.media_group_id.is_(None))
async def handle_incoming_message(
    message: Message,
    manager: Manager,
    redis: RedisStorage,
    user_data: UserData,
    album: Optional[Album] = None,
) -> None:
    """Forward an incoming user message (or media album) to the support forum topic.

    Flow:
    1. Silently drop the message if the user is banned.
    2. Get or create the forum topic for this user.
    3. Restore the dialogue archive if the topic was re-created after a ban.
    4. Copy the message to the topic.
    5. If the topic thread ID is stale (Telegram posted in General), recreate the topic and retry.
    6. Update the topic icon to the "user replied" state.
    7. Send a short confirmation to the user.
    """
    if user_data.is_banned:
        return

    user_data.last_activity_at = utc_now_iso()
    await redis.update_user(user_data.id, user_data)

    message_thread_id = await get_or_create_forum_topic(
        message.bot, redis, manager.config, user_data,
    )
    user_data.message_thread_id = message_thread_id

    try:
        if album:
            caption = album.caption or (album.messages[0].caption if album.messages else "") or ""
            await dialogue_store.add(
                user_id=user_data.id,
                thread_id=message_thread_id,
                direction="user",
                kind="album",
                text=f"[album:{len(album.messages)}] {caption}".strip(),
            )
        else:
            if message.text:
                kind, text = "text", message.text
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
                text = f"[{media_type}] {message.caption or ''}".strip()
            await dialogue_store.add(
                user_id=user_data.id,
                thread_id=message_thread_id,
                direction="user",
                kind=kind,
                text=text,
            )
    except Exception as ex:
        logger.warning("Failed to store user dialogue | user=%s err=%s", user_data.id, ex)

    async def _deliver(thread_id: int) -> tuple[bool, Optional[Message]]:
        """Attempt to copy the message/album into the forum topic.

        Returns (delivered, probe_message).
        probe_message is used to detect whether Telegram silently redirected
        the message to the General topic instead of the intended thread.
        """
        try:
            if not album:
                if message.text:
                    sent = await message.bot.send_message(
                        chat_id=manager.config.bot.GROUP_ID,
                        message_thread_id=thread_id,
                        text=message.text,
                        disable_web_page_preview=True,
                    )
                    return True, sent
                await message.copy_to(
                    chat_id=manager.config.bot.GROUP_ID,
                    message_thread_id=thread_id,
                )
                return True, None
            else:
                await album.copy_to(
                    chat_id=manager.config.bot.GROUP_ID,
                    message_thread_id=thread_id,
                )
                return True, None
        except TelegramBadRequest as ex:
            if "can't be forwarded" in str(ex).lower():
                text_fallback = manager.text_message.get("message_protected")
                sent = await message.bot.send_message(
                    chat_id=manager.config.bot.GROUP_ID,
                    message_thread_id=thread_id,
                    text=text_fallback,
                )
                return True, sent
            raise

    delivered = False
    try:
        # Try to restore dialogue archive if this topic was recreated after unban.
        try:
            if getattr(user_data, "archive_restore_pending", False):
                if await dialogue_store.has_any_messages(user_data.id):
                    title = f"📎 Архив диалога с {user_data.full_name} (последние 200 сообщений)"
                    await dialogue_store.send_recent_transcript(
                        bot=message.bot,
                        user_id=user_data.id,
                        chat_id=manager.config.bot.GROUP_ID,
                        message_thread_id=message_thread_id,
                        title=title,
                        limit=200,
                    )
                user_data.archive_restore_pending = False
                with contextlib.suppress(Exception):
                    await redis.update_user(user_data.id, user_data)
        except Exception as ex:
            logger.warning("Failed to restore archive | user=%s err=%s", user_data.id, ex)
            user_data.archive_restore_pending = False
            with contextlib.suppress(Exception):
                await redis.update_user(user_data.id, user_data)

        for attempt in range(1, 3):
            delivered, probe = await _deliver(message_thread_id)

            if (
                delivered
                and probe is not None
                and probe.message_thread_id != message_thread_id
            ):
                if attempt >= 2:
                    logger.error(
                        "Message posted outside topic after retry | user=%s expected=%s got=%s",
                        user_data.id, message_thread_id, probe.message_thread_id,
                    )
                    break

                logger.warning(
                    "Message posted outside topic, recreating | user=%s expected=%s got=%s",
                    user_data.id, message_thread_id, probe.message_thread_id,
                )
                with contextlib.suppress(Exception):
                    await message.bot.delete_message(
                        chat_id=manager.config.bot.GROUP_ID,
                        message_id=probe.message_id,
                    )

                topic_name = await build_topic_name(user_data, redis, config=manager.config)
                message_thread_id = await create_forum_topic(message.bot, manager.config, topic_name)
                user_data.message_thread_id = message_thread_id
                user_data.topic_created_at = utc_now_iso()
                user_data.topic_start_message_id = None
                user_data.topic_icon_state = "user"
                await redis.update_user(user_data.id, user_data)
                continue

            break

        if delivered and message_thread_id:
            await update_forum_topic_icon_cached(
                bot=message.bot,
                redis=redis,
                config=manager.config,
                user_data=user_data,
                message_thread_id=message_thread_id,
                is_manager_response=False,
            )

    except TelegramBadRequest as ex:
        if "message thread not found" in ex.message:
            topic_name = await build_topic_name(user_data, redis, config=manager.config)
            message_thread_id = await create_forum_topic(message.bot, manager.config, topic_name)
            user_data.message_thread_id = message_thread_id
            user_data.topic_created_at = utc_now_iso()
            user_data.topic_start_message_id = None
            user_data.topic_icon_state = "user"
            await redis.update_user(user_data.id, user_data)

            delivered, _ = await _deliver(message_thread_id)
            if delivered:
                await update_forum_topic_icon_cached(
                    bot=message.bot,
                    redis=redis,
                    config=manager.config,
                    user_data=user_data,
                    message_thread_id=message_thread_id,
                    is_manager_response=False,
                )
        else:
            raise

    try:
        text = (
            manager.text_message.get("message_sent")
            if delivered
            else manager.text_message.get("message_not_sent")
        )
        msg = await message.reply(text)
        await asyncio.sleep(5)
        await msg.delete()
    except Exception as ex:
        logger.warning("Failed to send confirmation | user=%s err=%s", user_data.id, ex)
