from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.agent_runtime.repository import AgentRunRepository, agent_run_repository
from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    AiChatMessage,
    AiChatSession,
    AgentDelegation,
    AgentRun,
    AgentTeamRun,
    DigitalHumanProfile,
    User,
)
from backend.app.core.settings import settings
from backend.app.agent_runtime.profile_access import target_roles_allow


DELEGATABLE_EXCLUDED_CODES = frozenset(
    {"stock-pm", "ask_stock", "position-entry-agent", "watchlist-entry-agent"}
)
TERMINAL_DELEGATION_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "timed_out"}
)
MAX_MEMBER_RESULT_CHARS = 12_000


def _utcnow() -> datetime:
    return datetime.utcnow()


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


class AgentTeamService:
    """Durable fan-out/fan-in orchestration for the stock PM Agent."""

    def __init__(
        self,
        session_factory: Callable[[], Session] | sessionmaker[Session] = SessionLocal,
        repository: AgentRunRepository = agent_run_repository,
    ) -> None:
        self.session_factory = session_factory
        self.repository = repository

    def list_delegatable_agents(self, user_id: int | None = None) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            user = db.get(User, user_id) if user_id is not None else None
            rows = db.scalars(
                select(DigitalHumanProfile).where(
                    DigitalHumanProfile.status == "enabled",
                    DigitalHumanProfile.delegation_enabled.is_(True),
                    DigitalHumanProfile.code.not_in(DELEGATABLE_EXCLUDED_CODES),
                ).order_by(DigitalHumanProfile.sort_order, DigitalHumanProfile.id)
            ).all()
            if user is not None:
                rows = [row for row in rows if target_roles_allow(row.target_roles, user.role)]
            return [
                {
                    "code": row.code,
                    "name": row.name,
                    "description": row.delegation_description or row.description,
                    "read_only": True,
                }
                for row in rows
            ]

    def start_team(
        self,
        *,
        leader_run_id: str,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        tasks = arguments.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("至少需要一个数字人子任务。")
        max_members = max(1, min(settings.agent_team_max_members, 12))
        if len(tasks) > max_members:
            raise ValueError(f"一次最多可委派 {max_members} 个子任务。")

        objective = str(arguments.get("objective") or "").strip()
        timeout_seconds = int(arguments.get("timeout_seconds") or settings.agent_team_timeout_seconds)
        timeout_seconds = max(60, min(timeout_seconds, 1800))
        deadline_at = _utcnow() + timedelta(seconds=timeout_seconds)

        child_runs: list[dict[str, Any]] = []
        with self.session_factory() as db:
            leader = db.scalar(
                select(AgentRun).where(AgentRun.run_id == leader_run_id).with_for_update()
            )
            if leader is None:
                raise ValueError("总调度 Run 不存在。")
            if leader.cancel_requested or leader.status != "running":
                raise RuntimeError("总调度任务已取消或不再运行，拒绝创建子任务。")
            existing = db.scalar(
                select(AgentTeamRun).where(
                    AgentTeamRun.leader_run_id == leader_run_id,
                    AgentTeamRun.tool_call_id == tool_call_id,
                ).limit(1)
            )
            if existing is not None:
                return {**self._team_dict(db, existing), "created": False}

            leader_request = _loads(leader.request_json, {})
            if str(leader_request.get("digital_human_code") or "") != "stock-pm":
                raise PermissionError("只有总调度数字人可以创建 Agent Team。")
            if int(leader.orchestration_depth or 0) >= 1:
                raise PermissionError("子数字人不能继续委派任务。")
            owner = db.get(User, leader.user_id)
            if owner is None or not owner.is_enabled:
                raise PermissionError("Agent Team 所有者不存在或已停用。")

            normalized: list[tuple[DigitalHumanProfile, str, str | None, bool]] = []
            for task in tasks:
                if not isinstance(task, dict):
                    raise ValueError("子任务必须是结构化对象。")
                profile_code = str(task.get("agent_code") or "").strip()
                instruction = str(task.get("instruction") or "").strip()
                expected_output = str(task.get("expected_output") or "").strip() or None
                required = bool(task.get("required", True))
                if not profile_code or not instruction:
                    raise ValueError("每个子任务都必须包含 agent_code 和 instruction。")
                if len(instruction) > 8000:
                    raise ValueError("单个子任务说明不能超过 8000 字符。")
                profile = db.scalar(
                    select(DigitalHumanProfile).where(
                        DigitalHumanProfile.code == profile_code,
                        DigitalHumanProfile.status == "enabled",
                        DigitalHumanProfile.delegation_enabled.is_(True),
                    ).limit(1)
                )
                if profile is None or profile.code in DELEGATABLE_EXCLUDED_CODES:
                    raise ValueError(f"数字人 {profile_code} 不可委派。")
                if not target_roles_allow(profile.target_roles, owner.role):
                    raise PermissionError(f"当前用户角色无权调用数字人 {profile_code}。")
                normalized.append((profile, instruction, expected_output, required))

            team_id = uuid.uuid4().hex
            root_run_id = leader.root_run_id or leader.run_id
            leader_engine = leader.engine
            objective = objective or str(leader_request.get("question") or "协同完成总任务")
            team = AgentTeamRun(
                team_id=team_id,
                leader_run_id=leader.run_id,
                root_run_id=root_run_id,
                user_id=leader.user_id,
                tool_call_id=tool_call_id[:160],
                objective=objective,
                status="running",
                max_members=max_members,
                deadline_at=deadline_at,
            )
            db.add(team)
            leader.team_id = team_id

            for index, (profile, instruction, expected_output, required) in enumerate(normalized, start=1):
                child_run_id = uuid.uuid4().hex
                external_id = f"{team_id}:{index}"
                child_session = AiChatSession(
                    user_id=leader.user_id,
                    digital_human_id=profile.id,
                    model_config_id=leader_request.get("model_config_id"),
                    title=f"Agent Team · {profile.name}"[:160],
                    status="active",
                    chat_mode="research",
                    source_type="delegation",
                    external_conversation_id=external_id[:160],
                    source_label="Agent Team",
                )
                db.add(child_session)
                db.flush()

                child_question = self._child_question(
                    objective=objective,
                    instruction=instruction,
                    expected_output=expected_output,
                    leader_question=str(leader_request.get("question") or ""),
                )
                db.add(
                    AiChatMessage(
                        session_id=child_session.id,
                        role="user",
                        content=child_question,
                        event_count=0,
                    )
                )
                digital_context = "\n".join(
                    [
                        f"当前数字人：{profile.name}",
                        f"风格：{profile.avatar_tone}",
                        f"职责：{profile.description}",
                        f"系统提示：{profile.system_prompt}",
                        "当前任务来自总调度 Agent Team。只完成分配给你的子任务，不要继续委派。",
                    ]
                )
                child_request = {
                    "session_id": child_session.id,
                    "user_id": leader.user_id,
                    "user_label": str(leader_request.get("user_label") or "team-owner"),
                    "question": child_question,
                    "user_text": child_question,
                    "attachments": list(leader_request.get("attachments") or []),
                    "history": [],
                    "digital_human_id": profile.id,
                    "digital_human_code": profile.code,
                    "digital_human_context": digital_context,
                    "model_config_id": leader_request.get("model_config_id"),
                    "chat_mode": "research",
                    "skills": [],
                    "memory_context": "",
                    "can_write_positions": False,
                    "allow_code_execution": False,
                    "approval_policy": "full",
                    "delegation_read_only": True,
                    "source_type": "delegation",
                    "source_ref": team_id,
                    "parent_run_id": leader.run_id,
                    "root_run_id": root_run_id,
                    "team_id": team_id,
                    "orchestration_depth": 1,
                }
                child_run = AgentRun(
                    run_id=child_run_id,
                    session_id=child_session.id,
                    user_id=leader.user_id,
                    engine=leader_engine,
                    chat_mode="research",
                    source_type="delegation",
                    source_ref=team_id,
                    parent_run_id=leader.run_id,
                    root_run_id=root_run_id,
                    team_id=team_id,
                    orchestration_depth=1,
                    deadline_at=deadline_at,
                    status="queued",
                    priority=250,
                    idempotency_key=f"team:{team_id}:{index}",
                    request_json=json.dumps(child_request, ensure_ascii=False, default=str),
                    attempts=1,
                    max_attempts=max(1, settings.agent_run_max_attempts),
                )
                db.add(child_run)
                delegation = AgentDelegation(
                    delegation_id=uuid.uuid4().hex,
                    team_id=team_id,
                    parent_run_id=leader.run_id,
                    child_run_id=child_run_id,
                    member_index=index,
                    target_profile_id=profile.id,
                    target_profile_code=profile.code,
                    target_profile_name=profile.name,
                    instruction=instruction,
                    expected_output=expected_output,
                    required=required,
                    status="queued",
                    deadline_at=deadline_at,
                )
                db.add(delegation)
                child_runs.append(
                    {
                        "delegation_id": delegation.delegation_id,
                        "run_id": child_run_id,
                        "session_id": child_session.id,
                        "agent_code": profile.code,
                        "agent_name": profile.name,
                        "status": "queued",
                    }
                )
            db.commit()

        for child in child_runs:
            self.repository.append_event(
                child["run_id"],
                {
                    "type": "session",
                    "session_id": child["session_id"],
                    "chat_mode": "research",
                    "source_type": "delegation",
                    "team_id": team_id,
                },
            )
            self.repository.append_event(
                child["run_id"],
                {"type": "queued", "engine": leader_engine, "status": "queued", "team_id": team_id},
            )
        return {
            "team_id": team_id,
            "leader_run_id": leader_run_id,
            "objective": objective,
            "status": "running",
            "deadline_at": deadline_at.isoformat(),
            "members": child_runs,
            "created": True,
        }

    def on_child_terminal(self, child_run_id: str) -> dict[str, Any] | None:
        event_payloads: list[dict[str, Any]] = []
        parent_run_id: str | None = None
        parent_requeued = False
        with self.session_factory() as db:
            delegation = db.scalar(
                select(AgentDelegation)
                .where(AgentDelegation.child_run_id == child_run_id)
                .with_for_update()
            )
            if delegation is None:
                return None
            child = db.get(AgentRun, child_run_id)
            if child is None or child.status not in {"completed", "failed", "cancelled"}:
                return None
            parent_run_id = delegation.parent_run_id
            if delegation.status not in TERMINAL_DELEGATION_STATUSES:
                answer = None
                answer_truncated = False
                if child.result_message_id:
                    message = db.get(AiChatMessage, child.result_message_id)
                    answer = message.content if message else None
                    if answer and len(answer) > MAX_MEMBER_RESULT_CHARS:
                        answer = answer[:MAX_MEMBER_RESULT_CHARS]
                        answer_truncated = True
                delegation.status = child.status
                delegation.error = child.error
                delegation.result_json = json.dumps(
                    {
                        "run_id": child.run_id,
                        "agent_code": delegation.target_profile_code,
                        "agent_name": delegation.target_profile_name,
                        "status": child.status,
                        "answer": answer,
                        "answer_truncated": answer_truncated,
                        "error": child.error,
                    },
                    ensure_ascii=False,
                    default=str,
                )
                delegation.finished_at = child.finished_at or _utcnow()
                event_payloads.append(
                    {
                        "type": "team_member_done",
                        "team_id": delegation.team_id,
                        "delegation_id": delegation.delegation_id,
                        "child_run_id": child.run_id,
                        "agent_code": delegation.target_profile_code,
                        "agent_name": delegation.target_profile_name,
                        "status": delegation.status,
                    }
                )

            team = db.scalar(
                select(AgentTeamRun).where(AgentTeamRun.team_id == delegation.team_id).with_for_update()
            )
            if team is None:
                db.commit()
                return None
            completion = self._finalize_team_locked(db, team, allow_timeout=False)
            if completion is not None:
                parent_requeued = completion["parent_requeued"]
                event_payloads.append(completion["event"])
            db.commit()

        event_seqs: list[int] = []
        for payload in event_payloads:
            if parent_run_id:
                stored = self.repository.append_event(parent_run_id, payload)
                event_seqs.append(stored.seq)
        return {
            "parent_run_id": parent_run_id,
            "parent_requeued": parent_requeued,
            "events": event_payloads,
            "event_seqs": event_seqs,
        }

    def result_for_tool_call(self, leader_run_id: str, tool_call_id: str) -> dict[str, Any] | None:
        with self.session_factory() as db:
            team = db.scalar(
                select(AgentTeamRun).where(
                    AgentTeamRun.leader_run_id == leader_run_id,
                    AgentTeamRun.tool_call_id == tool_call_id,
                ).limit(1)
            )
            if team is None or team.status == "running":
                return None
            result = _loads(team.result_json, {})
            return {
                "team_id": team.team_id,
                "status": team.status,
                "objective": team.objective,
                "members": result.get("members", []),
                "summary": result.get("summary"),
                "error": team.error,
            }

    def team_for_run(self, run_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
        with self.session_factory() as db:
            query = select(AgentTeamRun).where(
                (AgentTeamRun.leader_run_id == run_id) | (AgentTeamRun.team_id == run_id)
            )
            if user_id is not None:
                query = query.where(AgentTeamRun.user_id == user_id)
            team = db.scalar(query.order_by(AgentTeamRun.created_at.desc()).limit(1))
            return self._team_dict(db, team) if team else None

    def reconcile(self) -> list[str]:
        """Recover missed child callbacks and expire overdue Team runs."""

        with self.session_factory() as db:
            child_ids = list(
                db.scalars(
                    select(AgentDelegation.child_run_id)
                    .join(AgentRun, AgentRun.run_id == AgentDelegation.child_run_id)
                    .where(
                        AgentDelegation.status.not_in(TERMINAL_DELEGATION_STATUSES),
                        AgentRun.status.in_(["completed", "failed", "cancelled"]),
                    )
                    .limit(100)
                ).all()
            )
        requeued: set[str] = set()
        for child_run_id in child_ids:
            result = self.on_child_terminal(child_run_id)
            if result and result.get("parent_requeued") and result.get("parent_run_id"):
                requeued.add(str(result["parent_run_id"]))

        events: list[tuple[str, dict[str, Any]]] = []
        with self.session_factory() as db:
            overdue = db.scalars(
                select(AgentTeamRun).where(
                    AgentTeamRun.status == "running",
                    AgentTeamRun.deadline_at <= _utcnow(),
                ).with_for_update()
            ).all()
            for team in overdue:
                completion = self._finalize_team_locked(db, team, allow_timeout=True)
                if completion is None:
                    continue
                events.append((team.leader_run_id, completion["event"]))
                if completion["parent_requeued"]:
                    requeued.add(team.leader_run_id)
            db.commit()
        for run_id, payload in events:
            self.repository.append_event(run_id, payload)
        return sorted(requeued)

    def cancel_for_parent(self, leader_run_id: str) -> list[str]:
        cancelled_children: list[str] = []
        with self.session_factory() as db:
            teams = db.scalars(
                select(AgentTeamRun).where(
                    AgentTeamRun.leader_run_id == leader_run_id,
                    AgentTeamRun.status == "running",
                ).with_for_update()
            ).all()
            for team in teams:
                team.status = "cancelled"
                team.finished_at = _utcnow()
                delegations = db.scalars(
                    select(AgentDelegation).where(AgentDelegation.team_id == team.team_id)
                ).all()
                for delegation in delegations:
                    if delegation.status in TERMINAL_DELEGATION_STATUSES:
                        continue
                    child = db.get(AgentRun, delegation.child_run_id)
                    if child and child.status not in {"completed", "failed", "cancelled"}:
                        child.cancel_requested = True
                        if child.status in {"queued", "waiting_approval", "waiting_children"}:
                            child.status = "cancelled"
                            child.finished_at = _utcnow()
                        cancelled_children.append(child.run_id)
                    delegation.status = "cancelled"
                    delegation.finished_at = _utcnow()
            db.commit()
        return cancelled_children

    def _finalize_team_locked(
        self,
        db: Session,
        team: AgentTeamRun,
        *,
        allow_timeout: bool,
    ) -> dict[str, Any] | None:
        if team.status != "running":
            return None
        delegations = list(
            db.scalars(
                select(AgentDelegation)
                .where(AgentDelegation.team_id == team.team_id)
                .order_by(AgentDelegation.member_index)
            ).all()
        )
        now = _utcnow()
        unfinished = [row for row in delegations if row.status not in TERMINAL_DELEGATION_STATUSES]
        if unfinished and not allow_timeout:
            return None
        if unfinished:
            for row in unfinished:
                row.status = "timed_out"
                row.error = "子任务超过 Team 截止时间。"
                row.finished_at = now
                child = db.get(AgentRun, row.child_run_id)
                if child and child.status not in {"completed", "failed", "cancelled"}:
                    child.cancel_requested = True
                    if child.status in {"queued", "waiting_approval", "waiting_children"}:
                        child.status = "cancelled"
                        child.error = row.error
                        child.finished_at = now

        members = [
            _loads(row.result_json, {
                "run_id": row.child_run_id,
                "agent_code": row.target_profile_code,
                "agent_name": row.target_profile_name,
                "status": row.status,
                "answer": None,
                "error": row.error,
            })
            for row in delegations
        ]
        completed = sum(1 for row in delegations if row.status == "completed")
        failed = len(delegations) - completed
        if completed == len(delegations):
            team.status = "completed"
        elif completed:
            team.status = "partial"
        else:
            team.status = "failed"
        team.finished_at = now
        team.error = None if failed == 0 else f"{failed} 个子任务未成功完成。"
        summary = f"{completed}/{len(delegations)} 个子任务完成"
        team.result_json = json.dumps(
            {"team_id": team.team_id, "status": team.status, "summary": summary, "members": members},
            ensure_ascii=False,
            default=str,
        )

        parent = db.scalar(
            select(AgentRun).where(AgentRun.run_id == team.leader_run_id).with_for_update()
        )
        parent_requeued = False
        other_active_team = db.scalar(
            select(AgentTeamRun.team_id).where(
                AgentTeamRun.leader_run_id == team.leader_run_id,
                AgentTeamRun.team_id != team.team_id,
                AgentTeamRun.status == "running",
            ).limit(1)
        )
        if (
            parent is not None
            and parent.status == "waiting_children"
            and not parent.cancel_requested
            and other_active_team is None
        ):
            parent.status = "queued"
            parent.worker_id = None
            parent.heartbeat_at = now
            parent_requeued = True
        return {
            "parent_requeued": parent_requeued,
            "event": {
                "type": "team_done",
                "team_id": team.team_id,
                "status": team.status,
                "completed": completed,
                "total": len(delegations),
                "summary": summary,
            },
        }

    def _team_dict(self, db: Session, team: AgentTeamRun) -> dict[str, Any]:
        members = db.scalars(
            select(AgentDelegation)
            .where(AgentDelegation.team_id == team.team_id)
            .order_by(AgentDelegation.member_index)
        ).all()
        return {
            "team_id": team.team_id,
            "leader_run_id": team.leader_run_id,
            "root_run_id": team.root_run_id,
            "objective": team.objective,
            "status": team.status,
            "deadline_at": team.deadline_at.isoformat(),
            "finished_at": team.finished_at.isoformat() if team.finished_at else None,
            "error": team.error,
            "members": [
                {
                    "delegation_id": row.delegation_id,
                    "run_id": row.child_run_id,
                    "agent_code": row.target_profile_code,
                    "agent_name": row.target_profile_name,
                    "instruction": row.instruction,
                    "expected_output": row.expected_output,
                    "required": row.required,
                    "status": row.status,
                    "error": row.error,
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                    "result": _loads(row.result_json, None),
                }
                for row in members
            ],
        }

    @staticmethod
    def _child_question(
        *,
        objective: str,
        instruction: str,
        expected_output: str | None,
        leader_question: str,
    ) -> str:
        parts = [
            "你是 Agent Team 的专业成员，请独立完成下面的子任务。",
            f"总任务：{objective}",
            f"你的子任务：{instruction}",
        ]
        if expected_output:
            parts.append(f"交付要求：{expected_output}")
        if leader_question and leader_question != objective:
            parts.append(f"用户原始问题：{leader_question}")
        parts.append("请给出可直接交给总调度统稿的结论、证据、风险和未解决问题。")
        return "\n\n".join(parts)


agent_team_service = AgentTeamService()
