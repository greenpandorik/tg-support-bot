import asyncio
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from aiogram import Bot
from aiogram.types import FSInputFile, Message


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


@dataclass(frozen=True)
class DialogueRow:
    created_at: str
    direction: str  # "user" | "manager"
    kind: str  # "text" | "media" | "album"
    text: str
    author_tg_id: int | None
    author_username: str | None


class DialogueStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.getenv("DIALOGUE_SQLITE_PATH", ".data/dialogues.sqlite3")

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _init_sync(self) -> None:
        _ensure_parent_dir(self.path)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dialogue_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    created_at TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    author_tg_id INTEGER,
                    author_username TEXT
                )
                """
            )
            self._ensure_column(conn, "dialogue_messages", "author_tg_id", "INTEGER")
            self._ensure_column(conn, "dialogue_messages", "author_username", "TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dialogue_user_created ON dialogue_messages(user_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dialogue_user_thread ON dialogue_messages(user_id, thread_id)"
            )
            conn.commit()
        finally:
            conn.close()

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        cur = conn.execute(f"PRAGMA table_info({table})")
        columns = {str(row[1]) for row in cur.fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    async def add(
        self,
        *,
        user_id: int,
        thread_id: int | None,
        direction: str,
        kind: str,
        text: str,
        created_at: str | None = None,
        author_tg_id: int | None = None,
        author_username: str | None = None,
    ) -> None:
        created_at = created_at or utc_now_iso()
        await asyncio.to_thread(
            self._add_sync,
            user_id,
            thread_id,
            created_at,
            direction,
            kind,
            text,
            author_tg_id,
            author_username,
        )

    def _add_sync(
        self,
        user_id: int,
        thread_id: int | None,
        created_at: str,
        direction: str,
        kind: str,
        text: str,
        author_tg_id: int | None,
        author_username: str | None,
    ) -> None:
        if not text or not text.strip():
            return
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                """
                INSERT INTO dialogue_messages(
                    user_id, thread_id, created_at, direction, kind, text, author_tg_id, author_username
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    thread_id,
                    created_at,
                    direction,
                    kind,
                    text.strip(),
                    author_tg_id,
                    (author_username or "").lstrip("@") or None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def has_any_messages(self, user_id: int) -> bool:
        return await asyncio.to_thread(self._has_any_messages_sync, user_id)

    def _has_any_messages_sync(self, user_id: int) -> bool:
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.execute(
                "SELECT 1 FROM dialogue_messages WHERE user_id = ? LIMIT 1",
                (user_id,),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()

    async def get_recent(self, user_id: int, limit: int = 200) -> list[DialogueRow]:
        return await asyncio.to_thread(self._get_recent_sync, user_id, limit)

    def _get_recent_sync(self, user_id: int, limit: int) -> list[DialogueRow]:
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.execute(
                """
                SELECT created_at, direction, kind, text, author_tg_id, author_username
                FROM dialogue_messages
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            rows = [DialogueRow(*row) for row in cur.fetchall()]
            rows.reverse()
            return rows
        finally:
            conn.close()

    async def send_recent_transcript(
        self,
        *,
        bot: Bot,
        user_id: int,
        chat_id: int,
        message_thread_id: int | None,
        title: str,
        limit: int = 200,
        disable_notification: bool | None = None,
    ) -> None:
        rows = await self.get_recent(user_id, limit=limit)
        if not rows:
            return

        content = self._format_transcript(title, rows)
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as tmp:
            tmp.write(content)
            file_path = tmp.name

        try:
            kwargs = {}
            if message_thread_id:
                kwargs["message_thread_id"] = message_thread_id
            if disable_notification is not None:
                kwargs["disable_notification"] = disable_notification
            await bot.send_document(
                chat_id=chat_id,
                document=FSInputFile(file_path, filename="dialogue_archive.txt"),
                caption=title,
                **kwargs,
            )
        finally:
            try:
                os.unlink(file_path)
            except OSError:
                pass

    def _format_transcript(self, title: str, rows: Iterable[DialogueRow]) -> str:
        lines: list[str] = [title, ""]
        for row in rows:
            who = "USER" if row.direction == "user" else "MANAGER"
            author_parts: list[str] = []
            if row.author_tg_id is not None:
                author_parts.append(f"tg_id={row.author_tg_id}")
            if row.author_username:
                author_parts.append(f"@{row.author_username.lstrip('@')}")
            author_suffix = f" [{' | '.join(author_parts)}]" if author_parts else ""
            lines.append(f"[{row.created_at}] {who}{author_suffix} ({row.kind}): {row.text}")
        lines.append("")
        return "\n".join(lines)


dialogue_store = DialogueStore()
