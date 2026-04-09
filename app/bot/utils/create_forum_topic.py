import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app.config import Config
from .exceptions import CreateForumTopicException, NotEnoughRightsException, NotAForumException
from .redis import RedisStorage
from .redis.models import UserData

logger = logging.getLogger(__name__)
TOPIC_NAME_MAX_LENGTH = 128
TOPIC_STATUS_SEPARATOR = " | "
TOPIC_ICON_DESIRED_TTL_SECONDS = 60 * 60
TOPIC_ICON_RETRY_LOCK_TTL_SECONDS = 3 * 60
TOPIC_ICON_RETRY_DELAYS_SECONDS: tuple[float, ...] = (5.0, 20.0, 60.0)


def _topic_icon_desired_key(message_thread_id: int) -> str:
    return f"meta:topic_icon_desired:{message_thread_id}"


def _topic_icon_retry_lock_key(message_thread_id: int) -> str:
    return f"lock:topic_icon_retry:{message_thread_id}"


async def _retry_topic_icon_update(
    *,
    bot: Bot,
    redis: RedisStorage,
    config: Config,
    user_id: int,
    message_thread_id: int,
    lock_key: str,
    lock_token: str,
) -> None:
    """
    Best-effort background retry for editForumTopic icon updates.
    Uses Redis lock to dedupe, and checks a Redis desired-state key to avoid stale updates.
    """
    try:
        desired_key = _topic_icon_desired_key(message_thread_id)

        for delay in TOPIC_ICON_RETRY_DELAYS_SECONDS:
            await asyncio.sleep(delay)

            try:
                desired_state = await redis.get_value(desired_key)
            except Exception:
                desired_state = None
            if not desired_state:
                return

            icon_id = _icon_id_for_state(config, desired_state)
            if not icon_id:
                return

            # Load user_data for persistence (best-effort).
            user_data = await redis.get_user(user_id)
            if user_data and user_data.message_thread_id and user_data.message_thread_id != message_thread_id:
                user_data = None
            if not user_data:
                user_data = await redis.get_by_message_thread_id(message_thread_id)
                if user_data and user_data.id != user_id:
                    logger.warning(
                        "Topic icon retry resolved to different user_id | expected_user=%s got_user=%s thread_id=%s",
                        user_id,
                        user_data.id,
                        message_thread_id,
                    )
                    user_data = None

            if user_data and user_data.topic_icon_state == desired_state:
                return

            ok = await update_forum_topic_icon(
                bot=bot,
                config=config,
                message_thread_id=message_thread_id,
                icon_custom_emoji_id=icon_id,
            )
            if ok:
                if user_data:
                    user_data.topic_icon_state = desired_state
                    with contextlib.suppress(Exception):
                        await redis.update_user(user_data.id, user_data)
                    logger.info(
                        "Topic icon updated (retry) | user=%s thread_id=%s state=%s",
                        user_data.id,
                        message_thread_id,
                        desired_state,
                    )
                return

        last_state = None
        with contextlib.suppress(Exception):
            last_state = await redis.get_value(desired_key)
        logger.warning(
            "Topic icon retry exhausted | user=%s thread_id=%s state=%s",
            user_id,
            message_thread_id,
            last_state or "unknown",
        )
    except Exception:
        logger.exception(
            "Unexpected error in topic icon retry | user=%s thread_id=%s state=%s",
            user_id,
            message_thread_id,
            "unknown",
        )
    finally:
        with contextlib.suppress(Exception):
            await redis.release_lock(lock_key, lock_token)


def _status_state_for_name(user_data: UserData, desired_state: str | None) -> str:
    if desired_state:
        return desired_state
    if user_data.topic_icon_state:
        return user_data.topic_icon_state
    if not user_data.last_activity_at:
        return "start"
    return "user"


def _status_emoji_for_state(config: Config, state: str) -> str | None:
    if state == "start":
        return config.bot.TOPIC_STATUS_EMOJI_START or None
    if state == "user":
        return config.bot.TOPIC_STATUS_EMOJI_USER or None
    if state == "manager":
        return config.bot.TOPIC_STATUS_EMOJI_MANAGER or None
    if state == "ban":
        return config.bot.TOPIC_STATUS_EMOJI_BAN or None
    return None


