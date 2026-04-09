import asyncio
import contextlib
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from app.config import Config
from app.bot.utils.redis import RedisStorage
from app.bot.utils.archive_topic import ensure_archive_topic, ensure_logs_topic
from app.bot.db.dialogue_store import dialogue_store
from app.bot.db.cleanup_stats_store import CleanupStatsStore


logger = logging.getLogger(__name__)
T = TypeVar("T")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def cleanup_blocked_topics(
    *,
    bot: Bot,
    config: Config,
    redis: RedisStorage,
    older_than_days: int = 14,
    empty_older_than_days: int = 14,
    batch_size: int = 50,
    tg_call_min_interval_seconds: float = 0.25,
    tg_send_min_interval_seconds: float = 1.0,
    tg_send_document_min_interval_seconds: float = 15.0,
    tg_max_retries: int = 5,
    stats_store: CleanupStatsStore | None = None,
) -> None:
    """
    Delete forum topics for:
    - users who blocked the bot and stayed blocked for N days
    - inactive topics (no messages) for N days
    - users who started the bot but never sent any messages (empty topics) for N days
    Dialogue content is archived separately (see dialogue_store).
    """
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    blocked_threshold = now - timedelta(days=older_than_days)
    empty_threshold: datetime | None = None
    if empty_older_than_days > 0:
        empty_threshold = now - timedelta(days=empty_older_than_days)

    # Per-action counters: {"action": {"success": N, "failed": N}}
    _stats: dict[str, dict[str, int]] = defaultdict(lambda: {"success": 0, "failed": 0})

    tg_call_min_interval_seconds = max(0.0, tg_call_min_interval_seconds)
    tg_send_min_interval_seconds = max(0.0, tg_send_min_interval_seconds)
    tg_send_document_min_interval_seconds = max(0.0, tg_send_document_min_interval_seconds)
    tg_max_retries = max(1, int(tg_max_retries))

    logger.info(
        (
            "cleanup_blocked_topics start | blocked_days=%s blocked_threshold=%s empty_days=%s empty_threshold=%s "
            "batch_size=%s tg_call_min_interval=%s tg_send_min_interval=%s tg_send_document_min_interval=%s tg_max_retries=%s"
        ),
        older_than_days,
        blocked_threshold.isoformat(),
        empty_older_than_days,
        empty_threshold.isoformat() if empty_threshold else "disabled",
        batch_size,
        tg_call_min_interval_seconds,
        tg_send_min_interval_seconds,
        tg_send_document_min_interval_seconds,
        tg_max_retries,
    )

    # Prefer SQLite source-of-truth queries to avoid scanning all users in Redis.
    user_ids: list[int]
    try:
        if getattr(redis, "user_store", None):
            limit = (batch_size * 10) if batch_size else None
            blocked_ids = await redis.user_store.get_blocked_user_ids(  # type: ignore[attr-defined]
                blocked_threshold_iso=blocked_threshold.isoformat(),
                limit=limit,
            )
            inactive_ids = await redis.user_store.get_inactive_user_ids(  # type: ignore[attr-defined]
                inactive_threshold_iso=blocked_threshold.isoformat(),
                limit=limit,
            )
            empty_ids: list[int] = []
            if empty_threshold:
                empty_ids = await redis.user_store.get_empty_topic_user_ids(  # type: ignore[attr-defined]
                    empty_threshold_iso=empty_threshold.isoformat(),
                    limit=limit,
                )

            seen: set[int] = set()
            user_ids = []
            for uid in [*blocked_ids, *inactive_ids, *empty_ids]:
                if uid in seen:
                    continue
                seen.add(uid)
                user_ids.append(uid)
        else:
            user_ids = await redis.get_all_users_ids()
    except Exception:
        logger.exception("cleanup_blocked_topics candidate query failed; falling back to redis scan")
        user_ids = await redis.get_all_users_ids()

    logger.info("cleanup_blocked_topics candidates=%s", len(user_ids))

    archive_thread_id = None
    logs_thread_id = None
    for attempt in range(1, tg_max_retries + 1):
        try:
            archive_thread_id = await ensure_archive_topic(bot=bot, config=config, redis=redis)
            logs_thread_id = await ensure_logs_topic(bot=bot, config=config, redis=redis)
            break
        except TelegramRetryAfter as ex:
            logger.warning(
                "ensure topics floodwait | retry_after=%s attempt=%s/%s",
                ex.retry_after,
                attempt,
                tg_max_retries,
            )
            await asyncio.sleep(max(float(ex.retry_after), 0.0))
        except Exception:
            logger.exception("ensure topics failed")
            break

    next_any_call_at = 0.0  # loop.time() monotonic
    next_send_call_at = 0.0
    next_send_document_at = 0.0

    async def _tg_call(
        *,
        kind: str,  # "any" | "send" | "send_document"
        description: str,
        fn: Callable[[], Awaitable[T]],
    ) -> T | None:
        """
        Sequential Telegram calls with floodwait handling.

        Applies both a small constant delay (to avoid bursts) and respects TelegramRetryAfter.
        """
        nonlocal next_any_call_at, next_send_call_at, next_send_document_at
        loop = asyncio.get_running_loop()
        last_retry_after: float | None = None

        for attempt in range(1, tg_max_retries + 1):
            now = loop.time()
            wait_until = next_any_call_at
            if kind in {"send", "send_document"}:
                wait_until = max(wait_until, next_send_call_at)
            if kind == "send_document":
                wait_until = max(wait_until, next_send_document_at)

            if now < wait_until:
                await asyncio.sleep(wait_until - now)

            try:
                result = await fn()
            except TelegramRetryAfter as ex:
                retry_after = max(float(ex.retry_after), 0.0)
                last_retry_after = retry_after
                bump = loop.time() + retry_after
                # Floodwait is often global-ish per chat/method; pause all cleanup calls.
                next_any_call_at = max(next_any_call_at, bump)
                next_send_call_at = max(next_send_call_at, bump)
                next_send_document_at = max(next_send_document_at, bump)
                logger.warning(
                    "cleanup floodwait | kind=%s retry_after=%s attempt=%s/%s desc=%s",
                    kind,
                    ex.retry_after,
                    attempt,
                    tg_max_retries,
                    description,
                )
                await asyncio.sleep(retry_after)
                continue

            now2 = loop.time()
            next_any_call_at = max(next_any_call_at, now2 + tg_call_min_interval_seconds)
            if kind in {"send", "send_document"}:
                next_send_call_at = max(next_send_call_at, now2 + tg_send_min_interval_seconds)
            if kind == "send_document":
                next_send_document_at = max(next_send_document_at, now2 + tg_send_document_min_interval_seconds)

            return result

        logger.error(
            "cleanup floodwait unresolved | kind=%s attempts=%s last_retry_after=%s desc=%s",
            kind,
            tg_max_retries,
            last_retry_after,
            description,
        )
        return None

    async def _post_cleanup_log(*, text: str, description: str) -> None:
        nonlocal logs_thread_id

        async def _send(thread_id: int | None) -> None:
            kwargs = {}
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            await bot.send_message(
                chat_id=config.bot.GROUP_ID,
                text=text,
                **kwargs,
                disable_notification=True,
            )

        try:
            await _tg_call(
                kind="send",
                description=description,
                fn=lambda: _send(logs_thread_id),
            )
        except TelegramBadRequest:
            # Thread id might be invalid; fall back to the group root.
            logs_thread_id = None
            try:
                await _tg_call(
                    kind="send",
                    description=f"{description} (fallback)",
                    fn=lambda: _send(None),
                )
            except Exception:
                logger.exception("Failed to send cleanup log (fallback) | desc=%s", description)
        except Exception:
            logger.exception("Failed to send cleanup log | desc=%s", description)

    async def _delete_topic(
        *,
        user: Any,
        thread_id: int,
        action: str,
        reason: str,
        extra: str = "",
        attach_dialogue: bool = False,
        copy_topic_start_message: bool = False,
    ) -> bool:
        try:
            if copy_topic_start_message and logs_thread_id and getattr(user, "topic_start_message_id", None):
                try:
                    await _tg_call(
                        kind="send",
                        description=f"copy_topic_start_message user={user.id}",
                        fn=lambda: bot.copy_message(
                            chat_id=config.bot.GROUP_ID,
                            from_chat_id=config.bot.GROUP_ID,
                            message_id=int(user.topic_start_message_id),
                            message_thread_id=logs_thread_id,
                            disable_notification=True,
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Failed to copy topic start message to logs | user=%s message_id=%s",
                        user.id,
                        getattr(user, "topic_start_message_id", None),
                    )

            deleted = await _tg_call(
                kind="any",
                description=f"delete_forum_topic action={action} user={user.id} thread={thread_id}",
                fn=lambda: bot.delete_forum_topic(
                    chat_id=config.bot.GROUP_ID,
                    message_thread_id=thread_id,
                ),
            )
            if deleted is None:
                logger.error(
                    "Failed to delete forum topic due to floodwait | action=%s user=%s thread_id=%s",
                    action,
                    user.id,
                    thread_id,
                )
                return False

            user.message_thread_id = None
            if hasattr(user, "topic_start_message_id"):
                user.topic_start_message_id = None
            if hasattr(user, "topic_icon_state"):
                user.topic_icon_state = None
            if hasattr(user, "archive_restore_pending"):
                user.archive_restore_pending = True
            if hasattr(user, "archive_sent_at"):
                user.archive_sent_at = None
            user.archived_at = datetime.now(timezone.utc).isoformat()
            await redis.update_user(user.id, user)
            logger.info("Deleted forum topic | action=%s user=%s thread_id=%s", action, user.id, thread_id)
            await _post_cleanup_log(
                text=(
                    "<b>🧾 Cleanup</b>\n"
                    f"Action: <code>{action}</code>\n"
                    f"TG ID: <code>{user.id}</code>\n"
                    f"thread_id: <code>{thread_id}</code>\n"
                    f"Reason: {reason}\n"
                    f"state: <code>{user.state}</code>\n"
                    f"last_activity_at: <code>{getattr(user, 'last_activity_at', None)}</code>\n"
                    f"topic_created_at: <code>{getattr(user, 'topic_created_at', None)}</code>\n"
                    f"blocked_at: <code>{getattr(user, 'blocked_at', None)}</code>\n"
                    f"{extra}".rstrip()
                ),
                description=f"cleanup_log action={action} user={user.id}",
            )

            if attach_dialogue:
                try:
                    if await dialogue_store.has_any_messages(user.id):
                        await _tg_call(
                            kind="send_document",
                            description=f"dialogue_archive action={action} user={user.id}",
                            fn=lambda: dialogue_store.send_recent_transcript(
                                bot=bot,
                                user_id=user.id,
                                chat_id=config.bot.GROUP_ID,
                                message_thread_id=archive_thread_id,
                                title=f"📎 Dialogue archive | TG ID {user.id}",
                                limit=200,
                            ),
                        )
                except Exception:
                    logger.exception("Failed to attach dialogue archive | user=%s", user.id)
            return True
        except TelegramForbiddenError as ex:
            logger.warning(
                "No rights to delete forum topic | action=%s thread_id=%s err=%s",
                action,
                thread_id,
                ex.message,
            )
            await _post_cleanup_log(
                text=(
                    "<b>🧾 Cleanup</b>\n"
                    f"Action: <code>{action}_failed</code>\n"
                    f"TG ID: <code>{user.id}</code>\n"
                    f"thread_id: <code>{thread_id}</code>\n"
                    f"Error: <code>{ex.message}</code>"
                ),
                description=f"cleanup_log action={action}_failed user={user.id}",
            )
            return False
        except TelegramBadRequest as ex:
            # Topic already deleted / invalid id; clean up pointer.
            msg = ex.message.lower()
            if "topic_id_invalid" in msg or "message thread not found" in msg:
                user.message_thread_id = None
                if hasattr(user, "topic_start_message_id"):
                    user.topic_start_message_id = None
                if hasattr(user, "topic_icon_state"):
                    user.topic_icon_state = None
                if hasattr(user, "archive_restore_pending"):
                    user.archive_restore_pending = True
                if hasattr(user, "archive_sent_at"):
                    user.archive_sent_at = None
                user.archived_at = datetime.now(timezone.utc).isoformat()
                await redis.update_user(user.id, user)
                await _post_cleanup_log(
                    text=(
                        "<b>🧾 Cleanup</b>\n"
                        f"Action: <code>topic_missing_cleanup</code>\n"
                        f"TG ID: <code>{user.id}</code>\n"
                        f"old_thread_id: <code>{thread_id}</code>\n"
                        f"Reason: <code>{ex.message}</code>"
                    ),
                    description=f"cleanup_log topic_missing_cleanup user={user.id}",
                )
                return True
            else:
                logger.warning(
                    "Failed to delete forum topic | action=%s thread_id=%s err=%s",
                    action,
                    thread_id,
                    ex.message,
                )
                await _post_cleanup_log(
                    text=(
                        "<b>🧾 Cleanup</b>\n"
                        f"Action: <code>{action}_failed</code>\n"
                        f"TG ID: <code>{user.id}</code>\n"
                        f"thread_id: <code>{thread_id}</code>\n"
                        f"Error: <code>{ex.message}</code>"
                    ),
                    description=f"cleanup_log action={action}_failed user={user.id}",
                )
                return False
        except Exception:
            logger.exception("Unexpected error while deleting forum topic | action=%s thread_id=%s", action, thread_id)
            await _post_cleanup_log(
                text=(
                    "<b>🧾 Cleanup</b>\n"
                    f"Action: <code>{action}_failed</code>\n"
                    f"TG ID: <code>{user.id}</code>\n"
                    f"thread_id: <code>{thread_id}</code>\n"
                    "Error: <code>unexpected</code>"
                ),
                description=f"cleanup_log action={action}_failed user={user.id}",
            )
            return False

    if batch_size < 0:
        batch_size = 0

    processed = 0
    scanned = 0

    for user_id in user_ids:
        scanned += 1

        if batch_size and processed >= batch_size:
            logger.info(
                "cleanup_blocked_topics batch limit reached | processed=%s scanned=%s total_users=%s",
                processed,
                scanned,
                len(user_ids),
            )
            break

        user = await redis.get_user(user_id)
        if not user:
            continue

        if not user.message_thread_id:
            continue

        thread_id = user.message_thread_id

        # 1) Blocked users: delete after N days.
        if user.state == "kicked":
            blocked_at = _parse_iso(user.blocked_at)
            if not blocked_at:
                # Fallback for legacy records where blocked_at was never set:
                # use last_activity_at or topic_created_at as a proxy date.
                blocked_at = (
                    _parse_iso(user.last_activity_at)
                    or _parse_iso(getattr(user, "topic_created_at", None))
                )
            if not blocked_at or blocked_at > blocked_threshold:
                continue

            ok = await _delete_topic(
                user=user,
                thread_id=thread_id,
                action="delete_topic",
                reason=f"blocked &gt; {older_than_days} days",
                extra=f"Dialogue: <code>attached</code>\nblocked_at: <code>{user.blocked_at}</code>",
                attach_dialogue=True,
            )
            _stats["delete_topic"]["success" if ok else "failed"] += 1
            if ok:
                processed += 1
            continue

        # 2) Inactive topics: no messages for N days.
        if user.last_activity_at:
            last_activity_at = _parse_iso(user.last_activity_at)
            if last_activity_at and last_activity_at <= blocked_threshold:
                ok = await _delete_topic(
                    user=user,
                    thread_id=thread_id,
                    action="delete_topic_inactive",
                    reason=f"inactive &gt; {older_than_days} days",
                    extra=f"Dialogue: <code>attached</code>\nlast_activity_at: <code>{user.last_activity_at}</code>",
                    attach_dialogue=True,
                )
                _stats["delete_topic_inactive"]["success" if ok else "failed"] += 1
                if ok:
                    processed += 1
                continue

        # 3) Empty topics: user started the bot, but no messages were ever exchanged.
        if not empty_threshold:
            continue

        if user.last_activity_at:
            continue

        topic_created_at = _parse_iso(getattr(user, "topic_created_at", None))
        if not topic_created_at or topic_created_at > empty_threshold:
            continue

        # Extra safety: do not delete if we have any dialogue stored.
        try:
            if await dialogue_store.has_any_messages(user.id):
                continue
        except Exception:
            # If we can't check, fail open: skip deletion.
            logger.exception("Failed to check dialogue archive for empty-topic cleanup | user=%s", user.id)
            continue

        ok = await _delete_topic(
            user=user,
            thread_id=thread_id,
            action="delete_topic_empty",
            reason=f"no messages &gt; {empty_older_than_days} days",
            extra=f"topic_created_like: <code>{topic_created_at.isoformat()}</code>",
            attach_dialogue=False,
            copy_topic_start_message=False,
        )
        _stats["delete_topic_empty"]["success" if ok else "failed"] += 1
        if ok:
            processed += 1

    logger.info(
        "cleanup_blocked_topics done | processed=%s scanned=%s total_users=%s",
        processed,
        scanned,
        len(user_ids),
    )

    if stats_store and _stats:
        with contextlib.suppress(Exception):
            await stats_store.flush(date_str=date_str, stats=_stats)
