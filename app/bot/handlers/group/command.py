import logging
import html
import os
from contextlib import suppress

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, MagicData
from aiogram.types import Message
from aiogram.utils.markdown import hcode

from app.bot.manager import Manager
from app.bot.utils.redis import RedisStorage
from app.bot.db.dialogue_store import dialogue_store
from app.bot.utils.create_forum_topic import update_forum_topic_icon_state_cached
from app.bot.utils.reactions import pin_message_safe
from app.bot.db.cleanup_stats_store import cleanup_stats_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def _support_admin_ids() -> set[int]:
    """Return the set of Telegram user IDs that are allowed to run support commands.

    Populated from the SUPPORT_ADMIN_IDS environment variable (comma-separated).
    """
    raw = (os.getenv("SUPPORT_ADMIN_IDS") or "").strip()
    ids: set[int] = set()
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


async def _ensure_support_access(message: Message, manager: Manager) -> bool:
    """Return True if the sender is allowed to run support commands, otherwise reply with an error."""
    if message.from_user and message.from_user.id in _support_admin_ids():
        return True
    await message.reply("⛔️ Insufficient permissions.", disable_notification=True)
    return False


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

router_id = Router()
router_id.message.filter(F.chat.type.in_(["group", "supergroup"]))

router = Router()
router.message.filter(
    F.message_thread_id.is_not(None),
    F.chat.type.in_(["group", "supergroup"]),
    MagicData(F.event_chat.id == F.config.bot.GROUP_ID),  # type: ignore
)


# ---------------------------------------------------------------------------
# /id
# ---------------------------------------------------------------------------

@router_id.message(Command("id"))
async def handle_id(message: Message, manager: Manager) -> None:
    """Reply with the current chat ID. Useful for initial bot setup."""
    if not await _ensure_support_access(message, manager):
        return
    logger.info("cmd /id | chat_id=%s from_user=%s", message.chat.id,
                message.from_user.id if message.from_user else None)
    await message.reply(hcode(message.chat.id), disable_notification=True)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

@router.message(Command("help"))
async def handle_help(message: Message, manager: Manager, redis: RedisStorage) -> None:
    """Send the list of available support commands."""
    if not await _ensure_support_access(message, manager):
        return

    user_data = await redis.get_by_message_thread_id(message.message_thread_id)
    if not user_data:
        return

    manager.text_message.language_code = user_data.language_code or manager.config.bot.DEFAULT_LANGUAGE
    logger.info("cmd /help | thread_id=%s user=%s", message.message_thread_id, user_data.id)
    await message.reply(manager.text_message.get("support_commands"), disable_notification=True)


# ---------------------------------------------------------------------------
# /silent
# ---------------------------------------------------------------------------

@router.message(Command("silent"))
async def handle_silent(message: Message, manager: Manager, redis: RedisStorage) -> None:
    """Toggle silent mode for the user's topic.

    When silent mode is active, messages from the support side are not forwarded to the user.
    A pinned notice is posted inside the topic to remind agents of the current state.
    """
    if not await _ensure_support_access(message, manager):
        return

    user_data = await redis.get_by_message_thread_id(message.message_thread_id)
    if not user_data:
        return

    if user_data.message_silent_mode:
        text = manager.text_message.get("silent_mode_disabled")
        with suppress(TelegramBadRequest):
            await message.reply(text, disable_notification=True)
            await message.bot.unpin_chat_message(
                chat_id=message.chat.id,
                message_id=user_data.message_silent_id,
            )
        user_data.message_silent_mode = False
        user_data.message_silent_id = None
        logger.info("cmd /silent disabled | thread_id=%s user=%s", message.message_thread_id, user_data.id)
    else:
        text = manager.text_message.get("silent_mode_enabled")
        with suppress(TelegramBadRequest):
            msg = await message.reply(text, disable_notification=True)
            pinned = await pin_message_safe(msg, disable_notification=True)
            if not pinned:
                logger.warning(
                    "Failed to pin silent message | chat=%s thread_id=%s message_id=%s",
                    message.chat.id, message.message_thread_id, msg.message_id,
                )
        user_data.message_silent_mode = True
        user_data.message_silent_id = msg.message_id
        logger.info("cmd /silent enabled | thread_id=%s user=%s", message.message_thread_id, user_data.id)

    await redis.update_user(user_data.id, user_data)


