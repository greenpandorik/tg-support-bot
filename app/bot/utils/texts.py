from abc import abstractmethod, ABCMeta

from aiogram.utils.markdown import hbold

SUPPORTED_LANGUAGES = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
}


class Text(metaclass=ABCMeta):
    """Abstract base class for language-aware text containers."""

    def __init__(self, language_code: str) -> None:
        """
        :param language_code: ISO 639-1 code, e.g. "ru" or "en".
                              Falls back to "en" if the code is not in SUPPORTED_LANGUAGES.
        """
        self.language_code = language_code if language_code in SUPPORTED_LANGUAGES else "en"

    @property
    @abstractmethod
    def data(self) -> dict:
        """Language-keyed dictionary of text strings."""
        raise NotImplementedError

    def get(self, code: str) -> str:
        """Return the text string for *code* in the current language."""
        return self.data[self.language_code][code]


class TextMessage(Text):
    """All user-facing and support-facing text strings, in all supported languages."""

    @property
    def data(self) -> dict:
        return {
            "en": {
                "select_language": f"👋 <b>Hello</b>, {hbold('{full_name}')}!\n\nSelect language:",
                "change_language": "<b>Select language:</b>",
                "main_menu": "<b>Write your question</b> and we will answer as soon as possible:",
                "message_sent": "<b>Message sent!</b> Expect a response.",
                "message_edited": (
                    "<b>The message was edited only in your chat.</b> "
                    "To resend an edited message, send it as a new one."
                ),
                "message_protected": (
                    "<b>Message received!</b> You have forwarding restrictions enabled, "
                    "so support will only see the message metadata."
                ),
                "source": (
                    "Source code available on "
                    '<a href="https://github.com/nessshon/support-bot">GitHub</a>'
                ),
                "support_commands": (
                    "<b>Commands</b>\n"
                    "\n"
                    "<b>ℹ️ Info</b>\n"
                    "- /info — Show user info card\n"
                    "- /archive — Export dialogue as .txt\n"
                    "\n"
                    "<b>🔧 Moderation</b>\n"
                    "- /ban — Block / Unblock user\n"
                    "- /silent — Toggle silent mode (mute / unmute)\n"
                    "- /close — Mark topic as answered by manager\n"
                    "\n"
                    "<b>🛠 Misc</b>\n"
                    "- /id — Show current chat ID\n"
                    "- /help — This list\n"
                ),
                "user_started_bot": (
                    "TG ID: {tg_id_line}\n"
                    "Username: {username}\n"
                    "{commands_block}"
                ),
                "user_restarted_bot": f"User {hbold('{name}')} restarted the bot.",
                "user_stopped_bot": f"User {hbold('{name}')} stopped the bot.",
                "user_blocked_bot": f"User {hbold('{name}')} blocked the bot.",
                "user_blocked": "<b>User blocked.</b> Messages from this user are no longer accepted.",
                "user_unblocked": "<b>User unblocked.</b> Messages from this user are accepted again.",
                "blocked_by_user": "<b>Message not sent.</b> The bot has been blocked by the user.",
                "user_information": (
                    "<b>ID:</b> <code>{id}</code>\n"
                    "<b>Name:</b> {full_name}\n"
                    "<b>Username:</b> {username}\n"
                    "<b>Banned:</b> {is_banned}\n"
                    "<b>Registered:</b> {created_at}"
                ),
                "message_not_sent": "<b>Message not sent.</b> An unexpected error occurred.",
                "message_sent_to_user": "<b>Message sent to user.</b>",
                "silent_mode_enabled": (
                    "<b>Silent mode ON.</b> Messages will not be delivered to the user."
                ),
                "silent_mode_disabled": (
                    "<b>Silent mode OFF.</b> The user will receive all messages."
                ),
            },
            "ru": {
                "select_language": f"👋 <b>Привет</b>, {hbold('{full_name}')}!\n\nВыберите язык:",
                "change_language": "<b>Выберите язык:</b>",
                "main_menu": "<b>Напишите свой вопрос</b>, и мы ответим в ближайшее время:",
                "message_sent": "<b>Сообщение отправлено!</b> Ожидайте ответа.",
                "message_edited": (
                    "<b>Сообщение отредактировано только в вашем чате.</b> "
                    "Чтобы отправить отредактированное сообщение — отправьте его заново."
                ),
                "message_protected": (
                    "<b>Сообщение получено!</b> У вас включена защита от пересылки, "
                    "поэтому поддержка увидит только метаданные сообщения."
                ),
                "source": (
                    "Исходный код доступен на "
                    '<a href="https://github.com/nessshon/support-bot">GitHub</a>'
                ),
                "support_commands": (
                    "<b>Команды</b>\n"
                    "\n"
                    "<b>ℹ️ Информация</b>\n"
                    "- /info — Карточка пользователя\n"
                    "- /archive — Архив диалога (.txt)\n"
                    "\n"
                    "<b>🔧 Модерация</b>\n"
                    "- /ban — Заблокировать / разблокировать\n"
                    "- /silent — Тихий режим (мут / размут)\n"
                    "- /close — Отметить ответ менеджера\n"
                    "\n"
                    "<b>🛠 Разное</b>\n"
                    "- /id — ID текущего чата\n"
                    "- /help — Этот список\n"
                ),
                "user_started_bot": (
                    "TG ID: {tg_id_line}\n"
                    "Юзернейм: {username}\n"
                    "{commands_block}"
                ),
                "user_restarted_bot": f"Пользователь {hbold('{name}')} перезапустил(а) бота.",
                "user_stopped_bot": f"Пользователь {hbold('{name}')} остановил(а) бота.",
                "user_blocked_bot": f"Пользователь {hbold('{name}')} заблокировал(а) бота.",
                "user_blocked": "<b>Пользователь заблокирован.</b> Сообщения от него не принимаются.",
                "user_unblocked": "<b>Пользователь разблокирован.</b> Сообщения от него снова принимаются.",
                "blocked_by_user": "<b>Сообщение не отправлено.</b> Бот заблокирован пользователем.",
                "user_information": (
                    "<b>ID:</b> <code>{id}</code>\n"
                    "<b>Имя:</b> {full_name}\n"
                    "<b>Username:</b> {username}\n"
                    "<b>Заблокирован:</b> {is_banned}\n"
                    "<b>Зарегистрирован:</b> {created_at}"
                ),
                "message_not_sent": "<b>Сообщение не отправлено.</b> Произошла неожиданная ошибка.",
                "message_sent_to_user": "<b>Сообщение отправлено пользователю.</b>",
                "silent_mode_enabled": (
                    "<b>Тихий режим включён.</b> Сообщения не будут доставлены пользователю."
                ),
                "silent_mode_disabled": (
                    "<b>Тихий режим выключен.</b> Пользователь снова получает все сообщения."
                ),
            },
        }
