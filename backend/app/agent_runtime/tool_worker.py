from __future__ import annotations

import asyncio
import json
import signal
import socket
import uuid
from contextlib import suppress
from datetime import datetime

from backend.app.agent_runtime.artifact_service import artifact_service
from backend.app.agent_runtime.redis_broker import RedisBroker, redis_broker
from backend.app.agent_runtime.tool_repository import (
    ToolExecutionRepository,
    tool_execution_repository,
)
from backend.app.core.applog import get_logger, log_event
from backend.app.core.database import SessionLocal
from backend.app.core.models import Heartbeat
from backend.app.core.settings import settings


logger = get_logger("agent.runtime.tool_worker")


class ToolWorker:
    def __init__(
        self,
        repository: ToolExecutionRepository = tool_execution_repository,
        broker: RedisBroker = redis_broker,
        worker_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:10]}"
        self._stopping = asyncio.Event()
        self._running: set[asyncio.Task[None]] = set()
        self._inflight: set[str] = set()

    def _record_heartbeat(self, status: str) -> None:
        now = datetime.utcnow()
        detail = json.dumps(
            {
                "worker_id": self.worker_id,
                "running": len(self._running),
                "concurrency": max(1, settings.tool_worker_concurrency),
            },
            ensure_ascii=False,
        )
        with SessionLocal() as db:
            row = db.get(Heartbeat, "agent-tool-worker")
            if row is None:
                db.add(
                    Heartbeat(
                        component="agent-tool-worker",
                        status=status,
                        last_seen_at=now,
                        detail_json=detail,
                    )
                )
            else:
                row.status = status
                row.last_seen_at = now
                row.detail_json = detail
            db.commit()

    async def dispatch_outbox(self) -> int:
        dispatched = 0
        rows = await asyncio.to_thread(self.repository.pending_outbox, 100)
        for row in rows:
            execution_id = str(row["payload"].get("execution_id") or row["aggregate_id"])
            try:
                stream_id = await self.broker.publish_tool_task(execution_id)
                # The database poller consumes the row even while Redis is down.
                # Marking the outbox delivered prevents an unbounded publish loop.
                await asyncio.to_thread(
                    self.repository.mark_outbox_published,
                    int(row["id"]),
                    stream_id or "mysql-fallback",
                )
                dispatched += 1
            except Exception as exc:  # noqa: BLE001
                await asyncio.to_thread(
                    self.repository.mark_outbox_failed,
                    int(row["id"]),
                    str(exc),
                )
        return dispatched

    async def _heartbeat(self, execution_id: str) -> None:
        interval = max(2, settings.tool_worker_lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            active = await asyncio.to_thread(
                self.repository.heartbeat,
                execution_id,
                self.worker_id,
                settings.tool_worker_lease_seconds,
            )
            if not active:
                return

    async def _execute(self, execution_id: str, message_id: str | None) -> None:
        heartbeat: asyncio.Task[None] | None = None
        try:
            claimed = await asyncio.to_thread(
                self.repository.claim,
                execution_id,
                self.worker_id,
                settings.tool_worker_lease_seconds,
            )
            if claimed is None:
                return
            work = await asyncio.to_thread(self.repository.load_work, execution_id)
            if work is None:
                raise RuntimeError("Tool execution context is missing.")
            heartbeat = asyncio.create_task(
                self._heartbeat(execution_id),
                name=f"tool-heartbeat:{execution_id}",
            )
            from backend.app.agent_runtime.agentscope_engine import _build_definition, _execute_tool

            request_payload = dict(work["request_payload"])
            request_payload["run_id"] = work["run_id"]
            write_names = set(request_payload.get("write_tool_names") or [])
            if not write_names:
                _prompt, _schemas, write_names = _build_definition(request_payload)
            try:
                if work["has_side_effects"]:
                    # Python has its own hard sandbox timeout. Other writes are
                    # intentionally allowed to finish while the lease heartbeat
                    # remains active: cancelling a thread cannot cancel the
                    # underlying side effect and would create a false retry risk.
                    result = await asyncio.to_thread(
                        _execute_tool,
                        str(work["tool_name"]),
                        dict(work["arguments"]),
                        request_payload,
                        write_names,
                    )
                else:
                    async with asyncio.timeout(max(1, int(work["timeout_seconds"]))):
                        result = await asyncio.to_thread(
                            _execute_tool,
                            str(work["tool_name"]),
                            dict(work["arguments"]),
                            request_payload,
                            write_names,
                        )
                if not isinstance(result, dict):
                    result = {"result": result}
                if work["tool_name"] in {"ws_write_file", "ws_edit_file"} and result.get("path"):
                    result = {**result, "artifacts": [{"path": result["path"]}]}
                result = await asyncio.to_thread(
                    artifact_service.register_result,
                    result=result,
                    user_id=int(request_payload["user_id"]),
                    session_id=int(request_payload["session_id"]) if request_payload.get("session_id") else None,
                    run_id=str(work["run_id"]),
                    tool_execution_id=execution_id,
                    created_by=str(request_payload.get("user_label") or "agent"),
                )
                # A returned {error: ...} is a completed business result. The
                # model must see it (for example, to ask for a missing account)
                # and it is not evidence that a side effect has an unknown state.
                completed = await asyncio.to_thread(
                    self.repository.complete,
                    execution_id,
                    self.worker_id,
                    result,
                )
                status = "completed" if completed else None
            except TimeoutError:
                status = await asyncio.to_thread(
                    self.repository.fail,
                    execution_id,
                    self.worker_id,
                    error=f"Tool execution timed out after {work['timeout_seconds']} seconds.",
                    error_code="timeout",
                    retryable=True,
                )
            except Exception as exc:  # noqa: BLE001
                status = await asyncio.to_thread(
                    self.repository.fail,
                    execution_id,
                    self.worker_id,
                    error=str(exc),
                    error_code="worker_exception",
                    retryable=self._is_retryable(str(exc)),
                )
            resolved = await asyncio.to_thread(self.repository.get, execution_id)
            final_status = str(status or (resolved or {}).get("status") or "failed")
            await self.broker.publish_tool_result(execution_id, final_status)
            if final_status in {"dead", "unknown"}:
                await self.broker.dead_letter_tool(
                    execution_id,
                    final_status,
                    (resolved or {}).get("error"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Tool worker task failed: %s", exc)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            if message_id:
                await self.broker.acknowledge_tool_task(message_id)

    @staticmethod
    def _is_retryable(error: str) -> bool:
        lowered = error.lower()
        permanent = (
            "invalid request",
            "permission denied",
            "forbidden",
            "not found",
            "does not exist",
            "unknown tool",
        )
        if any(item in lowered for item in permanent):
            return False
        return any(
            item in lowered
            for item in (
                "timeout",
                "timed out",
                "connection",
                "temporarily unavailable",
                "rate limit",
                "http 429",
                "http 500",
                "http 502",
                "http 503",
                "http 504",
            )
        )

    def _launch(self, execution_id: str, message_id: str | None) -> None:
        if execution_id in self._inflight:
            return
        self._inflight.add(execution_id)
        task = asyncio.create_task(
            self._execute(execution_id, message_id),
            name=f"tool-execution:{execution_id}",
        )
        self._running.add(task)

        def done(completed: asyncio.Task[None]) -> None:
            self._running.discard(completed)
            self._inflight.discard(execution_id)

        task.add_done_callback(done)

    async def run(self) -> None:
        logger.info("Durable tool worker started: %s", self.worker_id)
        last_recovery = 0.0
        last_heartbeat = 0.0
        loop = asyncio.get_running_loop()
        try:
            while not self._stopping.is_set():
                await self.dispatch_outbox()
                now = loop.time()
                if now - last_heartbeat >= 10:
                    last_heartbeat = now
                    await asyncio.to_thread(self._record_heartbeat, "ok")
                if now - last_recovery >= 15:
                    last_recovery = now
                    recovered = await asyncio.to_thread(self.repository.recover_expired)
                    for row in recovered:
                        if row["status"] in {"unknown", "dead"}:
                            record = await asyncio.to_thread(self.repository.get, row["execution_id"])
                            await self.broker.dead_letter_tool(
                                row["execution_id"], row["status"], (record or {}).get("error")
                            )
                concurrency = max(1, settings.tool_worker_concurrency)
                if len(self._running) >= concurrency:
                    await asyncio.wait(self._running, return_when=asyncio.FIRST_COMPLETED)
                    continue
                stream_item = await self.broker.consume_tool_task(
                    self.worker_id,
                    timeout_ms=max(50, int(settings.tool_worker_poll_seconds * 1000)),
                )
                if stream_item:
                    message_id, execution_id = stream_item
                else:
                    message_id = None
                    execution_id = await asyncio.to_thread(self.repository.next_available)
                if execution_id:
                    self._launch(execution_id, message_id)
                    await asyncio.sleep(0)
                else:
                    await asyncio.sleep(max(0.05, settings.tool_worker_poll_seconds))
            if self._running:
                await asyncio.gather(*self._running, return_exceptions=True)
        finally:
            await asyncio.to_thread(self._record_heartbeat, "stopped")
            logger.info("Durable tool worker stopped")

    def stop(self) -> None:
        self._stopping.set()


async def _main() -> None:
    worker = ToolWorker()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, worker.stop)
    try:
        await worker.run()
    finally:
        await redis_broker.close()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        log_event("agent", "INFO", "Tool worker stopped by signal", to_db=False)
