import asyncio
import contextlib
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import Message

logger = logging.getLogger(__name__)


def _build_emoji_reaction(emoji: str) -> list[Any]:
    try:
        from aiogram.types import ReactionTypeEmoji

        return [ReactionTypeEmoji(emoji=emoji)]
    except Exception:
        return [{"type": "emoji", "emoji": emoji}]


async def set_message_reaction_safe(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    emoji: str,
    is_big: bool = False,
    max_attempts: int = 2,
) -> bool:
    if max_attempts < 1:
        max_attempts = 1

    reaction = _build_emoji_reaction(emoji)

    for attempt in range(1, max_attempts + 1):
        try:
            await bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=reaction,
                is_big=is_big,
            )
            return True

        except TelegramRetryAfter as ex:
            if attempt >= max_attempts:
                logger.warning(
                    "set_message_reaction rate-limited | chat=%s message_id=%s retry_after=%s",
                    chat_id,
                    message_id,
                    ex.retry_after,
                )
                return False
            await asyncio.sleep(ex.retry_after)

        except TelegramBadRequest as ex:
            logger.debug(
                "set_message_reaction bad request | chat=%s message_id=%s err=%s",
                chat_id,
                message_id,
                ex.message,
            )
            return False

        except TelegramAPIError as ex:
            logger.debug(
                "set_message_reaction api error | chat=%s message_id=%s err=%s",
                chat_id,
                message_id,
                ex,
            )
            return False

        except Exception as ex:
            logger.debug(
                "set_message_reaction unexpected error | chat=%s message_id=%s err=%s",
                chat_id,
                message_id,
                ex,
            )
            return False

    return False


async def pin_message_safe(
    message: Message,
    *,
    disable_notification: bool | None = None,
    max_attempts: int = 2,
) -> bool:
    if max_attempts < 1:
        max_attempts = 1

    for attempt in range(1, max_attempts + 1):
        try:
            if disable_notification is None:
                await message.pin()
            else:
                await message.pin(disable_notification=disable_notification)
            return True

        except TelegramRetryAfter as ex:
            if attempt >= max_attempts:
                logger.warning(
                    "pin_message rate-limited | chat=%s message_id=%s retry_after=%s",
                    message.chat.id,
                    message.message_id,
                    ex.retry_after,
                )
                return False
            await asyncio.sleep(ex.retry_after)

        except TelegramBadRequest as ex:
            msg = (ex.message or "").lower()
            if "message is already pinned" in msg or "message not modified" in msg:
                return True
            logger.debug(
                "pin_message bad request | chat=%s message_id=%s err=%s",
                message.chat.id,
                message.message_id,
                ex.message,
            )
            return False

        except TelegramAPIError as ex:
            logger.debug(
                "pin_message api error | chat=%s message_id=%s err=%s",
                message.chat.id,
                message.message_id,
                ex,
            )
            return False

        except Exception as ex:
            logger.debug(
                "pin_message unexpected error | chat=%s message_id=%s err=%s",
                message.chat.id,
                message.message_id,
                ex,
            )
            return False

    return False


async def ack_message(
    message: Message,
    *,
    ok: bool,
    ok_emoji: str = "👍",
    fail_emoji: str = "👎",
    fallback_ok_text: str | None = None,
    fallback_fail_text: str | None = None,
    delete_after_seconds: float = 5.0,
    disable_notification: bool = True,
) -> None:
    if ok:
        reacted = await set_message_reaction_safe(
            message.bot,
            chat_id=message.chat.id,
            message_id=message.message_id,
            emoji=ok_emoji,
        )
        if reacted:
            return
        text = fallback_ok_text
    else:
        # On failure, keep old behavior: send a short status message instead of reacting.
        text = fallback_fail_text
        if not text and fail_emoji:
            await set_message_reaction_safe(
                message.bot,
                chat_id=message.chat.id,
                message_id=message.message_id,
                emoji=fail_emoji,
            )
            return

    if not text:
        return

    try:
        reply = await message.reply(text, disable_notification=disable_notification)
        await asyncio.sleep(delete_after_seconds)
        with contextlib.suppress(Exception):
            await reply.delete()
    except Exception:
        logger.debug("ack_message fallback reply failed", exc_info=True)
