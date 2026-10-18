from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request

from backend.app.agent_runtime.agentscope_engine import agentscope_available, select_engine
from backend.app.agent_runtime.contracts import TERMINAL_RUN_STATUSES
from backend.app.agent_runtime.redis_broker import RedisBroker, redis_broker
from backend.app.agent_runtime.repository import AgentRunRepository, agent_run_repository
from backend.app.core.settings import settings


def format_sse(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "message")
    event_id = event.get("seq")
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


class AgentRuntimeService:
    def __init__(
        self,
        repository: AgentRunRepository = agent_run_repository,
        broker: RedisBroker = redis_broker,
    ) -> None:
        self.repository = repository
        self.broker = broker

    async def create_run(
        self,
        request_payload: dict[str, Any],
        *,
        session_event: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        chat_mode = str(request_payload.get("chat_mode") or "research")
        request_payload = dict(request_payload)
        engine = select_engine(chat_mode, settings.agent_runtime_mode)
        source_type = str(request_payload.get("source_type") or "web")
        source_ref = str(request_payload.get("source_ref") or "") or None
        priority = {"web": 300, "feishu": 200, "scheduler": 100}.get(source_type, 150)
        run, created = await asyncio.to_thread(
            self.repository.create_run,
            session_id=int(request_payload["session_id"]),
            user_id=int(request_payload["user_id"]),
            engine=engine,
            chat_mode=chat_mode,
            request_payload=request_payload,
            idempotency_key=idempotency_key,
            priority=priority,
            source_type=source_type,
            source_ref=source_ref,
        )
        if created:
            await self.append_event(run["run_id"], {**session_event, "run_id": run["run_id"]})
            await self.append_event(
                run["run_id"],
                {"type": "queued", "engine": engine, "status": "queued"},
            )
        if run["status"] == "queued":
            await self.broker.enqueue(run["run_id"])
        return {**run, "created": created}

    async def append_event(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        stored = await asyncio.to_thread(self.repository.append_event, run_id, event)
        await self.broker.publish_event(run_id, stored.seq)
        return stored.payload

    async def stream_events(
        self,
        run_id: str,
        *,
        user_id: int,
        after_seq: int,
        request: Request,
    ) -> AsyncIterator[str]:
        run = await asyncio.to_thread(self.repository.get_run, run_id, user_id=user_id)
        if run is None:
            raise KeyError(run_id)
        cursor = max(0, after_seq)
        pubsub = await self.broker.subscribe(run_id)
        try:
            yield ": connected\n\n"
            while True:
                events = await asyncio.to_thread(self.repository.events_after, run_id, cursor)
                for event in events:
                    cursor = event.seq
                    yield format_sse(event.payload)
                    if event.event_type == "done":
                        return
                if await request.is_disconnected():
                    return
                run = await asyncio.to_thread(self.repository.get_run, run_id, user_id=user_id)
                if run is None:
                    return
                if run["status"] in TERMINAL_RUN_STATUSES and cursor >= int(run["last_event_seq"]):
                    return
                if pubsub is not None:
                    await self.broker.wait_for_event(pubsub, timeout_seconds=8)
                    yield ": ping\n\n"
                else:
                    await asyncio.sleep(0.35)
        finally:
            await self.broker.close_subscription(pubsub)

    async def resume_after_approval(self, run_id: str) -> bool:
        run = await asyncio.to_thread(self.repository.get_run, run_id)
        if run is None or run["status"] != "waiting_approval":
            return False
        if not await asyncio.to_thread(self.repository.requeue, run_id):
            return False
        await self.broker.enqueue(run_id)
        return True

    async def status(self) -> dict[str, Any]:
        available, version = agentscope_available()
        return {
            "mode": settings.agent_runtime_mode,
            "llm_wire_api": settings.agent_llm_wire_api,
            "worker_enabled": settings.agent_runtime_worker_enabled,
            "agentscope": {"available": available, "version": version},
            "redis": await self.broker.health(),
        }


agent_runtime_service = AgentRuntimeService()
