from dataclasses import dataclass

from environs import Env


@dataclass
class BotConfig:
    """
    Bot configuration.

    Attributes:
    - TOKEN (str): Telegram bot token.
    - DEV_ID (int): Developer's Telegram user ID (used for admin commands and error reports).
    - GROUP_ID (int): Forum group chat ID where support topics are created.
    - BOT_EMOJI_ID (str): Custom emoji ID for the bot's default topic icon.
    - START_EMOJI_ID (str): Custom emoji ID for newly created topics (no messages yet).
    - BAN_EMOJI_ID (str): Custom emoji ID for topics where the user is banned.
    - MANAGER_EMOJI_ID (str): Custom emoji ID indicating a manager has replied.
    - TOPIC_FIXED_EMOJI_ID (str): Fixed emoji ID used for all topics when status-in-title mode is enabled.
    - TOPIC_STATUS_IN_TITLE (bool): If true, status prefix is shown in topic name instead of changing the icon.
    - TOPIC_STATUS_EMOJI_START (str): Emoji prefix for "new" status in topic name.
    - TOPIC_STATUS_EMOJI_USER (str): Emoji prefix for "user replied" status in topic name.
    - TOPIC_STATUS_EMOJI_MANAGER (str): Emoji prefix for "manager replied" status in topic name.
    - TOPIC_STATUS_EMOJI_BAN (str): Emoji prefix for "banned" status in topic name.
    - MULTI_LANGUAGE (bool): Enable multi-language support (ru/en).
    - DEFAULT_LANGUAGE (str): Fallback language when multi-language is disabled.
    """
    TOKEN: str
    DEV_ID: int
    GROUP_ID: int
    BOT_EMOJI_ID: str
    START_EMOJI_ID: str
    BAN_EMOJI_ID: str
    MANAGER_EMOJI_ID: str
    TOPIC_FIXED_EMOJI_ID: str
    TOPIC_STATUS_IN_TITLE: bool
    TOPIC_STATUS_EMOJI_START: str
    TOPIC_STATUS_EMOJI_USER: str
    TOPIC_STATUS_EMOJI_MANAGER: str
    TOPIC_STATUS_EMOJI_BAN: str
    MULTI_LANGUAGE: bool
    DEFAULT_LANGUAGE: str


@dataclass
class RedisConfig:
    """
    Redis connection configuration.

    Attributes:
    - HOST (str): Redis host.
    - PORT (int): Redis port.
    - DB (int): Redis database index.
    """
    HOST: str
    PORT: int
    DB: int

    def dsn(self) -> str:
        """Return Redis connection DSN string."""
        return f"redis://{self.HOST}:{self.PORT}/{self.DB}"


@dataclass
class Config:
    """
    Root application configuration.

    Attributes:
    - bot (BotConfig): Bot settings.
    - redis (RedisConfig): Redis settings.
    """
    bot: BotConfig
    redis: RedisConfig


def load_config() -> Config:
    """Load configuration from environment variables and return a Config instance."""
    env = Env()
    env.read_env()

    bot_emoji_id = env.str("BOT_EMOJI_ID")
    start_emoji_id = env.str("START_EMOJI_ID", bot_emoji_id)
    ban_emoji_id = env.str("BAN_EMOJI_ID", "")

    return Config(
        bot=BotConfig(
            TOKEN=env.str("BOT_TOKEN"),
            DEV_ID=env.int("BOT_DEV_ID"),
            GROUP_ID=env.int("BOT_GROUP_ID"),
            BOT_EMOJI_ID=bot_emoji_id,
            START_EMOJI_ID=start_emoji_id,
            BAN_EMOJI_ID=ban_emoji_id,
            MANAGER_EMOJI_ID=env.str("MANAGER_EMOJI_ID"),
            TOPIC_FIXED_EMOJI_ID=env.str("TOPIC_FIXED_EMOJI_ID", ""),
            TOPIC_STATUS_IN_TITLE=env.bool("TOPIC_STATUS_IN_TITLE", False),
            TOPIC_STATUS_EMOJI_START=env.str("TOPIC_STATUS_EMOJI_START", "🆕"),
            TOPIC_STATUS_EMOJI_USER=env.str("TOPIC_STATUS_EMOJI_USER", "✅"),
            TOPIC_STATUS_EMOJI_MANAGER=env.str("TOPIC_STATUS_EMOJI_MANAGER", "🤖"),
            TOPIC_STATUS_EMOJI_BAN=env.str("TOPIC_STATUS_EMOJI_BAN", "⛔"),
            MULTI_LANGUAGE=env.bool("MULTI_LANGUAGE", True),
            DEFAULT_LANGUAGE=env.str("DEFAULT_LANGUAGE", "ru"),
        ),
        redis=RedisConfig(
            HOST=env.str("REDIS_HOST"),
            PORT=env.int("REDIS_PORT"),
            DB=env.int("REDIS_DB"),
        ),
    )
