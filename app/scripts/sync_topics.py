import argparse
import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from redis.asyncio import Redis

from app.config import load_config
from app.bot.utils.redis import RedisStorage
from app.bot.db.user_state_store import user_state_store
from app.bot.utils.create_forum_topic import update_forum_topic_icon_state_cached

logger = logging.getLogger(__name__)


def _infer_state(user_data) -> str:
    if getattr(user_data, "state", None) == "kicked":
        return "ban"
    if getattr(user_data, "topic_icon_state", None):
        return user_data.topic_icon_state
    if not getattr(user_data, "last_activity_at", None):
        return "start"
    return "user"


async def run() -> None:
    parser = argparse.ArgumentParser(description="Sync topic names/icons for all existing threads")
    parser.add_argument("--tg-id", type=int, default=0, help="Process only this TG user id")
    parser.add_argument("--limit", type=int, default=0, help="Max users to process (0 = no limit)")
    parser.add_argument("--sleep", type=float, default=0.25, help="Sleep between updates (seconds)")
    args = parser.parse_args()

    config = load_config()

    redis = Redis.from_url(config.redis.dsn())
    storage = RedisStorage(redis)

    await user_state_store.init()

    bot = Bot(
        token=config.bot.TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    try:
        if args.tg_id:
            user_ids = [int(args.tg_id)]
        else:
            user_ids = await user_state_store.get_all_user_ids()
        processed = 0
        skipped = 0
        failed = 0

        for user_id in user_ids:
            if args.limit and processed >= args.limit:
                break

            user_data = await user_state_store.get_user(user_id)
            if not user_data or not user_data.message_thread_id:
                skipped += 1
                continue

            desired_state = _infer_state(user_data)
            ok = await update_forum_topic_icon_state_cached(
                bot=bot,
                redis=storage,
                config=config,
                user_data=user_data,
                message_thread_id=user_data.message_thread_id,
                desired_state=desired_state,
                force=True,
            )
            if ok:
                processed += 1
            else:
                failed += 1

            if args.sleep > 0:
                await asyncio.sleep(args.sleep)

        logger.info(
            "sync_topics done | updated=%s failed=%s skipped=%s total=%s",
            processed,
            failed,
            skipped,
            len(user_ids),
        )
    finally:
        await bot.session.close()
        await redis.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
