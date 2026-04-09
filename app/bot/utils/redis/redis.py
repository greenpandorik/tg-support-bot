import json
import asyncio
import time
import uuid
import logging
import contextlib

from redis.asyncio import Redis

from app.bot.db.user_state_store import UserStateStore, user_state_store

from .models import UserData

logger = logging.getLogger(__name__)


class RedisStorage:
    """Class for managing user data storage using Redis."""

    NAME = "users"

    def __init__(self, redis: Redis, *, user_store: UserStateStore | None = None) -> None:
        """
        Initializes the RedisStorage instance.

        :param redis: The Redis instance to be used for data storage.
        """
        self.redis = redis
        self.user_store = user_store or user_state_store

    async def acquire_lock(
        self,
        key: str,
        *,
        ttl_seconds: int = 30,
        wait_seconds: float = 10.0,
        poll_interval: float = 0.2,
    ) -> str | None:
        """
        Best-effort distributed lock using SET NX EX.
        Returns lock token on success, None on timeout.
        """
        token = uuid.uuid4().hex
        deadline = time.monotonic() + wait_seconds

        while time.monotonic() < deadline:
            ok = await self.redis.set(name=key, value=token, nx=True, ex=ttl_seconds)
            if ok:
                return token
            await asyncio.sleep(poll_interval)

        return None

    async def try_acquire_lock(self, key: str, *, ttl_seconds: int = 30) -> str | None:
        """
        Single-attempt lock (SET NX EX). Returns token on success, None otherwise.
        """
        token = uuid.uuid4().hex
        try:
            ok = await self.redis.set(name=key, value=token, nx=True, ex=ttl_seconds)
            return token if ok else None
        except Exception:
            return None

    async def release_lock(self, key: str, token: str) -> None:
        """
        Safe unlock: delete only if token matches.
        """
        script = """
        if redis.call("GET", KEYS[1]) == ARGV[1] then
            return redis.call("DEL", KEYS[1])
        else
            return 0
        end
        """
        try:
            await self.redis.eval(script, numkeys=1, keys=[key], args=[token])
        except Exception:
            # Unlock failures should not crash handlers.
            pass

    async def get_value(self, key: str) -> str | None:
        async with self.redis.client() as client:
            value = await client.get(key)
            if value is None:
                return None
            if isinstance(value, bytes):
                return value.decode("utf-8")
            return str(value)

    async def set_value(self, key: str, value: str, *, ex_seconds: int | None = None) -> None:
        async with self.redis.client() as client:
            await client.set(key, value, ex=ex_seconds)

    async def _get(self, name: str, key: str | int) -> bytes | None:
        """
        Retrieves data from Redis.

        :param name: The name of the Redis hash.
        :param key: The key to be retrieved.
        :return: The retrieved data or None if not found.
        """
        async with self.redis.client() as client:
            return await client.hget(name, key)

    async def _set(self, name: str, key: str | int, value: any) -> None:
        """
        Sets data in Redis.

        :param name: The name of the Redis hash.
        :param key: The key to be set.
        :param value: The value to be set.
        """
        async with self.redis.client() as client:
            await client.hset(name, key, value)

    async def _update_index(self, message_thread_id: int | None, user_id: int) -> None:
        if not message_thread_id:
            return
        index_key = f"{self.NAME}_index_{message_thread_id}"
        await self._set(index_key, user_id, "1")

    async def get_by_message_thread_id(self, message_thread_id: int) -> UserData | None:
        """
        Retrieves user data based on message thread ID.

        :param message_thread_id: The ID of the message thread.
        :return: The user data or None if not found.
        """
        user_id = await self._get_user_id_by_message_thread_id(message_thread_id)
        if user_id is not None:
            user = await self.get_user(user_id)
            if user is not None:
                return user

        # Fallback: Redis index can be missing after restarts/flushes; use SQLite source of truth.
        if self.user_store:
            try:
                user = await self.user_store.get_user_by_message_thread_id(message_thread_id)
            except Exception as ex:
                logger.warning(
                    "Failed to read user_state by thread_id from sqlite | thread_id=%s err=%s",
                    message_thread_id,
                    ex,
                )
                user = None
            if user is not None:
                with contextlib.suppress(Exception):
                    await self._cache_user(user)
                return user

        return None

    async def _get_user_id_by_message_thread_id(self, message_thread_id: int) -> int | None:
        """
        Retrieves user ID based on message thread ID.

        :param message_thread_id: The ID of the message thread.
        :return: The user ID or None if not found.
        """
        index_key = f"{self.NAME}_index_{message_thread_id}"
        async with self.redis.client() as client:
            user_ids = await client.hkeys(index_key)
            return int(user_ids[0]) if user_ids else None

    async def _get_user_redis_only(self, id_: int) -> UserData | None:
        data = await self._get(self.NAME, id_)
        if data is None:
            return None
        try:
            decoded_data = json.loads(data)
            return UserData(**decoded_data)
        except Exception as ex:
            logger.warning("Failed to decode user from redis | user=%s err=%s", id_, ex)
            return None

    async def _cache_user(self, user: UserData) -> None:
        json_data = json.dumps(user.to_dict())
        await self._set(self.NAME, user.id, json_data)
        if user.message_thread_id:
            await self._update_index(user.message_thread_id, user.id)

    async def get_user(self, id_: int) -> UserData | None:
        """
        Retrieves user data based on user ID.

        :param id_: The ID of the user.
        :return: The user data or None if not found.
        """
        try:
            user = await self._get_user_redis_only(id_)
        except Exception as ex:
            logger.warning("Failed to read user from redis | user=%s err=%s", id_, ex)
            user = None
        if user is not None:
            return user

        # Redis miss -> try SQLite source of truth.
        if self.user_store:
            try:
                user = await self.user_store.get_user(id_)
            except Exception as ex:
                logger.warning("Failed to read user_state from sqlite | user=%s err=%s", id_, ex)
                user = None
            if user is not None:
                with contextlib.suppress(Exception):
                    await self._cache_user(user)
                return user

        return None

    async def update_user(self, id_: int, data: UserData) -> None:
        # 0️⃣ Persist to SQLite source of truth (best-effort).
        if self.user_store:
            try:
                await self.user_store.upsert_user(data)
            except Exception as ex:
                logger.warning("Failed to persist user_state to sqlite | user=%s err=%s", id_, ex)

        # 1️⃣ Получаем старые данные
        old = await self._get_user_redis_only(id_)

        # 2️⃣ Если был старый thread_id — чистим индекс
        if old and old.message_thread_id and old.message_thread_id != data.message_thread_id:
            await self._delete_index(old.message_thread_id, id_)

        # 3️⃣ Сохраняем новые данные
        json_data = json.dumps(data.to_dict())
        await self._set(self.NAME, id_, json_data)

        # 4️⃣ Создаём новый индекс ТОЛЬКО если thread_id есть
        if data.message_thread_id:
            await self._update_index(data.message_thread_id, id_)

    async def get_all_users_ids(self) -> list[int]:
        """
        Retrieves all user IDs stored in the Redis hash.

        :return: A list of all user IDs.
        """
        ids: set[int] = set()
        with contextlib.suppress(Exception):
            ids.update(await self.get_all_users_ids_redis_only())

        if self.user_store:
            try:
                ids.update(await self.user_store.get_all_user_ids())
            except Exception as ex:
                logger.warning("Failed to list user_state ids from sqlite | err=%s", ex)

        return sorted(ids)

    async def get_all_users_ids_redis_only(self) -> list[int]:
        async with self.redis.client() as client:
            user_ids = await client.hkeys(self.NAME)
            return [int(user_id) for user_id in user_ids]

    async def backfill_user_store_from_redis(self) -> None:
        """
        One-time migration helper: if SQLite user_state is empty, copy all users from Redis into SQLite.
        """
        if not self.user_store:
            return

        try:
            existing = await self.user_store.count_users()
        except Exception as ex:
            logger.warning("Failed to count sqlite user_state rows | err=%s", ex)
            return

        if existing > 0:
            return

        try:
            user_ids = await self.get_all_users_ids_redis_only()
        except Exception as ex:
            logger.warning("Failed to list users from redis for sqlite backfill | err=%s", ex)
            return

        if not user_ids:
            return

        logger.info("Backfilling sqlite user_state from redis | users=%s", len(user_ids))
        for user_id in user_ids:
            user = await self._get_user_redis_only(user_id)
            if not user:
                continue
            try:
                await self.user_store.upsert_user(user)
            except Exception as ex:
                logger.warning("Failed to backfill user_state row | user=%s err=%s", user_id, ex)
        logger.info("Backfill sqlite user_state done | users=%s", len(user_ids))

    async def _delete_index(self, message_thread_id: int, user_id: int) -> None:
        index_key = f"{self.NAME}_index_{message_thread_id}"
        async with self.redis.client() as client:
            await client.hdel(index_key, user_id)
