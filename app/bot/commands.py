from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)

from app.bot.utils.texts import SUPPORTED_LANGUAGES
from app.config import Config


async def setup(bot: Bot, config: Config) -> None:
    """
    Register bot commands for all relevant scopes and languages.

    :param bot: Bot instance.
    :param config: Application config.
    """
    private_commands = {
        "en": [BotCommand(command="start", description="Restart bot")],
        "ru": [BotCommand(command="start", description="Перезапустить бота")],
    }

    group_commands = {
        "en": [
            BotCommand(command="ban", description="Block / Unblock user"),
            BotCommand(command="silent", description="Toggle silent mode"),
            BotCommand(command="close", description="Mark topic as answered by manager"),
            BotCommand(command="info", description="Show user info"),
            BotCommand(command="archive", description="Export dialogue as .txt"),
            BotCommand(command="help", description="Command list"),
        ],
        "ru": [
            BotCommand(command="ban", description="Заблокировать / Разблокировать пользователя"),
            BotCommand(command="silent", description="Включить / Выключить тихий режим"),
            BotCommand(command="close", description="Отметить ответ менеджера"),
            BotCommand(command="info", description="Информация о пользователе"),
            BotCommand(command="archive", description="Архив диалога (.txt)"),
            BotCommand(command="help", description="Список команд"),
        ],
    }

    await bot.set_my_commands(
        commands=private_commands["en"],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        commands=private_commands["ru"],
        scope=BotCommandScopeAllPrivateChats(),
        language_code="ru",
    )
    await bot.set_my_commands(
        commands=group_commands["en"],
        scope=BotCommandScopeAllGroupChats(),
    )
    await bot.set_my_commands(
        commands=group_commands["ru"],
        scope=BotCommandScopeAllGroupChats(),
        language_code="ru",
    )


async def delete(bot: Bot, config: Config) -> None:
    """
    Remove all registered bot commands.

    :param bot: Bot instance.
    :param config: Application config.
    """
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=config.bot.DEV_ID))
        await bot.delete_my_commands(
            scope=BotCommandScopeChat(chat_id=config.bot.DEV_ID), language_code="ru"
        )
    except TelegramBadRequest:
        raise ValueError(f"Chat with DEV_ID {config.bot.DEV_ID} not found.")

    await bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
    await bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats(), language_code="ru")
    await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
    await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats(), language_code="ru")
