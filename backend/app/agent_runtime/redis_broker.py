from __future__ import annotations

import asyncio
import secrets
from typing import Any

from backend.app.core.applog import get_logger
from backend.app.core.settings import settings


logger = get_logger("agent.runtime.redis")


class RedisBroker:
    """Redis queue, wake-up bus, and lease provider.

    MySQL remains the source of truth. Every method degrades cleanly when
    Redis is unavailable so queued runs can still be recovered by DB polling.
    """

    QUEUE_KEY = "hermes:agent:runs:queue"

    def __init__(self, url: str | None = None) -> None:
        self.url = (url if url is not None else settings.agent_redis_url).strip()
        self._client: Any | None = None
        self._warned = False

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def _get_client(self):
        if not self.configured:
            return None
        if self._client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(
                self.url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=5,
                health_check_interval=30,
            )
        return self._client

    def _log_failure_once(self, operation: str, exc: Exception) -> None:
        if self._warned:
            return
        self._warned = True
        logger.warning("Redis %s failed; falling back to MySQL polling: %s", operation, exc)

    async def health(self) -> dict[str, Any]:
        client = self._get_client()
        if client is None:
            return {"configured": False, "ok": False, "version": None}
        try:
            info = await client.info(section="server")
            self._warned = False
            return {"configured": True, "ok": True, "version": info.get("redis_version")}
        except Exception as exc:  # noqa: BLE001
            self._log_failure_once("health check", exc)
            return {"configured": True, "ok": False, "version": None}

    async def enqueue(self, run_id: str) -> bool:
        client = self._get_client()
        if client is None:
            return False
        try:
            await client.lpush(self.QUEUE_KEY, run_id)
            self._warned = False
            return True
        except Exception as exc:  # noqa: BLE001
            self._log_failure_once("enqueue", exc)
            return False

    async def dequeue(self, timeout_seconds: int = 1) -> str | None:
        client = self._get_client()
        if client is None:
            return None
        try:
            item = await client.brpop(self.QUEUE_KEY, timeout=max(1, timeout_seconds))
            self._warned = False
            return str(item[1]) if item else None
        except Exception as exc:  # noqa: BLE001
            self._log_failure_once("dequeue", exc)
            return None

    @staticmethod
    def run_channel(run_id: str) -> str:
        return f"hermes:agent:run:{run_id}:events"

    async def publish_event(self, run_id: str, seq: int) -> None:
        client = self._get_client()
        if client is None:
            return
        try:
            await client.publish(self.run_channel(run_id), str(seq))
            self._warned = False
        except Exception as exc:  # noqa: BLE001
            self._log_failure_once("publish", exc)

    async def subscribe(self, run_id: str):
        client = self._get_client()
        if client is None:
            return None
        try:
            pubsub = client.pubsub()
            await pubsub.subscribe(self.run_channel(run_id))
            self._warned = False
            return pubsub
        except Exception as exc:  # noqa: BLE001
            self._log_failure_once("subscribe", exc)
            return None

    async def wait_for_event(self, pubsub, timeout_seconds: float) -> None:
        if pubsub is None:
            return
        try:
            await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            self._log_failure_once("event wait", exc)
            await asyncio.sleep(min(1.0, max(0.1, timeout_seconds)))

    async def close_subscription(self, pubsub) -> None:
        if pubsub is None:
            return
        try:
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass

    async def acquire_lease(self, resource: str, ttl_seconds: int) -> str | None:
        client = self._get_client()
        if client is None:
            return "mysql-fallback"
        token = secrets.token_hex(16)
        key = f"hermes:agent:lease:{resource}"
        try:
            acquired = await client.set(key, token, nx=True, ex=max(5, ttl_seconds))
            self._warned = False
            return token if acquired else None
        except Exception as exc:  # noqa: BLE001
            self._log_failure_once("lease acquire", exc)
            # Database row locking still serializes runs when Redis is down.
            # Keep accepted work moving instead of stranding it in the queue.
            return "mysql-fallback"

    async def renew_lease(self, resource: str, token: str, ttl_seconds: int) -> bool:
        if token == "mysql-fallback":
            return True
        client = self._get_client()
        if client is None:
            return False
        key = f"hermes:agent:lease:{resource}"
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('expire', KEYS[1], ARGV[2])
        end
        return 0
        """
        try:
            return bool(await client.eval(script, 1, key, token, max(5, ttl_seconds)))
        except Exception as exc:  # noqa: BLE001
            self._log_failure_once("lease renew", exc)
            return False

    async def release_lease(self, resource: str, token: str) -> None:
        if token == "mysql-fallback":
            return
        client = self._get_client()
        if client is None:
            return
        key = f"hermes:agent:lease:{resource}"
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('del', KEYS[1])
        end
        return 0
        """
        try:
            await client.eval(script, 1, key, token)
        except Exception as exc:  # noqa: BLE001
            self._log_failure_once("lease release", exc)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


redis_broker = RedisBroker()
