from __future__ import annotations

import json
import re
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


DELEGATABLE_EXCLUDED_CODES = frozenset({"stock-pm", "ask_stock"})
TRANSACTION_DELEGATION_CODES = frozenset({"position-entry-agent", "watchlist-entry-agent"})
AGENT_CODE_ALIASES = {
    "stock-researcher": "research-analyst",
    "stock-research-analyst": "research-analyst",
    "researcher": "research-analyst",
    "individual-stock-researcher": "research-analyst",
    "个股研究员": "research-analyst",
    "risk-manager": "risk-officer",
    "risk-analyst": "risk-officer",
    "风控官": "risk-officer",
    "风控复盘官": "risk-officer",
    "intelligence-agent": "intelligence-officer",
    "news-agent": "intelligence-officer",
    "情报官": "intelligence-officer",
    "quant-agent": "quant-analyst",
    "量化分析师": "quant-analyst",
    "position-agent": "position-entry-agent",
    "position-entry": "position-entry-agent",
    "portfolio-entry-agent": "position-entry-agent",
    "holding-entry-agent": "position-entry-agent",
    "持仓agent": "position-entry-agent",
    "持仓录入agent": "position-entry-agent",
    "持仓录入": "position-entry-agent",
    "watchlist-agent": "watchlist-entry-agent",
    "watchlist-entry": "watchlist-entry-agent",
    "自选agent": "watchlist-entry-agent",
    "自选录入agent": "watchlist-entry-agent",
    "自选录入": "watchlist-entry-agent",
}
TERMINAL_DELEGATION_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "timed_out"}
)
MAX_MEMBER_RESULT_CHARS = 12_000
MAX_TEAM_MEMBERS = 20
MAX_AGENT_WORK_PACKAGE_CHARS = 24_000


