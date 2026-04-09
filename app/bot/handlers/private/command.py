import logging

from aiogram import Router, F
from aiogram.filters import Command, MagicData
from aiogram.types import Message
from aiogram_newsletter.manager import ANManager

from app.bot.handlers.private.windows import Window
from app.bot.manager import Manager
from app.bot.utils.create_forum_topic import get_or_create_forum_topic
from app.bot.utils.redis import RedisStorage
from app.bot.utils.redis.models import UserData

logger = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type == "private")


async def _setup_user_language(manager: Manager, redis: RedisStorage, user_data: UserData) -> bool:
    """Ensure the user's language is set.

    Returns True if the language is already known (proceed to main menu),
    False if the user needs to pick a language first.
    """
    if user_data.language_code:
        manager.text_message.language_code = user_data.language_code
        return True

    if not manager.config.bot.MULTI_LANGUAGE:
        user_data.language_code = manager.config.bot.DEFAULT_LANGUAGE
        await redis.update_user(user_data.id, user_data)
        manager.text_message.language_code = user_data.language_code
        return True

    return False


@router.message(Command("start"))
async def cmd_start(
    message: Message,
    manager: Manager,
    redis: RedisStorage,
    user_data: UserData,
) -> None:
    """Handle the /start command.

    Sets up the user's language preference if not already set, then shows
    the main menu. Creates (or verifies) the forum topic in the support group.
    """
    language_set = await _setup_user_language(manager, redis, user_data)

    if language_set:
        await Window.main_menu(manager)
    else:
        await Window.select_language(manager)

    await manager.delete_message(message)

    await get_or_create_forum_topic(message.bot, redis, manager.config, user_data, verify=True)


@router.message(Command("language"))
async def cmd_language(
    message: Message,
    manager: Manager,
    user_data: UserData,
) -> None:
    """Handle the /language command — allows the user to change their language."""
    if user_data.language_code:
        await Window.change_language(manager)
    else:
        await Window.select_language(manager)
    await manager.delete_message(message)


@router.message(Command("source"))
async def cmd_source(message: Message, manager: Manager) -> None:
    """Handle the /source command — sends a link to the project source code."""
    text = manager.text_message.get("source")
    await manager.send_message(text)
    await manager.delete_message(message)


@router.message(
    Command("newsletter"),
    MagicData(F.event_from_user.id == F.config.bot.DEV_ID),  # type: ignore
)
async def cmd_newsletter(
    message: Message,
    manager: Manager,
    an_manager: ANManager,
    redis: RedisStorage,
) -> None:
    """Handle the /newsletter command (developer only).

    Opens the aiogram-newsletter bulk messaging menu.
    Only available to the user whose ID matches BOT_DEV_ID.
    """
    users_ids = await redis.get_all_users_ids()
    await an_manager.newsletter_menu(users_ids, Window.main_menu)
    await manager.delete_message(message)