def _apply_status_prefix(
    *,
    base_name: str,
    user_data: UserData,
    config: Config,
    desired_state: str | None,
) -> str:
    if not config.bot.TOPIC_STATUS_IN_TITLE:
        return base_name[:TOPIC_NAME_MAX_LENGTH]

    state = _status_state_for_name(user_data, desired_state)
    emoji = _status_emoji_for_state(config, state)
    if not emoji:
        return base_name[:TOPIC_NAME_MAX_LENGTH]

    prefix = f"{emoji}{TOPIC_STATUS_SEPARATOR}"
    max_len = max(0, TOPIC_NAME_MAX_LENGTH - len(prefix))
    return f"{prefix}{base_name[:max_len]}"


async def build_topic_name(
    user_data: UserData,
    redis: RedisStorage,
    *,
    config: Config | None = None,
    desired_state: str | None = None,
) -> str:
    """Return the forum topic name for a user, optionally prefixed with a status emoji.

    When TOPIC_STATUS_IN_TITLE is enabled the name gets a status prefix such as
    "🆕 | John Doe".  Otherwise only the user's full name is used.
    """
    base_name = (user_data.full_name or "").strip() or str(user_data.id)
    if not config:
        return base_name[:TOPIC_NAME_MAX_LENGTH]
    return _apply_status_prefix(
        base_name=base_name,
        user_data=user_data,
        config=config,
        desired_state=desired_state,
    )


async def get_or_create_forum_topic(
    bot: Bot,
    redis: RedisStorage,
    config: Config,
    user_data: UserData,
    *,
    verify: bool = False,
) -> int:
    thread_id = user_data.message_thread_id

    # Fast path: don't call editForumTopic on every user message (rate-limited).
    # We verify topic existence only on explicit requests (e.g., /start, status events),
    # and rely on delivery errors / "General" fallback detection elsewhere.
    if thread_id and not verify:
        return thread_id

    topic_name = await build_topic_name(user_data, redis, config=config)

    if thread_id:
        try:
            logger.info("forum topic reuse check | user=%s thread_id=%s", user_data.id, thread_id)
            edit_kwargs = {
                "chat_id": config.bot.GROUP_ID,
                "message_thread_id": thread_id,
                "name": topic_name,  # noop
            }
            # If the topic is still "empty" (no activity), ensure it has START icon on verify.
            if (
                not config.bot.TOPIC_STATUS_IN_TITLE
                and verify
                and not user_data.last_activity_at
                and config.bot.START_EMOJI_ID
            ):
                edit_kwargs["icon_custom_emoji_id"] = config.bot.START_EMOJI_ID

            await bot.edit_forum_topic(**edit_kwargs)
            if "icon_custom_emoji_id" in edit_kwargs:
                user_data.topic_icon_state = "start"
                with contextlib.suppress(Exception):
                    await redis.update_user(user_data.id, user_data)
            logger.info("forum topic reused | user=%s thread_id=%s", user_data.id, thread_id)
            return thread_id
        except TelegramRetryAfter as ex:
            # Fail open: topic most likely exists; avoid blocking user message flow.
            logger.warning(
                "Rate limited on editForumTopic, skipping verify | user=%s thread_id=%s retry_after=%s",
                user_data.id,
                thread_id,
                ex.retry_after,
            )
            return thread_id

        except TelegramBadRequest as ex:
            msg = ex.message.lower()

            # ✅ topic существует
            if "topic_not_modified" in msg:
                logger.info("forum topic exists (noop) | user=%s thread_id=%s", user_data.id, thread_id)
                return thread_id

            # ❌ topic удалён
            if "topic_id_invalid" in msg or "message thread not found" in msg:
                logger.info("forum topic invalid -> reset | user=%s old_thread_id=%s", user_data.id, thread_id)
                user_data.message_thread_id = None
                user_data.topic_created_at = None
                user_data.topic_start_message_id = None
                user_data.topic_icon_state = None
                await redis.update_user(user_data.id, user_data)
            else:
                raise

    lock_key = f"forum_topic_lock:{user_data.id}"
    lock_token = await redis.acquire_lock(lock_key, ttl_seconds=30, wait_seconds=10.0)
    if not lock_token:
        # Someone else is likely creating the topic; re-check redis and reuse if possible.
        fresh = await redis.get_user(user_data.id)
        if fresh and fresh.message_thread_id:
            user_data.message_thread_id = fresh.message_thread_id
            logger.info(
                "forum topic create lock busy -> reused fresh thread_id | user=%s thread_id=%s",
                user_data.id,
                fresh.message_thread_id,
            )
            return fresh.message_thread_id

        # Last resort: proceed without lock (should be rare).
        logger.warning("forum topic lock timeout | user=%s proceeding without lock", user_data.id)
        lock_token = None

    try:
        # Double-check after lock: another worker may have already created the topic.
        fresh = await redis.get_user(user_data.id)
        if fresh and fresh.message_thread_id:
            user_data.message_thread_id = fresh.message_thread_id
            try:
                await bot.edit_forum_topic(
                    chat_id=config.bot.GROUP_ID,
                    message_thread_id=fresh.message_thread_id,
                    name=topic_name,  # noop
                )
                logger.info(
                    "forum topic already created while waiting for lock | user=%s thread_id=%s",
                    user_data.id,
                    fresh.message_thread_id,
                )
                return fresh.message_thread_id
            except TelegramBadRequest as ex:
                if "topic_id_invalid" in ex.message.lower():
                    user_data.message_thread_id = None
                    user_data.topic_created_at = None
                    user_data.topic_start_message_id = None
                    user_data.topic_icon_state = None
                    await redis.update_user(user_data.id, user_data)
                else:
                    raise

        # создаём новый topic
        # If we're here on explicit verification (e.g. /start), use START icon until the user writes.
        # If we're here from message flow, create with USER icon to avoid extra editForumTopic call.
        icon_state = "start" if verify else "user"
        if config.bot.TOPIC_STATUS_IN_TITLE:
            icon_custom_emoji_id = config.bot.TOPIC_FIXED_EMOJI_ID or config.bot.BOT_EMOJI_ID
        else:
            icon_custom_emoji_id = (
                config.bot.START_EMOJI_ID
                if icon_state == "start"
                else config.bot.BOT_EMOJI_ID
            )
        new_thread_id = await create_forum_topic(
            bot,
            config,
            topic_name,
            icon_custom_emoji_id=icon_custom_emoji_id,
        )
        user_data.message_thread_id = new_thread_id
        user_data.topic_created_at = datetime.now(timezone.utc).isoformat()
        user_data.topic_start_message_id = None
        user_data.topic_icon_state = icon_state
        await redis.update_user(user_data.id, user_data)
        logger.info(
            "forum topic created | user=%s thread_id=%s icon_state=%s icon_id=%s",
            user_data.id,
            new_thread_id,
            icon_state,
            icon_custom_emoji_id,
        )
        return new_thread_id

    finally:
        if lock_token:
            await redis.release_lock(lock_key, lock_token)


