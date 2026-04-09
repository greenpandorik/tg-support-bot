import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.bot.utils.redis.models import UserData

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


class UserStateStore:
    """
    SQLite-backed source of truth for user/topic state.

    Stores a JSON blob of UserData (data_json), plus a few extracted columns for fast lookups/cleanup queries.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.getenv("USER_STATE_SQLITE_PATH", ".data/user_state.sqlite3")

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _init_sync(self) -> None:
        _ensure_parent_dir(self.path)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=3000;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_state (
                    id INTEGER PRIMARY KEY,
                    message_thread_id INTEGER,
                    state TEXT,
                    blocked_at TEXT,
                    last_activity_at TEXT,
                    topic_created_at TEXT,
                    subscription_checked_at TEXT,
                    archived_at TEXT,
                    updated_at TEXT NOT NULL,
                    data_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_state_thread_id ON user_state(message_thread_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_state_state_blocked ON user_state(state, blocked_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_state_last_activity ON user_state(last_activity_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_state_topic_created ON user_state(topic_created_at)"
            )
            conn.commit()
        finally:
            conn.close()

    async def count_users(self) -> int:
        return await asyncio.to_thread(self._count_users_sync)

    def _count_users_sync(self) -> int:
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.execute("SELECT COUNT(1) FROM user_state")
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    async def upsert_user(self, user_data: UserData) -> None:
        payload: dict[str, Any] = user_data.to_dict()
        data_json = json.dumps(payload, ensure_ascii=False)
        await asyncio.to_thread(
            self._upsert_user_sync,
            user_data.id,
            user_data.message_thread_id,
            str(payload.get("state") or ""),
            payload.get("blocked_at"),
            payload.get("last_activity_at"),
            payload.get("topic_created_at"),
            utc_now_iso(),
            data_json,
        )

    def _upsert_user_sync(
        self,
        user_id: int,
        message_thread_id: int | None,
        state: str,
        blocked_at: str | None,
        last_activity_at: str | None,
        topic_created_at: str | None,
        updated_at: str,
        data_json: str,
    ) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                """
                INSERT INTO user_state(
                    id,
                    message_thread_id,
                    state,
                    blocked_at,
                    last_activity_at,
                    topic_created_at,
                    updated_at,
                    data_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    message_thread_id=excluded.message_thread_id,
                    state=excluded.state,
                    blocked_at=excluded.blocked_at,
                    last_activity_at=excluded.last_activity_at,
                    topic_created_at=excluded.topic_created_at,
                    updated_at=excluded.updated_at,
                    data_json=excluded.data_json
                """,
                (
                    user_id,
                    message_thread_id,
                    state or None,
                    blocked_at,
                    last_activity_at,
                    topic_created_at,
                    updated_at,
                    data_json,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def get_user(self, user_id: int) -> UserData | None:
        raw = await asyncio.to_thread(self._get_user_json_sync, user_id)
        if not raw:
            return None
        try:
            decoded = json.loads(raw)
            return UserData(**decoded)
        except Exception as ex:
            logger.warning("Failed to decode user_state from sqlite | user=%s err=%s", user_id, ex)
            return None

    def _get_user_json_sync(self, user_id: int) -> str | None:
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.execute("SELECT data_json FROM user_state WHERE id = ? LIMIT 1", (user_id,))
            row = cur.fetchone()
            return str(row[0]) if row else None
        finally:
            conn.close()

    async def get_user_by_message_thread_id(self, message_thread_id: int) -> UserData | None:
        raw = await asyncio.to_thread(self._get_user_json_by_thread_sync, message_thread_id)
        if not raw:
            return None
        try:
            decoded = json.loads(raw)
            return UserData(**decoded)
        except Exception as ex:
            logger.warning(
                "Failed to decode user_state by thread_id from sqlite | thread_id=%s err=%s",
                message_thread_id,
                ex,
            )
            return None

    def _get_user_json_by_thread_sync(self, message_thread_id: int) -> str | None:
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.execute(
                "SELECT data_json FROM user_state WHERE message_thread_id = ? LIMIT 1",
                (message_thread_id,),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None
        finally:
            conn.close()

    async def get_all_user_ids(self) -> list[int]:
        return await asyncio.to_thread(self._get_all_user_ids_sync)

    def _get_all_user_ids_sync(self) -> list[int]:
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.execute("SELECT id FROM user_state ORDER BY id ASC")
            return [int(r[0]) for r in cur.fetchall()]
        finally:
            conn.close()

    async def get_blocked_user_ids(
        self,
        *,
        blocked_threshold_iso: str,
        limit: int | None = None,
    ) -> list[int]:
        sql = (
            "SELECT id FROM user_state "
            "WHERE message_thread_id IS NOT NULL "
            "AND state = 'kicked' "
            "AND blocked_at IS NOT NULL "
            "AND blocked_at <= ? "
            "ORDER BY blocked_at ASC"
        )
        params: tuple[Any, ...] = (blocked_threshold_iso,)
        return await asyncio.to_thread(self._select_ids_sync, sql, params, limit)

    async def get_inactive_user_ids(
        self,
        *,
        inactive_threshold_iso: str,
        limit: int | None = None,
    ) -> list[int]:
        sql = (
            "SELECT id FROM user_state "
            "WHERE message_thread_id IS NOT NULL "
            "AND (state IS NULL OR state != 'kicked') "
            "AND last_activity_at IS NOT NULL "
            "AND last_activity_at <= ? "
            "ORDER BY last_activity_at ASC"
        )
        params: tuple[Any, ...] = (inactive_threshold_iso,)
        return await asyncio.to_thread(self._select_ids_sync, sql, params, limit)

    async def get_empty_topic_user_ids(
        self,
        *,
        empty_threshold_iso: str,
        limit: int | None = None,
    ) -> list[int]:
        sql = (
            "SELECT id FROM user_state "
            "WHERE message_thread_id IS NOT NULL "
            "AND (state IS NULL OR state != 'kicked') "
            "AND last_activity_at IS NULL "
            "AND topic_created_at IS NOT NULL "
            "AND topic_created_at <= ? "
            "ORDER BY topic_created_at ASC"
        )
        params: tuple[Any, ...] = (empty_threshold_iso,)
        return await asyncio.to_thread(self._select_ids_sync, sql, params, limit)

    def _select_ids_sync(
        self,
        sql: str,
        params: tuple[Any, ...],
        limit: int | None,
    ) -> list[int]:
        conn = sqlite3.connect(self.path)
        try:
            if limit is not None and limit > 0:
                sql = f"{sql} LIMIT ?"
                params = (*params, int(limit))
            cur = conn.execute(sql, params)
            return [int(r[0]) for r in cur.fetchall()]
        finally:
            conn.close()


user_state_store = UserStateStore()

