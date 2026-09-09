from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from backend.app.agent_runtime.executor import AgentRunExecutor
from backend.app.agent_runtime.redis_broker import RedisBroker, redis_broker
from backend.app.agent_runtime.repository import AgentRunRepository, agent_run_repository
from backend.app.core.applog import get_logger
from backend.app.core.models import Heartbeat
from backend.app.core.settings import settings
from backend.app.agents.tool_approval import expire_pending_run_approvals


logger = get_logger("agent.runtime.worker")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _heartbeat_now() -> datetime:
    return datetime.now(_SHANGHAI).replace(tzinfo=None)


class AgentRuntimeWorker:
    def __init__(
        self,
        repository: AgentRunRepository = agent_run_repository,
        broker: RedisBroker = redis_broker,
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.executor = AgentRunExecutor(repository, broker)
        from backend.app.agent_runtime.team_service import AgentTeamService
        from backend.app.agent_runtime.workflow_service import AgentWorkflowService

        self.team_service = AgentTeamService(repository.session_factory, repository)
        self.workflow_service = AgentWorkflowService(repository.session_factory, repository)
        self.worker_id = self.executor.worker_id
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._running: set[asyncio.Task[bool]] = set()
        self._inflight_ids: set[str] = set()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="agent-runtime-worker")
        self._task.add_done_callback(self._log_unexpected_exit)

    def _log_unexpected_exit(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or self._stopping.is_set():
            return
        exc = task.exception()
        if exc is not None:
            logger.critical(
                "Durable Agent worker loop exited unexpectedly: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            logger.critical("Durable Agent worker loop exited unexpectedly without an error")

    def _record_heartbeat(self, status: str) -> None:
        detail = json.dumps(
            {
                "worker_id": self.worker_id,
                "running": len(self._running),
                "concurrency": max(1, settings.agent_worker_concurrency),
            },
            ensure_ascii=False,
        )
        with self.repository.session_factory() as db:
            row = db.get(Heartbeat, "agent-runtime-worker")
            if row is None:
                db.add(
                    Heartbeat(
                        component="agent-runtime-worker",
                        status=status,
                        last_seen_at=_heartbeat_now(),
                        detail_json=detail,
                    )
                )
            else:
                row.status = status
                row.last_seen_at = _heartbeat_now()
                row.detail_json = detail
            db.commit()

    async def _safe_background_call(
        self,
        label: str,
        callback: Callable[..., Any],
        *args: Any,
        default: Any,
    ) -> Any:
        """Keep one recovery/monitoring failure from stopping queue consumption."""

        try:
            return await asyncio.to_thread(callback, *args)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent worker %s failed; queue consumption will continue: %s", label, exc)
            return default

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._running:
            _done, pending = await asyncio.wait(
                self._running,
                timeout=max(1, settings.agent_shutdown_grace_seconds),
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._running.clear()
        self._inflight_ids.clear()

    def _recover_orphan_board_tasks(self) -> list[str]:
        """恢复孤儿看板任务：board_visible=True 且 queued 超过 5 分钟的任务。

        这些任务可能因为 Redis 唤醒失败而未被 Worker 领取。
        重新 enqueue 是幂等的（已在 Redis 中的不会重复执行）。
        """
        from datetime import timedelta

        from sqlalchemy import select

        from backend.app.agent_runtime.repository import _utcnow
        from backend.app.core.database import SessionLocal
        from backend.app.core.models import AgentRun

        orphan_run_ids: list[str] = []
        cutoff = _utcnow() - timedelta(minutes=5)
        try:
            with SessionLocal() as db:
                orphans = db.scalars(
                    select(AgentRun.run_id).where(
                        AgentRun.board_visible.is_(True),
                        AgentRun.status == "queued",
                        AgentRun.queued_at.isnot(None),
                        AgentRun.queued_at < cutoff,
                        AgentRun.cancel_requested.is_(False),
                    )
                ).all()
                orphan_run_ids = list(orphans)
        except Exception as exc:
            logger.warning("孤儿看板任务扫描失败: %s", exc)
            return []

        if orphan_run_ids:
            logger.info("发现 %d 个孤儿看板任务，重新入队", len(orphan_run_ids))
        return orphan_run_ids

    def _launch(self, run_id: str) -> None:
        if run_id in self._inflight_ids:
            return
        self._inflight_ids.add(run_id)
        task = asyncio.create_task(self.executor.execute(run_id), name=f"agent-run:{run_id}")
        self._running.add(task)

        def done(completed: asyncio.Task[bool]) -> None:
            self._running.discard(completed)
            self._inflight_ids.discard(run_id)
            if not completed.cancelled() and completed.exception() is not None:
                exc = completed.exception()
                logger.error(
                    "Agent run task crashed: %s",
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__) if exc else None,
                )

        task.add_done_callback(done)

    async def _loop(self) -> None:
        logger.info("Durable Agent worker started")
        last_recovery = 0.0
        last_cleanup = 0.0
        last_heartbeat = 0.0
        try:
            while not self._stopping.is_set():
                try:
                    concurrency = max(1, settings.agent_worker_concurrency)
                    if len(self._running) >= concurrency:
                        await asyncio.wait(self._running, return_when=asyncio.FIRST_COMPLETED)
                        continue
                    now = time.monotonic()
                    if now - last_heartbeat >= 10:
                        last_heartbeat = now
                        await self._safe_background_call(
                            "heartbeat",
                            self._record_heartbeat,
                            "ok",
                            default=None,
                        )
                    if now - last_recovery >= 30:
                        last_recovery = now
                        recovered = await self._safe_background_call(
                            "stale-run recovery",
                            self.repository.recover_stale_runs,
                            max(settings.agent_lock_ttl_seconds * 2, 120),
                            default=[],
                        )
                        for run_id in recovered:
                            await self.broker.enqueue(run_id)
                        expired_runs = await self._safe_background_call(
                            "approval expiry",
                            expire_pending_run_approvals,
                            default=[],
                        )
                        for run_id in expired_runs:
                            if await asyncio.to_thread(self.repository.requeue, run_id):
                                await self.broker.enqueue(run_id)
                        resumed_team_runs = await self._safe_background_call(
                            "team reconciliation",
                            self.team_service.reconcile,
                            default=[],
                        )
                        for run_id in resumed_team_runs:
                            await self.broker.enqueue(run_id)
                        resumed_workflow_runs = await self._safe_background_call(
                            "workflow reconciliation",
                            self.workflow_service.reconcile,
                            default=[],
                        )
                        for run_id in resumed_workflow_runs:
                            await self.broker.enqueue(run_id)
                        # 孤儿看板任务恢复：board_visible=True 且 queued 超过 5 分钟的任务
                        orphan_board_runs = await self._safe_background_call(
                            "orphan board recovery",
                            self._recover_orphan_board_tasks,
                            default=[],
                        )
                        for run_id in orphan_board_runs:
                            await self.broker.enqueue(run_id)
                    if now - last_cleanup >= 3600:
                        last_cleanup = now
                        pruned = await self._safe_background_call(
                            "event pruning",
                            self.repository.prune_terminal_events,
                            settings.agent_event_retention_days,
                            default=0,
                        )
                        if pruned:
                            logger.info("Pruned %d expired Agent run events", pruned)
                    # Redis is a wake-up bus; MySQL is the ordered source of truth.
                    # Always select the next run from MySQL so a queued web request can
                    # overtake lower-priority scheduled work already present in Redis.
                    await self.broker.dequeue(timeout_seconds=1)
                    run_id = await asyncio.to_thread(self.repository.next_queued_run)
                    if not run_id:
                        await asyncio.sleep(0.4)
                        continue
                    self._launch(run_id)
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Durable Agent worker loop iteration failed; retrying: %s",
                        exc,
                    )
                    await asyncio.sleep(1)
        finally:
            await self._safe_background_call(
                "stopped heartbeat",
                self._record_heartbeat,
                "stopped",
                default=None,
            )
            logger.info("Durable Agent worker stopped")


agent_runtime_worker = AgentRuntimeWorker()
