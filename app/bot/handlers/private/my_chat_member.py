import logging

from aiogram import Router, F
from aiogram.enums import ChatMemberStatus
from aiogram.types import ChatMemberUpdated
from aiogram.utils.markdown import hlink

from app.bot.manager import Manager
from app.bot.utils.create_forum_topic import (
    get_or_create_forum_topic,
    update_forum_topic_icon_state_cached,
)
from app.bot.utils.redis import RedisStorage
from app.bot.utils.redis.models import UserData
from app.bot.db.dialogue_store import utc_now_iso

router = Router()
router.my_chat_member.filter(F.chat.type == "private")
logger = logging.getLogger(__name__)


@router.my_chat_member()
async def handle_chat_member_update(
        update: ChatMemberUpdated,
        redis: RedisStorage,
        user_data: UserData,
        manager: Manager,
) -> None:
    """
    Handle updates of the bot chat member status.

    :param update: ChatMemberUpdated object.
    :param redis: RedisStorage object.
    :param user_data: UserData object.
    :param manager: Manager object.
    :return: None
    """
    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status
    logger.info("my_chat_member | user=%s %s -> %s", user_data.id, old_status, new_status)

    # Update the user's state based on the new chat member status
    user_data.state = new_status

    if new_status == ChatMemberStatus.KICKED:
        user_data.blocked_at = utc_now_iso()
        user_data.archive_restore_pending = False
        logger.info("user blocked bot | user=%s blocked_at=%s", user_data.id, user_data.blocked_at)

    if old_status == ChatMemberStatus.KICKED and new_status == ChatMemberStatus.MEMBER:
        # User unblocked the bot: mark archive to be restored into the next (re)opened topic.
        user_data.archive_restore_pending = True
        logger.info("user unblocked bot | user=%s archive_restore_pending=true", user_data.id)

    await redis.update_user(user_data.id, user_data)

    if user_data.state == ChatMemberStatus.MEMBER:
        text = manager.text_message.get("user_restarted_bot")
    elif user_data.state == ChatMemberStatus.KICKED:
        # In private chats this usually means the user blocked the bot.
        text = manager.text_message.get("user_blocked_bot")
    else:
        # LEFT / other states: user stopped the bot / removed chat.
        text = manager.text_message.get("user_stopped_bot")

    url = f"https://t.me/{user_data.username[1:]}" if user_data.username != "-" else f"tg://user?id={user_data.id}"

    # Always post status update into user's topic.
    # If topic was deleted (e.g. cleanup), recreate it first so message won't go to "general".
    kwargs = {}
    thread_id: int | None = None
    try:
        thread_id = await get_or_create_forum_topic(
            update.bot,
            redis,
            manager.config,
            user_data,
            verify=True,
        )
        kwargs["message_thread_id"] = thread_id
    except Exception as ex:
        logger.warning("Failed to ensure user topic for status message | user=%s err=%s", user_data.id, ex)

    # Update topic icon based on user status (blocked/unblocked).
    if thread_id:
        try:
            if new_status == ChatMemberStatus.KICKED and manager.config.bot.BAN_EMOJI_ID:
                await update_forum_topic_icon_state_cached(
                    bot=update.bot,
                    redis=redis,
                    config=manager.config,
                    user_data=user_data,
                    message_thread_id=thread_id,
                    desired_state="ban",
                )
            elif old_status == ChatMemberStatus.KICKED and new_status == ChatMemberStatus.MEMBER:
                desired = "start" if not user_data.last_activity_at else "user"
                await update_forum_topic_icon_state_cached(
                    bot=update.bot,
                    redis=redis,
                    config=manager.config,
                    user_data=user_data,
                    message_thread_id=thread_id,
                    desired_state=desired,
                )
        except Exception as ex:
            logger.warning("Failed to update topic icon on status change | user=%s thread_id=%s err=%s", user_data.id, thread_id, ex)

    await update.bot.send_message(
        chat_id=manager.config.bot.GROUP_ID,
        text=text.format(name=hlink(user_data.full_name, url)),
        disable_notification=True,
        **kwargs,
    )
