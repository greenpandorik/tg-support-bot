from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta


@dataclass
class UserData:
    """Per-user state stored in Redis (and backed up to SQLite via user_state_store).

    Fields:
    - message_thread_id: Forum topic thread ID in the support group.
    - message_silent_id: Message ID of the pinned silent-mode notice (None when inactive).
    - message_silent_mode: Whether silent mode is currently active for this user.
    - id: Telegram user ID.
    - full_name: User's full name at the time of last interaction.
    - username: Telegram username (without @), or None.
    - topic_start_message_id: Message ID of the pinned info card in the topic.
    - topic_icon_state: Current visual state of the topic icon/title prefix.
                        One of: "start" | "user" | "manager" | "ban".
    - state: Telegram chat member status ("member", "kicked", etc.).
    - is_banned: Whether the user is blocked from messaging support.
    - language_code: User's selected UI language ("ru" or "en").
    - created_at: ISO timestamp of first bot interaction.
    - last_activity_at: ISO timestamp of last user message.
    - topic_created_at: ISO timestamp of current topic creation.
    - blocked_at: ISO timestamp when the user blocked the bot.
    - archive_sent_at: ISO timestamp when the dialogue archive was last sent into the topic.
    - archive_restore_pending: If True, the archive should be restored into the next topic after unban.
    """
    message_thread_id: int | None
    message_silent_id: int | None
    message_silent_mode: bool

    id: int
    full_name: str
    username: str | None
    topic_start_message_id: int | None = None
    topic_icon_state: str | None = None
    state: str = "member"
    is_banned: bool = False
    language_code: str | None = None
    created_at: str = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S %Z")
    last_activity_at: str | None = None
    topic_created_at: str | None = None
    blocked_at: str | None = None
    archive_sent_at: str | None = None
    archive_restore_pending: bool = False

    def to_dict(self) -> dict:
        """Return a plain dictionary representation of this object."""
        return asdict(self)
