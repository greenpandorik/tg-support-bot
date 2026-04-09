import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


class CleanupStatsStore:
    """
    SQLite-backed store for daily cleanup statistics.

    Tracks success/failed counts per action type per UTC day.
    Uses the same SQLite file as user_state_store (different table).
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
                CREATE TABLE IF NOT EXISTS cleanup_stats (
                    date   TEXT NOT NULL,
                    action TEXT NOT NULL,
                    success INTEGER NOT NULL DEFAULT 0,
                    failed  INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (date, action)
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    async def flush(
        self,
        *,
        date_str: str,
        stats: dict[str, dict[str, int]],
    ) -> None:
        """
        Persist a batch of counters to the DB.

        stats format: {"action_name": {"success": N, "failed": N}, ...}
        """
        if not stats:
            return
        await asyncio.to_thread(self._flush_sync, date_str, stats)

    def _flush_sync(self, date_str: str, stats: dict[str, dict[str, int]]) -> None:
        conn = sqlite3.connect(self.path)
        try:
            for action, counts in stats.items():
                success = counts.get("success", 0)
                failed = counts.get("failed", 0)
                if success == 0 and failed == 0:
                    continue
                conn.execute(
                    """
                    INSERT INTO cleanup_stats(date, action, success, failed)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(date, action) DO UPDATE SET
                        success = success + excluded.success,
                        failed  = failed  + excluded.failed
                    """,
                    (date_str, action, success, failed),
                )
            conn.commit()
        finally:
            conn.close()

    async def get_stats(self, *, days: int = 30) -> list[dict]:
        """Return per-day, per-action stats for the last N days."""
        return await asyncio.to_thread(self._get_stats_sync, days)

    def _get_stats_sync(self, days: int) -> list[dict]:
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.execute(
                """
                SELECT date, action, success, failed
                FROM cleanup_stats
                WHERE date >= date('now', ? || ' days')
                ORDER BY date DESC, action ASC
                """,
                (f"-{days}",),
            )
            return [
                {"date": r[0], "action": r[1], "success": r[2], "failed": r[3]}
                for r in cur.fetchall()
            ]
        finally:
            conn.close()


cleanup_stats_store = CleanupStatsStore()
