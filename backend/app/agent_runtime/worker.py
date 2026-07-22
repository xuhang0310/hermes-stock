from __future__ import annotations

import asyncio
import time
from contextlib import suppress

from backend.app.agent_runtime.executor import AgentRunExecutor
from backend.app.agent_runtime.redis_broker import RedisBroker, redis_broker
from backend.app.agent_runtime.repository import AgentRunRepository, agent_run_repository
from backend.app.core.applog import get_logger
from backend.app.core.settings import settings
from backend.app.agents.tool_approval import expire_pending_run_approvals


logger = get_logger("agent.runtime.worker")


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

        self.team_service = AgentTeamService(repository.session_factory, repository)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._running: set[asyncio.Task[bool]] = set()
        self._inflight_ids: set[str] = set()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="agent-runtime-worker")

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
        while not self._stopping.is_set():
            concurrency = max(1, settings.agent_worker_concurrency)
            if len(self._running) >= concurrency:
                await asyncio.wait(self._running, return_when=asyncio.FIRST_COMPLETED)
                continue
            now = time.monotonic()
            if now - last_recovery >= 30:
                last_recovery = now
                recovered = await asyncio.to_thread(
                    self.repository.recover_stale_runs,
                    max(settings.agent_lock_ttl_seconds * 2, 120),
                )
                for run_id in recovered:
                    await self.broker.enqueue(run_id)
                expired_runs = await asyncio.to_thread(expire_pending_run_approvals)
                for run_id in expired_runs:
                    if await asyncio.to_thread(self.repository.requeue, run_id):
                        await self.broker.enqueue(run_id)
                resumed_team_runs = await asyncio.to_thread(self.team_service.reconcile)
                for run_id in resumed_team_runs:
                    await self.broker.enqueue(run_id)
            if now - last_cleanup >= 3600:
                last_cleanup = now
                pruned = await asyncio.to_thread(
                    self.repository.prune_terminal_events,
                    settings.agent_event_retention_days,
                )
                if pruned:
                    logger.info("Pruned %d expired Agent run events", pruned)
            run_id = await self.broker.dequeue(timeout_seconds=1)
            if not run_id:
                run_id = await asyncio.to_thread(self.repository.next_queued_run)
            if not run_id:
                await asyncio.sleep(0.4)
                continue
            self._launch(run_id)
            await asyncio.sleep(0)
        logger.info("Durable Agent worker stopped")


agent_runtime_worker = AgentRuntimeWorker()
