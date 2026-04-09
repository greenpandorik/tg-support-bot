import logging

from aiogram import Router, F
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery

from app.bot.handlers.private.windows import Window
from app.bot.manager import Manager
from app.bot.utils.redis import RedisStorage
from app.bot.utils.redis.models import UserData
from app.bot.utils.texts import SUPPORTED_LANGUAGES

logger = logging.getLogger(__name__)

router = Router()
router.callback_query.filter(F.message.chat.type == "private", StateFilter(None))


@router.callback_query()
async def handle_language_selection(
    call: CallbackQuery,
    manager: Manager,
    redis: RedisStorage,
    user_data: UserData,
) -> None:
    """Handle language selection from the inline keyboard.

    When the user taps a language button, stores the choice in Redis and
    switches to the main menu.
    """
    if call.data in SUPPORTED_LANGUAGES:
        user_data.language_code = call.data
        manager.text_message.language_code = call.data
        await redis.update_user(user_data.id, user_data)
        await manager.state.update_data(language_code=call.data)
        await Window.main_menu(manager)

    await call.answer()