def _utcnow() -> datetime:
    return datetime.utcnow()


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _agent_code_key(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[\s_]+", "-", value.strip().casefold())).strip("-")


def _agent_access(profile: DigitalHumanProfile) -> dict[str, Any]:
    transaction_agent = profile.code in TRANSACTION_DELEGATION_CODES
    return {
        "read_only": not transaction_agent,
        "business_write_scope": (
            "positions"
            if profile.code == "position-entry-agent"
            else "watchlist"
            if profile.code == "watchlist-entry-agent"
            else None
        ),
    }


def _merge_agent_work_items(items: list[dict[str, Any]]) -> tuple[str, str | None]:
    if len(items) == 1:
        return str(items[0]["instruction"]), items[0].get("expected_output")

    instruction_lines = [
        "请在同一次执行中完成以下工作项，不要拆成多个独立任务：",
        *[
            f"{index}. {item['instruction']}"
            for index, item in enumerate(items, start=1)
        ],
    ]
    expected_lines = [
        f"{index}. {item.get('expected_output') or '完成对应工作项，并明确结论、证据与缺口。'}"
        for index, item in enumerate(items, start=1)
    ]
    return "\n".join(instruction_lines), "请按工作项分别交付：\n" + "\n".join(expected_lines)


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
                    **_agent_access(row),
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
        max_members = max(1, min(settings.agent_team_max_members, MAX_TEAM_MEMBERS))

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

            available_profiles = list(
                db.scalars(
                    select(DigitalHumanProfile).where(
                        DigitalHumanProfile.status == "enabled",
                        DigitalHumanProfile.delegation_enabled.is_(True),
                        DigitalHumanProfile.code.not_in(DELEGATABLE_EXCLUDED_CODES),
                    )
                ).all()
            )
            available_profiles = [
                profile
                for profile in available_profiles
                if target_roles_allow(profile.target_roles, owner.role)
            ]
            profiles_by_code = {profile.code: profile for profile in available_profiles}
            profiles_by_key = {
                key: profile
                for profile in available_profiles
                for key in {_agent_code_key(profile.code), _agent_code_key(profile.name)}
            }
            available_codes = sorted(profiles_by_code)
            grouped: dict[str, dict[str, Any]] = {}
            rejected_tasks: list[dict[str, Any]] = []
            for index, task in enumerate(tasks, start=1):
                if not isinstance(task, dict):
                    rejected_tasks.append(
                        {
                            "member_index": index,
                            "requested_agent_code": None,
                            "status": "rejected",
                            "required": True,
                            "error": "子任务必须是结构化对象。",
                        }
                    )
                    continue
                profile_code = str(task.get("agent_code") or "").strip()
                instruction = str(task.get("instruction") or "").strip()
                expected_output = str(task.get("expected_output") or "").strip() or None
                required = bool(task.get("required", True))
                if not profile_code or not instruction:
                    rejected_tasks.append(
                        {
                            "member_index": index,
                            "requested_agent_code": profile_code or None,
                            "status": "rejected",
                            "required": required,
                            "error": "子任务必须同时包含 agent_code 和 instruction。",
                        }
                    )
                    continue
                if len(instruction) > 8000:
                    rejected_tasks.append(
                        {
                            "member_index": index,
                            "requested_agent_code": profile_code,
                            "status": "rejected",
                            "required": required,
                            "error": "单个子任务说明不能超过 8000 字符。",
                        }
                    )
                    continue
                requested_key = _agent_code_key(profile_code)
                resolved_code = AGENT_CODE_ALIASES.get(requested_key, profile_code)
                profile = profiles_by_code.get(resolved_code) or profiles_by_key.get(requested_key)
                if profile is None:
                    rejected_tasks.append(
                        {
                            "member_index": index,
                            "requested_agent_code": profile_code,
                            "status": "rejected",
                            "required": required,
                            "error": (
                                f"数字人 {profile_code} 不可委派；可用 agent_code："
                                + ", ".join(available_codes)
                            ),
                        }
                    )
                    continue
                group = grouped.get(profile.code)
                if group is None:
                    group = {
                        "profile": profile,
                        "requested_codes": [],
                        "source_task_indexes": [],
                        "items": [],
                        "instruction_signatures": set(),
                        "required": False,
                    }
                    grouped[profile.code] = group
                group["requested_codes"].append(profile_code)
                group["source_task_indexes"].append(index)
                group["required"] = bool(group["required"] or required)
                signature = " ".join(instruction.split())
                if signature not in group["instruction_signatures"]:
                    group["instruction_signatures"].add(signature)
                    group["items"].append(
                        {
                            "instruction": instruction,
                            "expected_output": expected_output,
                            "required": required,
                            "source_task_index": index,
                        }
                    )

            normalized: list[dict[str, Any]] = []
            for group in grouped.values():
                instruction, expected_output = _merge_agent_work_items(group["items"])
                if len(instruction) + len(expected_output or "") > MAX_AGENT_WORK_PACKAGE_CHARS:
                    rejected_tasks.append(
                        {
                            "member_index": group["source_task_indexes"][0],
                            "requested_agent_code": group["requested_codes"][0],
                            "status": "rejected",
                            "required": bool(group["required"]),
                            "error": (
                                f"数字人 {group['profile'].code} 的合并工作包超过 "
                                f"{MAX_AGENT_WORK_PACKAGE_CHARS} 字符，请精简要求后重试。"
                            ),
                        }
                    )
                    continue
                normalized.append(
                    {
                        **group,
                        "instruction": instruction,
                        "expected_output": expected_output,
                    }
                )

            if not normalized:
                details = "; ".join(item["error"] for item in rejected_tasks)
                raise ValueError(f"没有可执行的子任务。{details}")
            if len(normalized) > max_members:
                raise ValueError(f"一次最多可委派 {max_members} 个不同数字人。")

            valid_task_count = sum(len(group["source_task_indexes"]) for group in normalized)
            dispatch_stats = {
                "requested_task_count": len(tasks),
                "accepted_task_count": valid_task_count,
                "agent_count": len(normalized),
                "merged_task_count": max(0, valid_task_count - len(normalized)),
                "rejected_task_count": len(rejected_tasks),
            }

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
                result_json=json.dumps(
                    {"rejected_tasks": rejected_tasks, "dispatch_stats": dispatch_stats},
                    ensure_ascii=False,
                ),
                deadline_at=deadline_at,
            )
            db.add(team)
            leader.team_id = team_id

            for index, work_package in enumerate(normalized, start=1):
                profile = work_package["profile"]
                instruction = work_package["instruction"]
                expected_output = work_package["expected_output"]
                required = bool(work_package["required"])
                requested_codes = list(dict.fromkeys(work_package["requested_codes"]))
                child_run_id = uuid.uuid4().hex
                external_id = f"{team_id}:{index}"
                instruction_summary = " ".join(instruction.split())
                child_session = AiChatSession(
                    user_id=leader.user_id,
                    digital_human_id=profile.id,
                    model_config_id=leader_request.get("model_config_id"),
                    title=f"子任务 #{index} · {profile.name} · {instruction_summary}"[:160],
                    status="active",
                    chat_mode="research",
                    source_type="delegation",
                    external_conversation_id=external_id[:160],
                    source_label="Agent Team 子任务",
                )
                db.add(child_session)
                db.flush()

                child_question = self._child_question(
                    objective=objective,
                    instruction=instruction,
                    expected_output=expected_output,
                    leader_question=str(leader_request.get("question") or ""),
                    member_index=index,
                    agent_name=profile.name,
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
                transaction_agent = profile.code in TRANSACTION_DELEGATION_CODES
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
                    "can_write_positions": profile.code == "position-entry-agent",
                    "default_account_id": leader_request.get("default_account_id"),
                    "allow_code_execution": False,
                    "approval_policy": str(leader_request.get("approval_policy") or "full"),
                    "delegation_read_only": not transaction_agent,
                    "source_type": "delegation",
                    "source_ref": team_id,
                    "parent_run_id": leader.run_id,
                    "root_run_id": root_run_id,
                    "team_id": team_id,
                    "orchestration_depth": 1,
                    "delegated_work_items": work_package["items"],
                    "delegated_source_task_indexes": work_package["source_task_indexes"],
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
                    idempotency_key=f"team:{team_id}:agent:{profile.code}"[:120],
                    request_json=json.dumps(child_request, ensure_ascii=False, default=str),
                    attempts=1,
                    max_attempts=max(1, settings.agent_run_max_attempts),
                )
                db.add(child_run)
                # These mappers have no ORM relationship, so make the FK parent
                # visible before inserting the delegation that references it.
                db.flush()
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
                        "requested_agent_code": requested_codes[0],
                        "requested_agent_codes": requested_codes,
                        "agent_code_normalized": any(code != profile.code for code in requested_codes),
                        "agent_name": profile.name,
                        "work_item_count": len(work_package["items"]),
                        "source_task_indexes": work_package["source_task_indexes"],
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
            "rejected_tasks": rejected_tasks,
            "dispatch_stats": dispatch_stats,
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
                "rejected_tasks": result.get("rejected_tasks", []),
                "dispatch_stats": result.get("dispatch_stats", {}),
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

        preflight = _loads(team.result_json, {})
        rejected_tasks = list(preflight.get("rejected_tasks") or [])
        dispatch_stats = dict(preflight.get("dispatch_stats") or {})
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
        failed = len(delegations) - completed + len(rejected_tasks)
        total = len(delegations) + len(rejected_tasks)
        if completed == total:
            team.status = "completed"
        elif completed:
            team.status = "partial"
        else:
            team.status = "failed"
        team.finished_at = now
        team.error = None if failed == 0 else f"{failed} 个子任务未成功完成。"
        summary = f"{completed}/{total} 个子任务完成"
        team.result_json = json.dumps(
            {
                "team_id": team.team_id,
                "status": team.status,
                "summary": summary,
                "members": members,
                "rejected_tasks": rejected_tasks,
                "dispatch_stats": dispatch_stats,
            },
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
                "total": total,
                "summary": summary,
            },
        }

    def _team_dict(self, db: Session, team: AgentTeamRun) -> dict[str, Any]:
        stored_result = _loads(team.result_json, {})
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
            "rejected_tasks": stored_result.get("rejected_tasks", []),
            "dispatch_stats": stored_result.get("dispatch_stats", {}),
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
        member_index: int,
        agent_name: str,
    ) -> str:
        parts = [
            f"# Agent Team 子任务 #{member_index}",
            f"**执行数字人：{agent_name}**",
            "这是总调度派发的独立子任务。请只完成本任务，不要继续委派。",
            f"## 总任务\n{objective}",
            f"## 派发提示词\n{instruction}",
        ]
        if expected_output:
            parts.append(f"## 交付要求\n{expected_output}")
        if leader_question and leader_question != objective:
            parts.append(f"## 用户原始问题\n{leader_question}")
        parts.append("请输出可直接交给总调度统稿的结论、证据、风险和未解决问题。")
        return "\n\n".join(parts)


agent_team_service = AgentTeamService()
