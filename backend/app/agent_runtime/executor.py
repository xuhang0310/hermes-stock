from __future__ import annotations

import asyncio
import random
import socket
import time
import uuid
from contextlib import suppress
from datetime import datetime
from typing import Any

from backend.app.agent_runtime.agentscope_engine import run_agentscope
from backend.app.agent_runtime.contracts import EngineOutcome
from backend.app.agent_runtime.duration import elapsed_wall_clock_ms
from backend.app.agent_runtime.event_sink import DurableEventSink
from backend.app.agent_runtime.legacy_engine import run_legacy
from backend.app.agent_runtime.redis_broker import RedisBroker, redis_broker
from backend.app.agent_runtime.repository import AgentRunRepository, agent_run_repository
from backend.app.core.applog import log_event
from backend.app.core.database import SessionLocal
from backend.app.core.models import AiChatMessage, AiChatSession
from backend.app.core.settings import settings


class AgentRunExecutor:
    def __init__(
        self,
        repository: AgentRunRepository = agent_run_repository,
        broker: RedisBroker = redis_broker,
        worker_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:10]}"
        from backend.app.agent_runtime.team_service import AgentTeamService

        self.team_service = AgentTeamService(repository.session_factory, repository)

    async def execute(self, run_id: str) -> bool:
        run_lease = await self.broker.acquire_lease(f"run:{run_id}", settings.agent_lock_ttl_seconds)
        if run_lease is None:
            return False
        session_lease: str | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        request_payload: dict[str, Any] | None = None
        try:
            request_payload = await asyncio.to_thread(self.repository.load_request, run_id)
            if request_payload is None:
                return False
            session_id = int(request_payload["session_id"])
            session_lease = await self.broker.acquire_lease(
                f"session:{session_id}", settings.agent_lock_ttl_seconds
            )
            if session_lease is None:
                return False
            run = await asyncio.to_thread(self.repository.claim_run, run_id, self.worker_id)
            if run is None:
                return False

            sink = DurableEventSink(run_id, self.repository, self.broker)
            heartbeat_task = asyncio.create_task(
                self._heartbeat(run_id, session_id, run_lease, session_lease),
                name=f"agent-heartbeat:{run_id}",
            )
            started = time.monotonic()

            async def save_checkpoint(state_json: str, waiting: list[dict[str, Any]] | None) -> None:
                await asyncio.to_thread(
                    self.repository.save_checkpoint,
                    run_id,
                    engine=run["engine"],
                    state_json=state_json,
                    waiting=waiting,
                )

            async def is_cancelled() -> bool:
                return await asyncio.to_thread(self.repository.is_cancel_requested, run_id)

            checkpoint = await asyncio.to_thread(self.repository.load_checkpoint, run_id)
            try:
                async with asyncio.timeout(max(30, settings.agent_run_timeout_seconds)):
                    if run["engine"] == "agentscope":
                        outcome = await run_agentscope(
                            run_id=run_id,
                            request_payload=request_payload,
                            checkpoint_json=checkpoint["state_json"] if checkpoint else None,
                            emit=sink.emit,
                            save_checkpoint=save_checkpoint,
                            is_cancelled=is_cancelled,
                        )
                    else:
                        outcome = await run_legacy(
                            request_payload=request_payload,
                            emit=sink.emit,
                            is_cancelled=is_cancelled,
                        )
            except TimeoutError:
                outcome = EngineOutcome(status="failed", error="Agent 执行超时，任务已停止。")
            await sink.flush()

            if await is_cancelled() or outcome.status == "cancelled":
                duration_ms = self._total_duration_ms(run, started)
                await sink.emit({"type": "done", "status": "cancelled", "duration_ms": duration_ms})
                await sink.flush()
                await asyncio.to_thread(self.repository.mark_terminal, run_id, "cancelled")
                await self._notify_team_parent(run_id)
                return True
            if outcome.status == "waiting_approval":
                approval_already_resolved = await asyncio.to_thread(
                    self.repository.mark_waiting,
                    run_id,
                )
                if approval_already_resolved:
                    await self.broker.enqueue(run_id)
                return True
            if outcome.status == "waiting_children":
                team_already_resolved = await asyncio.to_thread(
                    self.repository.mark_waiting_children,
                    run_id,
                )
                if team_already_resolved:
                    await self.broker.enqueue(run_id)
                return True
            if outcome.status == "failed":
                await self._handle_failure(run, sink, outcome.error or "Agent execution failed", started)
                await self._notify_team_parent(run_id)
                return True

            duration_ms = self._total_duration_ms(run, started)
            message_id = await asyncio.to_thread(
                self._store_assistant_message,
                request_payload,
                outcome.final_answer,
                None,
                duration_ms,
            )
            await sink.emit(
                {
                    "type": "done",
                    "status": "completed",
                    "duration_ms": duration_ms,
                    "completion_state": outcome.completion_state,
                    "completion": outcome.completion,
                }
            )
            await sink.flush()
            await asyncio.to_thread(
                self.repository.mark_terminal,
                run_id,
                "completed",
                result_message_id=message_id,
                completion_state=outcome.completion_state,
                completion=outcome.completion,
            )
            await self._notify_team_parent(run_id)
            self._schedule_post_run(request_payload)
            log_event(
                "agent", "INFO", "Durable Agent run completed", to_db=False,
                run_id=run_id, engine=run["engine"], ms=duration_ms,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            await self._recover_or_fail(run_id, str(exc))
            return False
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            if session_lease is not None and request_payload is not None:
                await self.broker.release_lease(f"session:{request_payload['session_id']}", session_lease)
            await self.broker.release_lease(f"run:{run_id}", run_lease)

    async def _heartbeat(
        self,
        run_id: str,
        session_id: int,
        run_token: str,
        session_token: str | None,
    ) -> None:
        interval = max(5, settings.agent_lock_ttl_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(self.repository.touch, run_id, self.worker_id)
            await self.broker.renew_lease(f"run:{run_id}", run_token, settings.agent_lock_ttl_seconds)
            if session_token is not None:
                await self.broker.renew_lease(
                    f"session:{session_id}", session_token, settings.agent_lock_ttl_seconds
                )

    async def _handle_failure(
        self,
        run: dict[str, Any],
        sink: DurableEventSink,
        error: str,
        started: float,
    ) -> None:
        run_id = run["run_id"]
        has_side_effects = await asyncio.to_thread(self.repository.has_side_effects, run_id)
        if (
            not has_side_effects
            and self._is_retryable(error)
            and int(run["attempts"]) < int(run["max_attempts"])
        ):
            delay_seconds = min(10.0, 2 ** max(0, int(run["attempts"]) - 1)) + random.uniform(0, 0.5)
            await sink.emit(
                {
                    "type": "retrying",
                    "attempt": int(run["attempts"]) + 1,
                    "max_attempts": int(run["max_attempts"]),
                    "retry_after_ms": int(delay_seconds * 1000),
                    "reset_stream": True,
                    "message": "本次执行失败，系统正在自动恢复。",
                }
            )
            await sink.flush()
            if await asyncio.to_thread(self.repository.requeue, run_id, error=error):
                await asyncio.sleep(delay_seconds)
                await self.broker.enqueue(run_id)
                return
        duration_ms = self._total_duration_ms(run, started)
        await sink.emit({"type": "error", "content": error})
        await sink.emit({"type": "done", "status": "failed", "duration_ms": duration_ms})
        await sink.flush()
        request_payload = await asyncio.to_thread(self.repository.load_request, run_id)
        message_id = None
        if request_payload:
            message_id = await asyncio.to_thread(
                self._store_assistant_message, request_payload, "", error, duration_ms
            )
        await asyncio.to_thread(
            self.repository.mark_terminal,
            run_id,
            "failed",
            error=error,
            result_message_id=message_id,
        )

    async def _recover_or_fail(self, run_id: str, error: str) -> None:
        run = await asyncio.to_thread(self.repository.get_run, run_id)
        if run and run["status"] not in {"completed", "failed", "cancelled"}:
            if run.get("cancel_requested"):
                await asyncio.to_thread(self.repository.mark_terminal, run_id, "cancelled")
                sink = DurableEventSink(run_id, self.repository, self.broker)
                await sink.emit({"type": "done", "status": "cancelled"})
                await sink.flush()
                await self._notify_team_parent(run_id)
                return
            has_side_effects = await asyncio.to_thread(self.repository.has_side_effects, run_id)
            if not has_side_effects and self._is_retryable(error) and await asyncio.to_thread(
                self.repository.requeue, run_id, error=error
            ):
                await self.broker.enqueue(run_id)
                return
            await asyncio.to_thread(self.repository.mark_terminal, run_id, "failed", error=error)
            sink = DurableEventSink(run_id, self.repository, self.broker)
            await sink.emit({"type": "error", "content": error})
            await sink.emit({"type": "done", "status": "failed"})
            await sink.flush()
            await self._notify_team_parent(run_id)
        log_event("agent", "ERROR", "Durable Agent worker failed", run_id=run_id, error=error[:500])

    async def _notify_team_parent(self, run_id: str) -> None:
        result = await asyncio.to_thread(self.team_service.on_child_terminal, run_id)
        if not result or not result.get("parent_run_id"):
            return
        parent_run_id = str(result["parent_run_id"])
        for seq in result.get("event_seqs") or []:
            await self.broker.publish_event(parent_run_id, int(seq))
        if result.get("parent_requeued"):
            await self.broker.enqueue(parent_run_id)

    @staticmethod
    def _total_duration_ms(run: dict[str, Any], attempt_started: float) -> int:
        attempt_ms = max(0, int((time.monotonic() - attempt_started) * 1000))
        total_ms = elapsed_wall_clock_ms(run.get("created_at"), datetime.utcnow())
        return max(attempt_ms, total_ms or 0)

    @staticmethod
    def _is_retryable(error: str) -> bool:
        value = error.lower()
        permanent_markers = (
            "invalid request",
            "invalid_request",
            "authentication",
            "unauthorized",
            "permission denied",
            "forbidden",
            "api key",
            "http 400",
            "http 401",
            "http 403",
            "status code: 400",
            "status code: 401",
            "status code: 403",
        )
        if any(marker in value for marker in permanent_markers):
            return False
        transient_markers = (
            "timeout",
            "timed out",
            "执行超时",
            "connection",
            "连接失败",
            "temporarily unavailable",
            "rate limit",
            "too many requests",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "status code: 429",
            "status code: 500",
            "status code: 502",
            "status code: 503",
            "status code: 504",
        )
        return any(marker in value for marker in transient_markers)

    def _store_assistant_message(
        self,
        request_payload: dict[str, Any],
        content: str,
        error: str | None,
        duration_ms: int,
    ) -> int | None:
        if not content and not error:
            return None
        with SessionLocal() as db:
            chat_session = db.get(AiChatSession, int(request_payload["session_id"]))
            if chat_session is None:
                return None
            row = AiChatMessage(
                session_id=chat_session.id,
                role="assistant",
                content=content,
                error=error,
                event_count=0,
                duration_ms=duration_ms,
            )
            db.add(row)
            chat_session.status = "error" if error else "active"
            db.commit()
            db.refresh(row)
            return row.id

    @staticmethod
    def _schedule_post_run(request_payload: dict[str, Any]) -> None:
        from backend.app.ai.memory import schedule_memory_digestion
        from backend.app.ai.prompt_recommendations import schedule_recommendation_refresh

        profile_id = request_payload.get("digital_human_id")
        if profile_id and request_payload.get("chat_mode") == "research":
            schedule_recommendation_refresh(
                user_id=int(request_payload["user_id"]),
                profile_id=int(profile_id),
                chat_mode="research",
            )
        asyncio.create_task(
            schedule_memory_digestion(
                int(request_payload["session_id"]),
                int(request_payload["user_id"]),
                int(profile_id) if profile_id else None,
            )
        )
