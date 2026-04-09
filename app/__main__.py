import asyncio
import os
import logging
import contextlib
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .bot import commands
from .bot.handlers import include_routers
from .bot.middlewares import register_middlewares
from .bot.db.cleanup_stats_store import cleanup_stats_store
from .bot.db.dialogue_store import dialogue_store
from .bot.db.user_state_store import user_state_store
from .bot.jobs.cleanup import cleanup_blocked_topics
from .bot.utils.archive_topic import ensure_archive_topic
from .bot.utils.redis import RedisStorage as UsersRedisStorage
from .config import load_config, Config
from .logger import setup_logger

logger = logging.getLogger(__name__)


async def on_shutdown(
    apscheduler: AsyncIOScheduler,
    dispatcher: Dispatcher,
    config: Config,
    bot: Bot,
) -> None:
    """
    Graceful shutdown handler — cleans up scheduler, commands, storage, and session.

    :param apscheduler: The scheduler instance.
    :param dispatcher: The bot dispatcher.
    :param config: Application config.
    :param bot: The bot instance.
    """
    apscheduler.shutdown()
    await commands.delete(bot, config)
    await dispatcher.storage.close()
    await bot.delete_webhook()
    await bot.session.close()


async def on_startup(
    apscheduler: AsyncIOScheduler,
    config: Config,
    bot: Bot,
    dispatcher: Dispatcher,
) -> None:
    """
    Startup handler — initialises storage, registers commands, schedules background jobs.

    :param apscheduler: The scheduler instance.
    :param config: Application config.
    :param bot: The bot instance.
    :param dispatcher: The bot dispatcher.
    """
    apscheduler.start()
    await commands.setup(bot, config)

    await dialogue_store.init()

    with contextlib.suppress(Exception):
        await cleanup_stats_store.init()

    try:
        await user_state_store.init()
    except Exception:
        logger.critical(
            "CRITICAL: SQLite user_state_store init FAILED — user states won't persist to disk!"
        )
        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id=config.bot.GROUP_ID,
                text=(
                    "⚠️ <b>ALERT</b>: SQLite user_state_store failed to initialise.\n"
                    "Topic states will not be persisted. They may be lost after a restart."
                ),
            )

    users_redis = UsersRedisStorage(dispatcher.storage.redis)  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        await users_redis.backfill_user_store_from_redis()
    await ensure_archive_topic(bot=bot, config=config, redis=users_redis)

    cleanup_interval_minutes = int(os.getenv("CLEANUP_BLOCKED_TOPICS_INTERVAL_MINUTES", "720"))
    cleanup_older_than_days = int(os.getenv("CLEANUP_BLOCKED_TOPICS_OLDER_THAN_DAYS", "14"))
    cleanup_empty_older_than_days = int(
        os.getenv("CLEANUP_EMPTY_TOPICS_OLDER_THAN_DAYS", str(cleanup_older_than_days))
    )
    cleanup_batch_size = int(os.getenv("CLEANUP_BLOCKED_TOPICS_BATCH_SIZE", "50"))
    cleanup_tg_call_min_interval_seconds = float(os.getenv("CLEANUP_TG_CALL_MIN_INTERVAL_SECONDS", "0.25"))
    cleanup_tg_send_min_interval_seconds = float(os.getenv("CLEANUP_TG_SEND_MIN_INTERVAL_SECONDS", "1"))
    cleanup_tg_send_document_min_interval_seconds = float(
        os.getenv("CLEANUP_TG_SEND_DOCUMENT_MIN_INTERVAL_SECONDS", "15")
    )
    cleanup_tg_max_retries = int(os.getenv("CLEANUP_TG_MAX_RETRIES", "5"))

    apscheduler.add_job(
        cleanup_blocked_topics,
        trigger="interval",
        minutes=cleanup_interval_minutes,
        next_run_time=datetime.now(timezone.utc),
        kwargs={
            "bot": bot,
            "config": config,
            "redis": users_redis,
            "older_than_days": cleanup_older_than_days,
            "empty_older_than_days": cleanup_empty_older_than_days,
            "batch_size": cleanup_batch_size,
            "tg_call_min_interval_seconds": cleanup_tg_call_min_interval_seconds,
            "tg_send_min_interval_seconds": cleanup_tg_send_min_interval_seconds,
            "tg_send_document_min_interval_seconds": cleanup_tg_send_document_min_interval_seconds,
            "tg_max_retries": cleanup_tg_max_retries,
            "stats_store": cleanup_stats_store,
        },
        id="cleanup_blocked_topics",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        jobstore="local",
    )


async def main() -> None:
    """Entry point — initialises bot, dispatcher, scheduler, and starts polling."""
    config = load_config()

    apscheduler = AsyncIOScheduler(
        jobstores={"default": MemoryJobStore(), "local": MemoryJobStore()},
    )

    storage = RedisStorage.from_url(url=config.redis.dsn())

    bot = Bot(
        token=config.bot.TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(
        apscheduler=apscheduler,
        storage=storage,
        config=config,
        bot=bot,
    )

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    include_routers(dp)
    register_middlewares(dp, config=config, redis=storage.redis, apscheduler=apscheduler)

    await bot.delete_webhook()
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    setup_logger()
    asyncio.run(main())