# ---------------------------------------------------------------------------
# /close
# ---------------------------------------------------------------------------

@router.message(Command("close"))
async def handle_close(message: Message, manager: Manager, redis: RedisStorage) -> None:
    """Mark the topic icon/title as answered by a manager.

    Updates the forum topic icon (or title prefix if TOPIC_STATUS_IN_TITLE is enabled)
    to the "manager" state, indicating a reply has been sent.
    """
    if not await _ensure_support_access(message, manager):
        return

    user_data = await redis.get_by_message_thread_id(message.message_thread_id)
    if not user_data:
        return

    manager.text_message.language_code = user_data.language_code or manager.config.bot.DEFAULT_LANGUAGE
    is_ru = manager.text_message.language_code == "ru"

    logger.info(
        "cmd /close | thread_id=%s user=%s from_user=%s state=%s topic_icon_state=%s",
        message.message_thread_id, user_data.id,
        message.from_user.id if message.from_user else None,
        user_data.state, user_data.topic_icon_state,
    )

    if manager.config.bot.TOPIC_STATUS_IN_TITLE:
        if not manager.config.bot.TOPIC_STATUS_EMOJI_MANAGER:
            text = "⚠️ TOPIC_STATUS_EMOJI_MANAGER не задан" if is_ru else "⚠️ TOPIC_STATUS_EMOJI_MANAGER is not set"
            await message.reply(text, disable_notification=True)
            return
    else:
        if not manager.config.bot.MANAGER_EMOJI_ID:
            text = "⚠️ MANAGER_EMOJI_ID не задан" if is_ru else "⚠️ MANAGER_EMOJI_ID is not set"
            await message.reply(text, disable_notification=True)
            return

    ok = await update_forum_topic_icon_state_cached(
        bot=message.bot,
        redis=redis,
        config=manager.config,
        user_data=user_data,
        message_thread_id=message.message_thread_id,
        desired_state="manager",
        force=True,
    )
    if ok:
        text = "✅ Статус менеджера установлен" if is_ru else "✅ Manager status set"
    else:
        text = "⚠️ Не удалось обновить статус" if is_ru else "⚠️ Failed to update status"
    await message.reply(text, disable_notification=True)


# ---------------------------------------------------------------------------
# /info
# ---------------------------------------------------------------------------

@router.message(Command("info"))
async def handle_info(message: Message, manager: Manager, redis: RedisStorage) -> None:
    """Show a basic info card for the user associated with this topic.

    Displays Telegram ID, username, full name, language, ban status, and
    the date the topic was created.
    """
    if not await _ensure_support_access(message, manager):
        return

    user_data = await redis.get_by_message_thread_id(message.message_thread_id)
    if not user_data:
        return

    manager.text_message.language_code = user_data.language_code or manager.config.bot.DEFAULT_LANGUAGE
    is_ru = manager.text_message.language_code == "ru"
    logger.info("cmd /info | thread_id=%s user=%s", message.message_thread_id, user_data.id)

    username = (
        f"@{user_data.username.lstrip('@')}"
        if user_data.username and user_data.username != "-"
        else ("No username" if not is_ru else "Нет юзернейма")
    )

    banned_label = ("Yes" if not is_ru else "Да") if user_data.is_banned else ("No" if not is_ru else "Нет")
    created_at = getattr(user_data, "created_at", None) or "—"

    text = (
        f"<b>{'User info' if not is_ru else 'Инфо о пользователе'}</b>\n"
        f"TG ID: <code>{user_data.id}</code>\n"
        f"{'Name' if not is_ru else 'Имя'}: {html.escape(user_data.full_name or '—')}\n"
        f"Username: {html.escape(username)}\n"
        f"{'Language' if not is_ru else 'Язык'}: {html.escape(user_data.language_code or '—')}\n"
        f"{'Banned' if not is_ru else 'Заблокирован'}: {banned_label}\n"
        f"{'Topic created' if not is_ru else 'Топик создан'}: {html.escape(str(created_at))}\n"
    )
    await message.reply(text, disable_notification=True)


