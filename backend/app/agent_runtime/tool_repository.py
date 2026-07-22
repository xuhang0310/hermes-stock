from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.agent_runtime.tool_specs import ToolSpec
from backend.app.core.database import SessionLocal
from backend.app.core.models import AgentOutbox, AgentRun, AgentToolExecution


TERMINAL_TOOL_STATUSES = {"completed", "failed", "dead", "unknown", "cancelled"}


def _utcnow() -> datetime:
    return datetime.utcnow()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _execution_dict(row: AgentToolExecution) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    if row.result_json:
        try:
            parsed = json.loads(row.result_json)
            result = parsed if isinstance(parsed, dict) else {"result": parsed}
        except json.JSONDecodeError:
            result = {"result": row.result_json}
    return {
        "execution_id": row.execution_id,
        "run_id": row.run_id,
        "tool_call_id": row.tool_call_id,
        "tool_name": row.tool_name,
        "tool_version": row.tool_version,
        "execution_mode": row.execution_mode,
        "status": row.status,
        "attempts": row.attempts,
        "max_attempts": row.max_attempts,
        "timeout_seconds": row.timeout_seconds,
        "has_side_effects": bool(row.has_side_effects),
        "result": result,
        "error": row.error,
        "error_code": row.error_code,
        "lease_owner": row.lease_owner,
        "lease_expires_at": row.lease_expires_at.isoformat() if row.lease_expires_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


class ToolExecutionRepository:
    """MySQL source of truth for external tool execution and delivery.

    Redis carries wake-ups only. A duplicated stream message cannot duplicate a
    side effect because the execution row is claimed under a database lock.
    """

    def __init__(self, session_factory: Callable[[], Session] | sessionmaker[Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    def submit(
        self,
        *,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        spec: ToolSpec,
        tool_call_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        canonical = _json(arguments)
        arguments_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        idempotency_key = hashlib.sha256(
            f"{run_id}:{tool_name}:{spec.version}:{arguments_sha}".encode("utf-8")
        ).hexdigest()
        normalized_call_id = (tool_call_id or f"external:{arguments_sha[:32]}")[:160]
        with self.session_factory() as db:
            existing = db.scalar(
                select(AgentToolExecution)
                .where(AgentToolExecution.idempotency_key == idempotency_key)
                .limit(1)
            )
            if existing is not None:
                return _execution_dict(existing), False

            row = AgentToolExecution(
                execution_id=uuid.uuid4().hex,
                run_id=run_id,
                tool_call_id=normalized_call_id,
                tool_name=tool_name[:120],
                tool_version=spec.version[:40],
                execution_mode=spec.execution_mode,
                status="queued",
                idempotency_key=idempotency_key,
                arguments_sha256=arguments_sha,
                arguments_json=canonical,
                has_side_effects=spec.side_effect != "none",
                attempts=0,
                max_attempts=max(1, spec.max_attempts),
                timeout_seconds=max(1, spec.timeout_seconds),
                available_at=_utcnow(),
            )
            db.add(row)
            db.add(
                AgentOutbox(
                    run_id=run_id,
                    aggregate_type="tool_execution",
                    aggregate_id=row.execution_id,
                    topic="agent.tool.execute",
                    payload_json=_json({"execution_id": row.execution_id}),
                    status="pending",
                    available_at=_utcnow(),
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.scalar(
                    select(AgentToolExecution)
                    .where(AgentToolExecution.idempotency_key == idempotency_key)
                    .limit(1)
                )
                if existing is not None:
                    return _execution_dict(existing), False
                raise
            db.refresh(row)
            return _execution_dict(row), True

    def get(self, execution_id: str) -> dict[str, Any] | None:
        with self.session_factory() as db:
            row = db.get(AgentToolExecution, execution_id)
            return _execution_dict(row) if row else None

    def load_work(self, execution_id: str) -> dict[str, Any] | None:
        with self.session_factory() as db:
            row = db.get(AgentToolExecution, execution_id)
            if row is None:
                return None
            run = db.get(AgentRun, row.run_id)
            if run is None:
                return None
            try:
                arguments = json.loads(row.arguments_json or "{}")
            except json.JSONDecodeError:
                arguments = {}
            try:
                request_payload = json.loads(run.request_json)
            except json.JSONDecodeError:
                request_payload = {}
            return {
                **_execution_dict(row),
                "arguments": arguments if isinstance(arguments, dict) else {},
                "request_payload": request_payload if isinstance(request_payload, dict) else {},
            }

    def next_available(self) -> str | None:
        now = _utcnow()
        with self.session_factory() as db:
            return db.scalar(
                select(AgentToolExecution.execution_id)
                .where(
                    AgentToolExecution.status.in_(["queued", "retry"]),
                    AgentToolExecution.available_at <= now,
                )
                .order_by(AgentToolExecution.available_at, AgentToolExecution.created_at)
                .limit(1)
            )

    def claim(self, execution_id: str, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        now = _utcnow()
        with self.session_factory() as db:
            row = db.scalar(
                select(AgentToolExecution)
                .where(AgentToolExecution.execution_id == execution_id)
                .with_for_update()
            )
            if (
                row is None
                or row.status not in {"queued", "retry"}
                or (row.available_at and row.available_at > now)
            ):
                return None
            row.status = "running"
            row.attempts = int(row.attempts or 0) + 1
            row.lease_owner = worker_id[:160]
            row.lease_expires_at = now + timedelta(seconds=max(5, lease_seconds))
            row.heartbeat_at = now
            row.started_at = now
            row.error = None
            row.error_code = None
            db.commit()
            db.refresh(row)
            return _execution_dict(row)

    def heartbeat(self, execution_id: str, worker_id: str, lease_seconds: int) -> bool:
        now = _utcnow()
        with self.session_factory() as db:
            row = db.get(AgentToolExecution, execution_id)
            if row is None or row.status != "running" or row.lease_owner != worker_id:
                return False
            row.heartbeat_at = now
            row.lease_expires_at = now + timedelta(seconds=max(5, lease_seconds))
            db.commit()
            return True

    def complete(self, execution_id: str, worker_id: str, result: dict[str, Any]) -> bool:
        with self.session_factory() as db:
            row = db.scalar(
                select(AgentToolExecution)
                .where(AgentToolExecution.execution_id == execution_id)
                .with_for_update()
            )
            if row is None or row.status != "running" or row.lease_owner != worker_id:
                return False
            row.status = "completed"
            row.result_json = _json(result)
            row.finished_at = _utcnow()
            row.lease_owner = None
            row.lease_expires_at = None
            row.heartbeat_at = _utcnow()
            db.commit()
            return True

    def fail(
        self,
        execution_id: str,
        worker_id: str,
        *,
        error: str,
        error_code: str,
        retryable: bool,
    ) -> str | None:
        now = _utcnow()
        with self.session_factory() as db:
            row = db.scalar(
                select(AgentToolExecution)
                .where(AgentToolExecution.execution_id == execution_id)
                .with_for_update()
            )
            if row is None or row.status != "running" or row.lease_owner != worker_id:
                return None
            row.error = error[:8000]
            row.error_code = error_code[:80]
            row.lease_owner = None
            row.lease_expires_at = None
            row.heartbeat_at = now
            if retryable and not row.has_side_effects and row.attempts < row.max_attempts:
                delay = min(60, 2 ** max(0, row.attempts - 1))
                row.status = "retry"
                row.next_retry_at = now + timedelta(seconds=delay)
                row.available_at = row.next_retry_at
                db.add(
                    AgentOutbox(
                        run_id=row.run_id,
                        aggregate_type="tool_execution",
                        aggregate_id=row.execution_id,
                        topic="agent.tool.execute",
                        payload_json=_json({"execution_id": row.execution_id}),
                        status="pending",
                        available_at=row.available_at,
                    )
                )
            else:
                row.status = "unknown" if row.has_side_effects else "dead"
                row.finished_at = now
            status = row.status
            db.commit()
            return status

    def recover_expired(self) -> list[dict[str, str]]:
        now = _utcnow()
        recovered: list[dict[str, str]] = []
        with self.session_factory() as db:
            rows = db.scalars(
                select(AgentToolExecution)
                .where(
                    AgentToolExecution.status == "running",
                    AgentToolExecution.lease_expires_at.is_not(None),
                    AgentToolExecution.lease_expires_at < now,
                )
                .with_for_update()
            ).all()
            for row in rows:
                row.lease_owner = None
                row.lease_expires_at = None
                row.error = "Tool worker lease expired before a terminal result was recorded."
                row.error_code = "lease_expired"
                if not row.has_side_effects and row.attempts < row.max_attempts:
                    row.status = "retry"
                    row.available_at = now
                    row.next_retry_at = now
                    db.add(
                        AgentOutbox(
                            run_id=row.run_id,
                            aggregate_type="tool_execution",
                            aggregate_id=row.execution_id,
                            topic="agent.tool.execute",
                            payload_json=_json({"execution_id": row.execution_id}),
                            status="pending",
                            available_at=now,
                        )
                    )
                else:
                    row.status = "unknown" if row.has_side_effects else "dead"
                    row.finished_at = now
                recovered.append({"execution_id": row.execution_id, "status": row.status})
            if rows:
                db.commit()
        return recovered

    def pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        now = _utcnow()
        with self.session_factory() as db:
            rows = db.scalars(
                select(AgentOutbox)
                .where(AgentOutbox.status == "pending", AgentOutbox.available_at <= now)
                .order_by(AgentOutbox.id)
                .limit(max(1, min(limit, 500)))
            ).all()
            result: list[dict[str, Any]] = []
            for row in rows:
                try:
                    payload = json.loads(row.payload_json)
                except json.JSONDecodeError:
                    payload = {}
                result.append(
                    {
                        "id": row.id,
                        "topic": row.topic,
                        "aggregate_id": row.aggregate_id,
                        "payload": payload if isinstance(payload, dict) else {},
                        "attempts": row.attempts,
                    }
                )
            return result

    def mark_outbox_published(self, outbox_id: int, published_ref: str | None) -> None:
        with self.session_factory() as db:
            row = db.get(AgentOutbox, outbox_id)
            if row is None or row.status != "pending":
                return
            row.status = "published"
            row.attempts += 1
            row.published_at = _utcnow()
            row.published_ref = (published_ref or "")[:160] or None
            row.error = None
            db.commit()

    def mark_outbox_failed(self, outbox_id: int, error: str) -> None:
        with self.session_factory() as db:
            row = db.get(AgentOutbox, outbox_id)
            if row is None or row.status != "pending":
                return
            row.attempts += 1
            row.error = error[:2000]
            delay = min(60, 2 ** min(row.attempts, 6))
            row.available_at = _utcnow() + timedelta(seconds=delay)
            db.commit()

    def cancel_for_run(self, run_id: str) -> int:
        with self.session_factory() as db:
            rows = db.scalars(
                select(AgentToolExecution).where(
                    AgentToolExecution.run_id == run_id,
                    AgentToolExecution.status.in_(["queued", "retry"]),
                )
            ).all()
            for row in rows:
                row.status = "cancelled"
                row.finished_at = _utcnow()
            if rows:
                db.commit()
            return len(rows)


tool_execution_repository = ToolExecutionRepository()