async def create_forum_topic(
    bot: Bot,
    config: Config,
    name: str,
    *,
    icon_custom_emoji_id: str | None = None,
) -> int:
    """
    Creates a forum topic in the specified chat.

    :param bot: The Aiogram Bot instance.
    :param config: The configuration object.
    :param name: The name of the forum topic.

    :return: The message thread ID of the created forum topic.
    :raises NotEnoughRightsException: If the bot doesn't have enough rights to create a forum topic.
    :raises CreateForumTopicException: If an error occurs while creating the forum topic.
    """
    try:
        chosen_icon = icon_custom_emoji_id or config.bot.BOT_EMOJI_ID

        # Attempt to create a forum topic
        forum_topic = await bot.create_forum_topic(
            chat_id=config.bot.GROUP_ID,
            name=name,
            icon_custom_emoji_id=chosen_icon,
            request_timeout=30,
        )
        return forum_topic.message_thread_id

    except TelegramRetryAfter as ex:
        # Handle Retry-After exception (rate limiting)
        logging.warning(ex.message)
        await asyncio.sleep(ex.retry_after)
        return await create_forum_topic(bot, config, name, icon_custom_emoji_id=icon_custom_emoji_id)

    except TelegramBadRequest as ex:
        msg = ex.message.lower()

        # Invalid/unsupported custom emoji for topic icon: retry without custom icon.
        if any(
            s in msg
            for s in (
                "icon_custom_emoji_id_invalid",
                "custom emoji",
                "emoji_id_invalid",
            )
        ):
            logger.warning(
                "Topic icon id invalid -> retry without icon | icon_id=%s err=%s",
                chosen_icon,
                ex.message,
            )
            try:
                forum_topic = await bot.create_forum_topic(
                    chat_id=config.bot.GROUP_ID,
                    name=name,
                    request_timeout=30,
                )
                return forum_topic.message_thread_id
            except TelegramRetryAfter as ex2:
                logging.warning(ex2.message)
                await asyncio.sleep(ex2.retry_after)
                return await create_forum_topic(bot, config, name, icon_custom_emoji_id=None)
            except TelegramBadRequest as ex2:
                msg2 = ex2.message.lower()
                if "not enough rights" in msg2 or "chat_admin_required" in msg2:
                    raise NotEnoughRightsException
                if "not a forum" in msg2 or "forum topics are not" in msg2:
                    raise NotAForumException
                logger.warning("Failed to create forum topic without icon | err=%s", ex2.message)
                raise CreateForumTopicException

        # Using some custom emojis as a topic icon may require Telegram Premium.
        # Bots can't be premium, so fail open: create the topic without a custom icon.
        if "premium_account_required" in msg:
            logger.warning(
                "Premium required for topic icon -> retry without icon | icon_id=%s err=%s",
                chosen_icon,
                ex.message,
            )
            if chosen_icon != config.bot.BOT_EMOJI_ID:
                # Try a known-good default icon first (if START/BAN icon is premium).
                return await create_forum_topic(bot, config, name, icon_custom_emoji_id=config.bot.BOT_EMOJI_ID)
            try:
                forum_topic = await bot.create_forum_topic(
                    chat_id=config.bot.GROUP_ID,
                    name=name,
                    request_timeout=30,
                )
                return forum_topic.message_thread_id
            except TelegramRetryAfter as ex2:
                logging.warning(ex2.message)
                await asyncio.sleep(ex2.retry_after)
                return await create_forum_topic(bot, config, name, icon_custom_emoji_id=None)
            except TelegramBadRequest as ex2:
                msg2 = ex2.message.lower()
                if "not enough rights" in msg2 or "chat_admin_required" in msg2:
                    raise NotEnoughRightsException
                if "not a forum" in msg2 or "forum topics are not" in msg2:
                    raise NotAForumException
                logger.warning("Failed to create forum topic without icon | err=%s", ex2.message)
                raise CreateForumTopicException

        if "not enough rights" in msg or "chat_admin_required" in msg:
            # Raise an exception if the bot doesn't have enough rights
            raise NotEnoughRightsException

        elif "not a forum" in msg or "forum topics are not" in msg:
            # Raise an exception if the chat is not a forum
            raise NotAForumException

        # Raise a generic exception for other cases
        logger.warning("Failed to create forum topic | err=%s", ex.message)
        raise CreateForumTopicException

    except Exception as ex:
        # Re-raise any other exceptions
        raise ex