# ---------------------------------------------------------------------------
# /archive
# ---------------------------------------------------------------------------

@router.message(Command("archive"))
async def handle_archive(message: Message, manager: Manager, redis: RedisStorage) -> None:
    """Export the dialogue with this user as a plain-text .txt file.

    Retrieves the last 200 messages from the local SQLite dialogue archive
    and sends them as a file attachment in the topic.
    """
    if not await _ensure_support_access(message, manager):
        return

    user_data = await redis.get_by_message_thread_id(message.message_thread_id)
    if not user_data:
        return

    manager.text_message.language_code = user_data.language_code or manager.config.bot.DEFAULT_LANGUAGE
    is_ru = manager.text_message.language_code == "ru"
    logger.info("cmd /archive | thread_id=%s user=%s", message.message_thread_id, user_data.id)

    ack = await message.reply(
        "⏳ Формирую архив диалога…" if is_ru else "⏳ Building dialogue archive…",
        disable_notification=True,
    )

    try:
        has_messages = await dialogue_store.has_any_messages(user_data.id)
    except Exception as ex:
        with suppress(TelegramBadRequest):
            await ack.delete()
        await message.reply(
            f"{'❌ Не удалось проверить архив' if is_ru else '❌ Failed to check archive'}: "
            f"<code>{html.escape(str(ex))}</code>",
            disable_notification=True,
        )
        return

    if not has_messages:
        with suppress(TelegramBadRequest):
            await ack.delete()
        await message.reply(
            "❔ Архив диалога не найден" if is_ru else "❔ Dialogue archive not found",
            disable_notification=True,
        )
        return

    title = (
        f"📎 Архив диалога с {user_data.full_name} (последние 200 сообщений)"
        if is_ru
        else f"📎 Dialogue archive with {user_data.full_name} (last 200 messages)"
    )
    try:
        await dialogue_store.send_recent_transcript(
            bot=message.bot,
            user_id=user_data.id,
            chat_id=message.chat.id,
            message_thread_id=message.message_thread_id,
            title=title,
            limit=200,
            disable_notification=True,
        )
    except Exception as ex:
        await message.reply(
            f"{'❌ Не удалось отправить архив' if is_ru else '❌ Failed to send archive'}: "
            f"<code>{html.escape(str(ex))}</code>",
            disable_notification=True,
        )
    finally:
        with suppress(TelegramBadRequest):
            await ack.delete()


# ---------------------------------------------------------------------------
# /ban
# ---------------------------------------------------------------------------

@router.message(Command("ban"))
async def handle_ban(message: Message, manager: Manager, redis: RedisStorage) -> None:
    """Toggle ban status for the user associated with this topic.

    When banned, incoming messages from the user are silently ignored.
    The command toggles the state on each call.
    """
    if not await _ensure_support_access(message, manager):
        return

    user_data = await redis.get_by_message_thread_id(message.message_thread_id)
    if not user_data:
        return

    if user_data.is_banned:
        user_data.is_banned = False
        text = manager.text_message.get("user_unblocked")
    else:
        user_data.is_banned = True
        text = manager.text_message.get("user_blocked")

    logger.info(
        "cmd /ban | thread_id=%s user=%s from_user=%s is_banned=%s",
        message.message_thread_id, user_data.id,
        message.from_user.id if message.from_user else None,
        user_data.is_banned,
    )
    await message.reply(text, disable_notification=True)
    await redis.update_user(user_data.id, user_data)
