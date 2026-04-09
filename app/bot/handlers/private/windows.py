from contextlib import suppress

from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hbold

from app.bot.manager import Manager
from aiogram.types import InlineKeyboardMarkup as Markup
from aiogram.types import InlineKeyboardButton as Button
from app.bot.utils.texts import SUPPORTED_LANGUAGES


def select_language_markup() -> Markup:
    """Build the language selection inline keyboard."""
    builder = InlineKeyboardBuilder().row(
        *[Button(text=text, callback_data=code) for code, text in SUPPORTED_LANGUAGES.items()],
        width=2,
    )
    return builder.as_markup()


class Window:
    """Static window factories — each method sends (or edits) a bot message."""

    @staticmethod
    async def select_language(manager: Manager) -> None:
        """Show the initial language selection screen."""
        text = manager.text_message.get("select_language")
        with suppress(IndexError, KeyError):
            text = text.format(full_name=hbold(manager.user.full_name))
        await manager.send_message(text, reply_markup=select_language_markup())

    @staticmethod
    async def main_menu(manager: Manager, **_) -> None:
        """Show the main support menu (prompt the user to write their question)."""
        text = manager.text_message.get("main_menu")
        with suppress(IndexError, KeyError):
            text = text.format(full_name=hbold(manager.user.full_name))
        await manager.send_message(text)
        await manager.state.set_state(None)

    @staticmethod
    async def change_language(manager: Manager) -> None:
        """Show the language change screen."""
        text = manager.text_message.get("change_language")
        await manager.send_message(text, reply_markup=select_language_markup())
