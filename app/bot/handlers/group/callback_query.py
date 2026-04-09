from aiogram import Router, F
from aiogram.filters import MagicData
from aiogram.types import CallbackQuery

router = Router()
router.callback_query.filter(
    MagicData(F.event_chat.id == F.config.bot.GROUP_ID),  # type: ignore
    F.message.chat.type.in_(["group", "supergroup"]),
    F.message.message_thread_id.is_not(None),
)