async def update_forum_topic_icon(
    bot: Bot,
    config: Config,
    message_thread_id: int | None,
    icon_custom_emoji_id: str | None,
) -> bool:
    if not message_thread_id:
        return False

    if not icon_custom_emoji_id:
        return False

    for attempt in range(1, 4):
        try:
            await bot.edit_forum_topic(
                chat_id=config.bot.GROUP_ID,
                message_thread_id=message_thread_id,
                icon_custom_emoji_id=icon_custom_emoji_id,
            )
            return True

        except TelegramRetryAfter as ex:
            if attempt >= 3:
                logger.warning(
                    "Rate limited while updating forum topic icon | thread_id=%s retry_after=%s",
                    message_thread_id,
                    ex.retry_after,
                )
                return False
            await asyncio.sleep(ex.retry_after)

        except TelegramBadRequest as ex:
            msg = ex.message.lower()

            # ⛔ НОРМАЛЬНЫЕ ситуации — молча игнорируем
            if (
                "topic_id_invalid" in msg
                or "topic_closed" in msg
                or "message thread not found" in msg
            ):
                logger.info(
                    "Skipped updating topic icon (topic unavailable) | thread_id=%s err=%s",
                    message_thread_id,
                    ex.message,
                )
                return False

            if "topic_not_modified" in msg:
                return True

            if "premium_account_required" in msg:
                logger.warning(
                    "Premium required for topic icon | thread_id=%s icon_id=%s err=%s",
                    message_thread_id,
                    icon_custom_emoji_id,
                    ex.message,
                )
                # Best-effort: try to remove custom icon so we at least don't get stuck on START.
                try:
                    await bot.edit_forum_topic(
                        chat_id=config.bot.GROUP_ID,
                        message_thread_id=message_thread_id,
                        icon_custom_emoji_id="",
                    )
                    return True
                except Exception as ex2:
                    logger.warning(
                        "Failed to remove topic icon after premium error | thread_id=%s err=%s",
                        message_thread_id,
                        ex2,
                    )
                    return False

            if "icon_custom_emoji_id_invalid" in msg or "emoji_id_invalid" in msg or "custom emoji" in msg:
                logger.warning(
                    "Invalid topic icon id | thread_id=%s icon_id=%s err=%s",
                    message_thread_id,
                    icon_custom_emoji_id,
                    ex.message,
                )
                return False

            # ❌ Реальные проблемы
            if "not enough rights" in msg:
                logging.error("Bot has no rights to edit forum topic")
                return False

            logging.error("Failed to update forum topic icon: %s", ex.message)
            return False

        except Exception:
            logging.exception("Unexpected error while updating forum topic icon")
            return False

    return False


