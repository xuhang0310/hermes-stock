from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from backend.app.agent_runtime.redis_broker import RedisBroker
from backend.app.agent_runtime.repository import AgentRunRepository
from backend.app.core.settings import settings


class DurableEventSink:
    """Persist events before notifying SSE subscribers.

    Tiny token deltas are coalesced to avoid turning one model response into
    thousands of MySQL transactions while preserving replay semantics.
    """

    DELTA_TYPES = frozenset({"content_delta", "reasoning_delta"})

    def __init__(self, run_id: str, repository: AgentRunRepository, broker: RedisBroker) -> None:
        self.run_id = run_id
        self.repository = repository
        self.broker = broker
        self._pending: dict[str, Any] | None = None
        self._progress_flushed_chars: dict[str, int] = {}

    async def emit(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "message")
        if event_type in self.DELTA_TYPES:
            content = str(event.get("content") or "")
            if self._pending and self._pending.get("type") == event_type:
                self._pending["content"] = str(self._pending.get("content") or "") + content
            else:
                await self.flush()
                self._pending = dict(event)
            if len(str(self._pending.get("content") or "")) >= max(32, settings.agent_event_flush_chars):
                await self.flush()
            return
        if event_type == "tool_progress":
            progress_key = self._tool_progress_key(event)
            if self._pending and (
                self._pending.get("type") != "tool_progress"
                or self._tool_progress_key(self._pending) != progress_key
            ):
                await self.flush()
            if self._pending is None:
                self._pending = dict(event)
            else:
                self._pending.update(event)
            current_chars = int(event.get("chars") or 0)
            last_chars = self._progress_flushed_chars.get(progress_key, 0)
            if current_chars - last_chars >= max(32, settings.agent_event_flush_chars):
                await self.flush()
            return
        await self.flush()
        await self._persist(event)

    async def flush(self) -> None:
        if self._pending is None:
            return
        event = self._pending
        self._pending = None
        if event.get("type") == "tool_progress":
            self._progress_flushed_chars[self._tool_progress_key(event)] = int(event.get("chars") or 0)
        await self._persist(event)

    @staticmethod
    def _tool_progress_key(event: dict[str, Any]) -> str:
        return str(event.get("tool_call_id") or event.get("index") or event.get("tool") or "tool")

    async def _persist(self, event: dict[str, Any]) -> None:
        stored = await asyncio.to_thread(
            self.repository.append_event,
            self.run_id,
            self._sanitize_event(event),
        )
        await self.broker.publish_event(self.run_id, stored.seq)

    @staticmethod
    def _sanitize_event(event: dict[str, Any]) -> dict[str, Any]:
        """Keep replay useful without persisting secrets or full file bodies."""

        safe = dict(event)
        event_type = str(safe.get("type") or "")
        if event_type == "tool_progress":
            safe["delta"] = ""
            return safe
        if event_type != "action":
            return safe

        raw = str(safe.get("content") or "")
        safe["arguments_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        try:
            arguments = json.loads(raw or "{}")
        except json.JSONDecodeError:
            safe["content"] = json.dumps(
                {"characters": len(raw), "unparseable": True},
                ensure_ascii=False,
            )
            return safe
        if not isinstance(arguments, dict):
            safe["content"] = json.dumps(
                {"type": type(arguments).__name__, "characters": len(raw)},
                ensure_ascii=False,
            )
            return safe

        def sanitize_value(key: str, value: Any) -> Any:
            lowered = key.lower()
            if any(token in lowered for token in ("base64", "secret", "token", "password", "api_key")):
                return "[redacted]"
            if key in {"content", "patch"} and isinstance(value, str):
                return {
                    "characters": len(value),
                    "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                }
            if isinstance(value, dict):
                return {
                    str(child_key): sanitize_value(str(child_key), child)
                    for child_key, child in value.items()
                }
            if isinstance(value, list):
                return [sanitize_value(key, child) for child in value]
            if isinstance(value, str) and len(value) > 4000:
                return {
                    "characters": len(value),
                    "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                }
            return value

        sanitized = {str(key): sanitize_value(str(key), value) for key, value in arguments.items()}
        safe["content"] = json.dumps(sanitized, ensure_ascii=False, default=str)
        return safe
