import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from app.config import Config
from app.bot.utils.redis import RedisStorage

logger = logging.getLogger(__name__)

ARCHIVE_TOPIC_KEY = "meta:archive_topic_thread_id"
ARCHIVE_TOPIC_NAME = "archive"
LOGS_TOPIC_KEY = "meta:logs_topic_thread_id"
LOGS_TOPIC_NAME = "logs"

async def get_archive_thread_id(redis: RedisStorage) -> int | None:
    stored = await redis.get_value(ARCHIVE_TOPIC_KEY)
    if not stored:
        return None
    try:
        return int(stored)
    except ValueError:
        return None


async def get_logs_thread_id(redis: RedisStorage) -> int | None:
    stored = await redis.get_value(LOGS_TOPIC_KEY)
    if not stored:
        return None
    try:
        return int(stored)
    except ValueError:
        return None


async def ensure_archive_topic(
    *,
    bot: Bot,
    config: Config,
    redis: RedisStorage,
) -> int | None:
    """
    Ensure a dedicated 'archive' forum topic exists and store its thread_id in Redis.
    Telegram API does not allow listing topics, so we persist thread_id ourselves.
    """
    stored = await redis.get_value(ARCHIVE_TOPIC_KEY)
    if stored:
        try:
            thread_id = int(stored)
        except ValueError:
            thread_id = None
        else:
            try:
                await bot.edit_forum_topic(
                    chat_id=config.bot.GROUP_ID,
                    message_thread_id=thread_id,
                    name=ARCHIVE_TOPIC_NAME,
                )
                logger.info("archive topic ready | thread_id=%s", thread_id)
                return thread_id
            except TelegramBadRequest as ex:
                msg = ex.message.lower()
                if "topic_not_modified" in msg:
                    logger.info("archive topic exists (noop) | thread_id=%s", thread_id)
                    return thread_id
                if "topic_id_invalid" in msg or "message thread not found" in msg:
                    logger.info("archive topic invalid -> recreate | old_thread_id=%s", thread_id)
                else:
                    logger.warning("archive topic verify failed | thread_id=%s err=%s", thread_id, ex.message)

    try:
        forum_topic = await bot.create_forum_topic(
            chat_id=config.bot.GROUP_ID,
            name=ARCHIVE_TOPIC_NAME,
            request_timeout=30,
        )
        thread_id = forum_topic.message_thread_id
        await redis.set_value(ARCHIVE_TOPIC_KEY, str(thread_id))
        logger.info("archive topic created | thread_id=%s", thread_id)
        return thread_id
    except TelegramForbiddenError as ex:
        logger.warning("no rights to create archive topic | err=%s", ex.message)
        return None
    except TelegramBadRequest as ex:
        logger.warning("failed to create archive topic | err=%s", ex.message)
        return None


async def ensure_logs_topic(
    *,
    bot: Bot,
    config: Config,
    redis: RedisStorage,
) -> int | None:
    """
    Ensure a dedicated 'logs' forum topic exists and store its thread_id in Redis.

    This topic is intended for maintenance/system logs. Keep the 'archive' topic for dialogue transcripts only.
    """
    stored = await redis.get_value(LOGS_TOPIC_KEY)
    if stored:
        try:
            thread_id = int(stored)
        except ValueError:
            thread_id = None
        else:
            try:
                await bot.edit_forum_topic(
                    chat_id=config.bot.GROUP_ID,
                    message_thread_id=thread_id,
                    name=LOGS_TOPIC_NAME,
                )
                logger.info("logs topic ready | thread_id=%s", thread_id)
                return thread_id
            except TelegramBadRequest as ex:
                msg = ex.message.lower()
                if "topic_not_modified" in msg:
                    logger.info("logs topic exists (noop) | thread_id=%s", thread_id)
                    return thread_id
                if "topic_id_invalid" in msg or "message thread not found" in msg:
                    logger.info("logs topic invalid -> recreate | old_thread_id=%s", thread_id)
                else:
                    logger.warning("logs topic verify failed | thread_id=%s err=%s", thread_id, ex.message)

    try:
        forum_topic = await bot.create_forum_topic(
            chat_id=config.bot.GROUP_ID,
            name=LOGS_TOPIC_NAME,
            request_timeout=30,
        )
        thread_id = forum_topic.message_thread_id
        await redis.set_value(LOGS_TOPIC_KEY, str(thread_id))
        logger.info("logs topic created | thread_id=%s", thread_id)
        return thread_id
    except TelegramForbiddenError as ex:
        logger.warning("no rights to create logs topic | err=%s", ex.message)
        return None
    except TelegramBadRequest as ex:
        logger.warning("failed to create logs topic | err=%s", ex.message)
        return None


async def send_archive_log(
    *,
    bot: Bot,
    config: Config,
    redis: RedisStorage,
    text: str,
) -> int | None:
    # Historical name: keep for compatibility, but use the dedicated logs topic.
    thread_id = await get_logs_thread_id(redis)

    kwargs = {}
    if thread_id:
        kwargs["message_thread_id"] = thread_id

    try:
        await bot.send_message(chat_id=config.bot.GROUP_ID, text=text, **kwargs, disable_notification=True)
        return thread_id
    except TelegramBadRequest:
        # Fall back to group without thread.
        if kwargs:
            try:
                await bot.send_message(chat_id=config.bot.GROUP_ID, text=text, disable_notification=True)
            except TelegramRetryAfter as ex:
                logger.warning("archive log rate-limited (fallback) | retry_after=%s", ex.retry_after)
            except TelegramAPIError as ex:
                logger.warning("archive log failed (fallback) | err=%s", ex)
        return None
    except TelegramRetryAfter as ex:
        logger.warning("archive log rate-limited | retry_after=%s", ex.retry_after)
        return thread_id
    except TelegramAPIError as ex:
        logger.warning("archive log failed | err=%s", ex)
        return None