def _icon_id_for_state(config: Config, state: str) -> str | None:
    """
    Maps internal topic_icon_state to custom emoji IDs.

    Supported states:
    - start: empty topic (user /start only)
    - user: last message from user
    - manager: last message from support
    - ban: user blocked the bot
    """
    if state == "start":
        return config.bot.START_EMOJI_ID or None
    if state == "user":
        return config.bot.BOT_EMOJI_ID or None
    if state == "manager":
        return config.bot.MANAGER_EMOJI_ID or None
    if state == "ban":
        return getattr(config.bot, "BAN_EMOJI_ID", "") or None
    return None


def _fixed_topic_icon_id(config: Config) -> str | None:
    return (config.bot.TOPIC_FIXED_EMOJI_ID or "").strip() or config.bot.BOT_EMOJI_ID or None


async def update_forum_topic_name(
    bot: Bot,
    config: Config,
    message_thread_id: int | None,
    *,
    name: str,
    icon_custom_emoji_id: str | None = None,
) -> bool:
    if not message_thread_id:
        return False

    if not name:
        return False

    for attempt in range(1, 4):
        try:
            kwargs = {
                "chat_id": config.bot.GROUP_ID,
                "message_thread_id": message_thread_id,
                "name": name,
            }
            if icon_custom_emoji_id:
                kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id
            await bot.edit_forum_topic(**kwargs)
            return True

        except TelegramRetryAfter as ex:
            if attempt >= 3:
                logger.warning(
                    "Rate limited while updating forum topic name | thread_id=%s retry_after=%s",
                    message_thread_id,
                    ex.retry_after,
                )
                return False
            await asyncio.sleep(ex.retry_after)

        except TelegramBadRequest as ex:
            msg = ex.message.lower()

            if "topic_not_modified" in msg:
                if icon_custom_emoji_id:
                    # Some clients seem to ignore name updates when icon is unchanged.
                    # Retry once with name-only to force the title refresh.
                    try:
                        await bot.edit_forum_topic(
                            chat_id=config.bot.GROUP_ID,
                            message_thread_id=message_thread_id,
                            name=name,
                        )
                        logger.info(
                            "Topic name updated (name-only retry) | thread_id=%s name=%s",
                            message_thread_id,
                            name,
                        )
                        return True
                    except TelegramBadRequest as ex2:
                        if "topic_not_modified" in ex2.message.lower():
                            return True
                        logger.warning(
                            "Failed to update topic name (name-only retry) | thread_id=%s err=%s",
                            message_thread_id,
                            ex2.message,
                        )
                        return False
                return True

            if (
                "topic_id_invalid" in msg
                or "topic_closed" in msg
                or "message thread not found" in msg
            ):
                logger.info(
                    "Skipped updating topic name (topic unavailable) | thread_id=%s err=%s",
                    message_thread_id,
                    ex.message,
                )
                return False

            if icon_custom_emoji_id and (
                "icon_custom_emoji_id_invalid" in msg
                or "emoji_id_invalid" in msg
                or "custom emoji" in msg
                or "premium_account_required" in msg
            ):
                logger.warning(
                    "Topic icon invalid/premium, retrying without icon | thread_id=%s icon_id=%s err=%s",
                    message_thread_id,
                    icon_custom_emoji_id,
                    ex.message,
                )
                # Retry once without custom icon.
                icon_custom_emoji_id = None
                continue

            logger.warning(
                "Failed to update topic name | thread_id=%s err=%s",
                message_thread_id,
                ex.message,
            )
            return False

        except TelegramAPIError as ex:
            logger.warning(
                "Failed to update topic name (api error) | thread_id=%s err=%s",
                message_thread_id,
                ex,
            )
            return False

        except Exception:
            logger.exception("Unexpected error while updating topic name")
            return False

    return False


