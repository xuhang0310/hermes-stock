from __future__ import annotations

import json
import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import delete, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, sessionmaker

from backend.app.agent_runtime.contracts import StoredRunEvent, TERMINAL_RUN_STATUSES
from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    AiChatSession,
    AgentRun,
    AgentRunCheckpoint,
    AgentRunEvent,
    AgentRunStep,
    AgentDelegation,
    AgentTeamRun,
    AgentToolApproval,
    AgentToolExecution,
)
from backend.app.core.settings import settings


def _utcnow() -> datetime:
    return datetime.utcnow()


def _run_dict(row: AgentRun) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "session_id": row.session_id,
        "user_id": row.user_id,
        "engine": row.engine,
        "chat_mode": row.chat_mode,
        "source_type": row.source_type,
        "source_ref": row.source_ref,
        "parent_run_id": row.parent_run_id,
        "root_run_id": row.root_run_id,
        "team_id": row.team_id,
        "orchestration_depth": row.orchestration_depth,
        "deadline_at": row.deadline_at.isoformat() if row.deadline_at else None,
        "status": row.status,
        "priority": row.priority,
        "last_event_seq": row.last_event_seq,
        "attempts": row.attempts,
        "max_attempts": row.max_attempts,
        "cancel_requested": bool(row.cancel_requested),
        "error": row.error,
        "result_message_id": row.result_message_id,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class AgentRunRepository:
    def __init__(self, session_factory: Callable[[], Session] | sessionmaker[Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    def create_run(
        self,
        *,
        session_id: int,
        user_id: int,
        engine: str,
        chat_mode: str,
        request_payload: dict[str, Any],
        idempotency_key: str | None = None,
        priority: int = 100,
        source_type: str = "web",
        source_ref: str | None = None,
        parent_run_id: str | None = None,
        root_run_id: str | None = None,
        team_id: str | None = None,
        orchestration_depth: int = 0,
        deadline_at: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        normalized_key = (idempotency_key or "").strip()[:120] or None
        with self.session_factory() as db:
            if normalized_key:
                existing = db.scalar(
                    select(AgentRun).where(
                        AgentRun.user_id == user_id,
                        AgentRun.idempotency_key == normalized_key,
                    ).limit(1)
                )
                if existing is not None:
                    return _run_dict(existing), False
            row = AgentRun(
                run_id=uuid.uuid4().hex,
                session_id=session_id,
                user_id=user_id,
                engine=engine,
                chat_mode=chat_mode,
                source_type=(source_type or "web")[:24],
                source_ref=(source_ref or "")[:160] or None,
                parent_run_id=parent_run_id,
                root_run_id=root_run_id,
                team_id=team_id,
                orchestration_depth=max(0, orchestration_depth),
                deadline_at=deadline_at,
                status="queued",
                priority=priority,
                idempotency_key=normalized_key,
                request_json=json.dumps(request_payload, ensure_ascii=False, default=str),
                attempts=1,
                max_attempts=max(1, settings.agent_run_max_attempts),
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                if normalized_key:
                    existing = db.scalar(
                        select(AgentRun).where(
                            AgentRun.user_id == user_id,
                            AgentRun.idempotency_key == normalized_key,
                        ).limit(1)
                    )
                    if existing is not None:
                        return _run_dict(existing), False
                raise
            db.refresh(row)
            return _run_dict(row), True

    def get_by_idempotency_key(self, user_id: int, idempotency_key: str | None) -> dict[str, Any] | None:
        normalized_key = (idempotency_key or "").strip()[:120] or None
        if normalized_key is None:
            return None
        with self.session_factory() as db:
            row = db.scalar(
                select(AgentRun).where(
                    AgentRun.user_id == user_id,
                    AgentRun.idempotency_key == normalized_key,
                ).limit(1)
            )
            return _run_dict(row) if row else None

    def get_run(self, run_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
        with self.session_factory() as db:
            query = select(AgentRun).where(AgentRun.run_id == run_id)
            if user_id is not None:
                query = query.where(AgentRun.user_id == user_id)
            row = db.scalar(query.limit(1))
            return _run_dict(row) if row else None

    def active_runs(
        self,
        user_id: int,
        *,
        chat_mode: str | None = None,
        session_id: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            query = select(AgentRun).where(
                AgentRun.user_id == user_id,
                AgentRun.status.not_in(TERMINAL_RUN_STATUSES),
            )
            if chat_mode:
                query = query.where(AgentRun.chat_mode == chat_mode)
            if session_id is not None:
                query = query.where(AgentRun.session_id == session_id)
            rows = db.scalars(
                query.order_by(AgentRun.created_at.desc()).limit(max(1, min(limit, 100)))
            ).all()
            return [_run_dict(row) for row in rows]

    def admission_counts(self, user_id: int) -> dict[str, int]:
        with self.session_factory() as db:
            user_active = db.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(
                    AgentRun.user_id == user_id,
                    AgentRun.status.not_in(TERMINAL_RUN_STATUSES),
                )
            )
            queued = db.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.status == "queued")
            )
            return {"user_active": int(user_active or 0), "queued": int(queued or 0)}

    def load_request(self, run_id: str) -> dict[str, Any] | None:
        with self.session_factory() as db:
            row = db.get(AgentRun, run_id)
            if row is None:
                return None
            return json.loads(row.request_json)

    def append_event(self, run_id: str, payload: dict[str, Any]) -> StoredRunEvent:
        event = dict(payload)
        event_type = str(event.get("type") or "message")[:80]
        with self.session_factory() as db:
            row = db.scalar(select(AgentRun).where(AgentRun.run_id == run_id).with_for_update())
            if row is None:
                raise KeyError(f"Agent run does not exist: {run_id}")
            row.last_event_seq += 1
            row.heartbeat_at = _utcnow()
            seq = row.last_event_seq
            event["run_id"] = run_id
            event["seq"] = seq
            stored = AgentRunEvent(
                run_id=run_id,
                seq=seq,
                event_type=event_type,
                payload_json=json.dumps(event, ensure_ascii=False, default=str),
            )
            db.add(stored)
            self._project_event(db, row, event, seq)
            db.commit()
            db.refresh(stored)
            return StoredRunEvent(
                run_id=run_id,
                seq=seq,
                event_type=event_type,
                payload=event,
                created_at=stored.created_at.isoformat() if stored.created_at else None,
            )

    @staticmethod
    def _project_event(
        db: Session,
        run: AgentRun,
        event: dict[str, Any],
        seq: int,
    ) -> None:
        """Project the event log into query-friendly step/tool records."""

        event_type = str(event.get("type") or "")
        if event_type == "stage_start":
            db.add(
                AgentRunStep(
                    run_id=run.run_id,
                    seq=seq,
                    step_key=str(event.get("stage") or f"stage-{seq}")[:160],
                    kind="stage",
                    status="running",
                    attempt=run.attempts,
                    input_json=json.dumps(event, ensure_ascii=False, default=str),
                )
            )
            return
        if event_type == "stage_done":
            step = db.scalar(
                select(AgentRunStep)
                .where(
                    AgentRunStep.run_id == run.run_id,
                    AgentRunStep.step_key == str(event.get("stage") or ""),
                    AgentRunStep.status == "running",
                )
                .order_by(AgentRunStep.id.desc())
                .limit(1)
            )
            if step:
                step.status = str(event.get("status") or "completed")[:32]
                step.output_json = json.dumps(event, ensure_ascii=False, default=str)
                step.finished_at = _utcnow()
            return

        tool_call_id = str(event.get("tool_call_id") or "").strip()
        if not tool_call_id:
            return
        tool = db.scalar(
            select(AgentToolExecution).where(
                AgentToolExecution.run_id == run.run_id,
                AgentToolExecution.tool_call_id == tool_call_id,
            ).limit(1)
        )
        if event_type == "action":
            arguments = str(event.get("content") or "")
            if tool is None:
                step_id = db.scalar(
                    select(AgentRunStep.id)
                    .where(AgentRunStep.run_id == run.run_id, AgentRunStep.status == "running")
                    .order_by(AgentRunStep.id.desc())
                    .limit(1)
                )
                db.add(
                    AgentToolExecution(
                        execution_id=uuid.uuid4().hex,
                        run_id=run.run_id,
                        step_id=step_id,
                        tool_call_id=tool_call_id[:160],
                        tool_name=str(event.get("tool") or "unknown")[:120],
                        risk_level=str(event.get("risk_level") or "low")[:24],
                        status="running",
                        arguments_sha256=str(
                            event.get("arguments_sha256")
                            or hashlib.sha256(arguments.encode("utf-8")).hexdigest()
                        )[:64],
                        arguments_json=arguments,
                        has_side_effects=bool(event.get("has_side_effects")),
                    )
                )
            return
        if tool is None:
            return
        if event_type == "approval_required":
            tool.status = "waiting_approval"
            tool.risk_level = str(event.get("risk_level") or tool.risk_level or "high")[:24]
        elif event_type == "approval_resolved":
            decision = str(event.get("decision") or "denied")
            tool.status = "running" if decision == "approved" else decision[:32]
            if decision != "approved":
                tool.finished_at = _utcnow()
        elif event_type == "tool_done":
            if tool.execution_mode == "inline":
                tool.status = "completed" if event.get("success") else "failed"
                tool.result_json = json.dumps(
                    {"success": bool(event.get("success")), "display_name": event.get("display_name")},
                    ensure_ascii=False,
                )
                tool.finished_at = _utcnow()

    def events_after(self, run_id: str, seq: int, *, limit: int = 500) -> list[StoredRunEvent]:
        with self.session_factory() as db:
            rows = db.scalars(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run_id, AgentRunEvent.seq > max(0, seq))
                .order_by(AgentRunEvent.seq)
                .limit(max(1, min(limit, 2000)))
            ).all()
            return [
                StoredRunEvent(
                    run_id=row.run_id,
                    seq=row.seq,
                    event_type=row.event_type,
                    payload=json.loads(row.payload_json),
                    created_at=row.created_at.isoformat() if row.created_at else None,
                )
                for row in rows
            ]

    def next_queued_run(self) -> str | None:
        with self.session_factory() as db:
            active = aliased(AgentRun)
            return db.scalar(
                select(AgentRun.run_id)
                .where(
                    AgentRun.status == "queued",
                    AgentRun.cancel_requested.is_(False),
                    ~exists(
                        select(active.run_id).where(
                            active.session_id == AgentRun.session_id,
                            active.run_id != AgentRun.run_id,
                            active.status.in_(["running", "waiting_approval", "waiting_children"]),
                        )
                    ),
                )
                .order_by(AgentRun.priority.desc(), AgentRun.created_at, AgentRun.run_id)
                .limit(1)
            )

    def claim_run(self, run_id: str, worker_id: str) -> dict[str, Any] | None:
        now = _utcnow()
        with self.session_factory() as db:
            session_id = db.scalar(select(AgentRun.session_id).where(AgentRun.run_id == run_id))
            if session_id is None:
                return None
            # Claimants lock the shared session before an individual run so
            # concurrent turns in one conversation use a consistent order.
            db.scalar(select(AiChatSession.id).where(AiChatSession.id == session_id).with_for_update())
            row = db.scalar(select(AgentRun).where(AgentRun.run_id == run_id).with_for_update())
            if row is None or row.status != "queued" or row.cancel_requested:
                return None
            other_active = db.scalar(
                select(AgentRun.run_id).where(
                    AgentRun.session_id == row.session_id,
                    AgentRun.run_id != row.run_id,
                    AgentRun.status.in_(["running", "waiting_approval", "waiting_children"]),
                ).limit(1)
            )
            if other_active:
                return None
            if row.attempts > row.max_attempts:
                row.status = "failed"
                row.error = "Agent run exceeded its retry limit."
                row.finished_at = now
                db.commit()
                return None
            row.status = "running"
            row.worker_id = worker_id[:160]
            row.started_at = row.started_at or now
            row.heartbeat_at = now
            delegation = db.scalar(
                select(AgentDelegation).where(AgentDelegation.child_run_id == row.run_id).limit(1)
            )
            if delegation is not None and delegation.status == "queued":
                delegation.status = "running"
                delegation.started_at = now
            db.commit()
            db.refresh(row)
            return _run_dict(row)

    def touch(self, run_id: str, worker_id: str) -> bool:
        with self.session_factory() as db:
            row = db.get(AgentRun, run_id)
            if row is None or row.worker_id != worker_id or row.status != "running":
                return False
            row.heartbeat_at = _utcnow()
            db.commit()
            return True

    def is_cancel_requested(self, run_id: str) -> bool:
        with self.session_factory() as db:
            value = db.scalar(select(AgentRun.cancel_requested).where(AgentRun.run_id == run_id))
            return bool(value)

    def has_side_effects(self, run_id: str) -> bool:
        with self.session_factory() as db:
            value = db.scalar(
                select(AgentToolExecution.execution_id).where(
                    AgentToolExecution.run_id == run_id,
                    AgentToolExecution.has_side_effects.is_(True),
                ).limit(1)
            )
            return value is not None

    def request_cancel(self, run_id: str, *, user_id: int) -> dict[str, Any] | None:
        now = _utcnow()
        with self.session_factory() as db:
            row = db.scalar(
                select(AgentRun).where(AgentRun.run_id == run_id, AgentRun.user_id == user_id).with_for_update()
            )
            if row is None:
                return None
            if row.status in TERMINAL_RUN_STATUSES:
                result = _run_dict(row)
                result["cancelled_now"] = False
                return result
            row.cancel_requested = True
            cancelled_now = False
            if row.status in {"queued", "waiting_approval", "waiting_children"}:
                row.status = "cancelled"
                row.finished_at = now
                cancelled_now = True
            db.commit()
            db.refresh(row)
            result = _run_dict(row)
            result["cancelled_now"] = cancelled_now
            return result

    def mark_waiting(self, run_id: str) -> bool:
        """Park a run, or requeue it when approval resolved before parking."""

        with self.session_factory() as db:
            row = db.scalar(select(AgentRun).where(AgentRun.run_id == run_id).with_for_update())
            if row and row.status == "running":
                has_pending = db.scalar(
                    select(AgentToolApproval.approval_id).where(
                        AgentToolApproval.run_id == run_id,
                        AgentToolApproval.status == "pending",
                    ).limit(1)
                )
                row.status = "waiting_approval" if has_pending else "queued"
                row.worker_id = None
                row.heartbeat_at = _utcnow()
                db.commit()
                return not bool(has_pending)
            return False

    def mark_waiting_children(self, run_id: str) -> bool:
        """Park a leader run unless every child already finished before parking."""

        with self.session_factory() as db:
            row = db.scalar(select(AgentRun).where(AgentRun.run_id == run_id).with_for_update())
            if row is None or row.status != "running":
                return False
            active_team = db.scalar(
                select(AgentTeamRun.team_id).where(
                    AgentTeamRun.leader_run_id == run_id,
                    AgentTeamRun.status == "running",
                ).limit(1)
            )
            row.status = "waiting_children" if active_team else "queued"
            row.worker_id = None
            row.heartbeat_at = _utcnow()
            db.commit()
            return active_team is None

    def requeue(self, run_id: str, *, error: str | None = None) -> bool:
        with self.session_factory() as db:
            row = db.scalar(select(AgentRun).where(AgentRun.run_id == run_id).with_for_update())
            if row is None or row.status in TERMINAL_RUN_STATUSES:
                return False
            if error and row.attempts >= row.max_attempts:
                row.status = "failed"
                row.error = (error or "Agent run exceeded its retry limit.")[:4000]
                row.finished_at = _utcnow()
                db.commit()
                return False
            if error:
                row.attempts += 1
            row.status = "queued"
            row.worker_id = None
            row.error = (error or "")[:4000] or None
            db.commit()
            return True

    def mark_terminal(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
        result_message_id: int | None = None,
    ) -> None:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"Invalid terminal status: {status}")
        with self.session_factory() as db:
            row = db.get(AgentRun, run_id)
            if row is None:
                return
            terminal_at = _utcnow()
            row.status = status
            row.error = error[:16000] if error else None
            row.result_message_id = result_message_id
            row.worker_id = None
            row.finished_at = terminal_at
            row.heartbeat_at = terminal_at
            db.query(AgentRunStep).filter(
                AgentRunStep.run_id == run_id,
                AgentRunStep.status == "running",
            ).update(
                {
                    AgentRunStep.status: "failed" if status == "failed" else status,
                    AgentRunStep.error: error[:16000] if error else None,
                    AgentRunStep.finished_at: terminal_at,
                },
                synchronize_session=False,
            )
            tool_status = "failed" if status == "failed" else "cancelled"
            tool_error = (error or f"Agent run ended with status {status} before the tool completed.")[:16000]
            db.query(AgentToolExecution).filter(
                AgentToolExecution.run_id == run_id,
                AgentToolExecution.execution_mode == "inline",
                AgentToolExecution.status.in_(["running", "waiting_approval"]),
            ).update(
                {
                    AgentToolExecution.status: tool_status,
                    AgentToolExecution.error: tool_error,
                    AgentToolExecution.finished_at: terminal_at,
                },
                synchronize_session=False,
            )
            db.commit()

    def save_checkpoint(
        self,
        run_id: str,
        *,
        engine: str,
        state_json: str,
        waiting: list[dict[str, Any]] | None = None,
    ) -> None:
        with self.session_factory() as db:
            row = db.get(AgentRunCheckpoint, run_id)
            waiting_json = json.dumps(waiting or [], ensure_ascii=False, default=str)
            if row is None:
                row = AgentRunCheckpoint(
                    run_id=run_id,
                    engine=engine,
                    state_json=state_json,
                    waiting_json=waiting_json,
                    version=1,
                )
                db.add(row)
            else:
                row.engine = engine
                row.state_json = state_json
                row.waiting_json = waiting_json
                row.version += 1
            db.commit()

    def load_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        with self.session_factory() as db:
            row = db.get(AgentRunCheckpoint, run_id)
            if row is None:
                return None
            try:
                waiting = json.loads(row.waiting_json or "[]")
            except json.JSONDecodeError:
                waiting = []
            return {
                "engine": row.engine,
                "state_json": row.state_json,
                "waiting": waiting,
                "version": row.version,
            }

    def recover_stale_runs(self, stale_after_seconds: int) -> list[str]:
        cutoff = _utcnow() - timedelta(seconds=max(30, stale_after_seconds))
        recovered: list[str] = []
        with self.session_factory() as db:
            rows = db.scalars(
                select(AgentRun).where(
                    AgentRun.status == "running",
                    AgentRun.heartbeat_at < cutoff,
                ).with_for_update()
            ).all()
            for row in rows:
                if row.attempts >= row.max_attempts:
                    row.status = "failed"
                    row.error = "Worker heartbeat expired and retry limit was reached."
                    row.finished_at = _utcnow()
                else:
                    row.attempts += 1
                    row.status = "queued"
                    row.worker_id = None
                    row.error = "Recovered after worker heartbeat expired."
                    recovered.append(row.run_id)
            db.commit()
        return recovered

    def prune_terminal_events(self, retention_days: int) -> int:
        """Delete replay events only after their run has reached a durable terminal state."""

        cutoff = _utcnow() - timedelta(days=max(1, retention_days))
        terminal_runs = select(AgentRun.run_id).where(
            AgentRun.status.in_(TERMINAL_RUN_STATUSES),
            AgentRun.finished_at.is_not(None),
            AgentRun.finished_at < cutoff,
        )
        with self.session_factory() as db:
            result = db.execute(
                delete(AgentRunEvent).where(
                    AgentRunEvent.created_at < cutoff,
                    AgentRunEvent.run_id.in_(terminal_runs),
                )
            )
            db.commit()
            return int(result.rowcount or 0)

agent_run_repository = AgentRunRepository()