async def update_forum_topic_icon_state_cached(
    *,
    bot: Bot,
    redis: RedisStorage,
    config: Config,
    user_data: UserData,
    message_thread_id: int | None,
    desired_state: str,
    force: bool = False,
) -> bool:
    """
    Cached topic icon update to reduce editForumTopic calls.

    :return: True if the icon is set (or already set), False otherwise.
    """
    if not message_thread_id:
        return False

    desired_key = _topic_icon_desired_key(message_thread_id)
    with contextlib.suppress(Exception):
        await redis.set_value(
            desired_key,
            desired_state,
            ex_seconds=TOPIC_ICON_DESIRED_TTL_SECONDS,
        )

    if config.bot.TOPIC_STATUS_IN_TITLE:
        if not force and user_data.topic_icon_state == desired_state:
            return True

        topic_name = await build_topic_name(
            user_data,
            redis,
            config=config,
            desired_state=desired_state,
        )
        fixed_icon_id = _fixed_topic_icon_id(config)
        ok = await update_forum_topic_name(
            bot,
            config,
            message_thread_id,
            name=topic_name,
            icon_custom_emoji_id=fixed_icon_id,
        )
        if not ok:
            logger.warning(
                "Topic name update failed | user=%s thread_id=%s to=%s",
                user_data.id,
                message_thread_id,
                desired_state,
            )
            return False

        user_data.topic_icon_state = desired_state
        with contextlib.suppress(Exception):
            await redis.update_user(user_data.id, user_data)
        logger.info(
            "Topic name updated | user=%s thread_id=%s state=%s name=%s icon_id=%s",
            user_data.id,
            message_thread_id,
            desired_state,
            topic_name,
            fixed_icon_id or "",
        )
        return True

    if not force and user_data.topic_icon_state == desired_state:
        return True

    icon_id = _icon_id_for_state(config, desired_state)
    if not icon_id:
        return False

    current = user_data.topic_icon_state or "none"
    emit_info = (current in ("none", "start") and desired_state == "user") or desired_state in ("ban", "start")
    if emit_info:
        logger.info(
            "Topic icon update requested | user=%s thread_id=%s from=%s to=%s icon_id=%s",
            user_data.id,
            message_thread_id,
            current,
            desired_state,
            icon_id,
        )

    ok = await update_forum_topic_icon(
        bot=bot,
        config=config,
        message_thread_id=message_thread_id,
        icon_custom_emoji_id=icon_id,
    )
    if not ok:
        logger.warning(
            "Topic icon update failed | user=%s thread_id=%s to=%s icon_id=%s",
            user_data.id,
            message_thread_id,
            desired_state,
            icon_id,
        )
        lock_key = _topic_icon_retry_lock_key(message_thread_id)
        lock_token = await redis.try_acquire_lock(lock_key, ttl_seconds=TOPIC_ICON_RETRY_LOCK_TTL_SECONDS)
        if lock_token:
            logger.info(
                "Topic icon retry scheduled | user=%s thread_id=%s desired=%s",
                user_data.id,
                message_thread_id,
                desired_state,
            )
            asyncio.create_task(
                _retry_topic_icon_update(
                    bot=bot,
                    redis=redis,
                    config=config,
                    user_id=user_data.id,
                    message_thread_id=message_thread_id,
                    lock_key=lock_key,
                    lock_token=lock_token,
                )
            )
        return False

    user_data.topic_icon_state = desired_state
    with contextlib.suppress(Exception):
        await redis.update_user(user_data.id, user_data)
    if emit_info:
        logger.info(
            "Topic icon updated | user=%s thread_id=%s state=%s",
            user_data.id,
            message_thread_id,
            desired_state,
        )
    return True


async def update_forum_topic_icon_cached(
    *,
    bot: Bot,
    redis: RedisStorage,
    config: Config,
    user_data: UserData,
    message_thread_id: int | None,
    is_manager_response: bool,
) -> None:
    if not message_thread_id:
        return

    desired = "manager" if is_manager_response else "user"
    await update_forum_topic_icon_state_cached(
        bot=bot,
        redis=redis,
        config=config,
        user_data=user_data,
        message_thread_id=message_thread_id,
        desired_state=desired,
    )
