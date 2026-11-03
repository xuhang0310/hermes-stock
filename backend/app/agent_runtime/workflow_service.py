from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.agent_runtime.repository import AgentRunRepository, agent_run_repository
from backend.app.agent_runtime.workflow_templates import BUILTIN_WORKFLOW_TEMPLATES
from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    AiChatMessage,
    AiChatSession,
    AgentRun,
    AgentTeamRun,
    AgentWorkflowNodeRun,
    AgentWorkflowRun,
    AgentWorkflowTemplate,
    AgentWorkflowTemplateVersion,
    DigitalHumanProfile,
    User,
)
from backend.app.core.settings import settings
from backend.app.market.digital_human_tools import is_role_profile


TERMINAL_NODE_STATUSES = frozenset({"completed", "failed", "blocked", "cancelled", "timed_out"})
MAX_NODE_RESULT_CHARS = 24000
MAX_UPSTREAM_CONTEXT_CHARS = 48000


def _utcnow() -> datetime:
    return datetime.utcnow()


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _canonical(definition: dict[str, Any]) -> tuple[str, str]:
    payload = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _render(template: str, variables: dict[str, Any]) -> str:
    class SafeVariables(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    return template.format_map(SafeVariables({key: str(value) for key, value in variables.items()}))


def _validate_definition(definition: dict[str, Any]) -> None:
    nodes = definition.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("工作流模板至少需要一个节点。")
    if len(nodes) > 20:
        raise ValueError("单个工作流最多支持 20 个节点。")
    keys = [str(node.get("key") or "") for node in nodes if isinstance(node, dict)]
    if len(keys) != len(nodes) or any(not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("工作流节点 key 必须存在且唯一。")
    known = set(keys)
    incoming: dict[str, set[str]] = {}
    for node in nodes:
        dependencies = {str(item) for item in node.get("depends_on") or []}
        if not dependencies.issubset(known):
            raise ValueError(f"节点 {node['key']} 存在未知依赖。")
        if node["key"] in dependencies:
            raise ValueError(f"节点 {node['key']} 不能依赖自身。")
        incoming[str(node["key"])] = dependencies
    ready = [key for key, dependencies in incoming.items() if not dependencies]
    visited: set[str] = set()
    while ready:
        key = ready.pop()
        if key in visited:
            continue
        visited.add(key)
        for target, dependencies in incoming.items():
            if target not in visited and dependencies.issubset(visited):
                ready.append(target)
    if len(visited) != len(nodes):
        raise ValueError("工作流模板存在环形依赖。")


class AgentWorkflowService:
    """Durable DAG scheduler that executes every node as a normal AgentRun."""

    def __init__(
        self,
        session_factory: Callable[[], Session] | sessionmaker[Session] = SessionLocal,
        repository: AgentRunRepository = agent_run_repository,
    ) -> None:
        self.session_factory = session_factory
        self.repository = repository

    def sync_builtin_templates(self) -> None:
        with self.session_factory() as db:
            for code, definition in BUILTIN_WORKFLOW_TEMPLATES.items():
                _validate_definition(definition)
                payload, digest = _canonical(definition)
                template = db.scalar(
                    select(AgentWorkflowTemplate).where(AgentWorkflowTemplate.code == code).limit(1)
                )
                if template is None:
                    template = AgentWorkflowTemplate(
                        template_id=uuid.uuid4().hex,
                        code=code,
                        name=str(definition["name"]),
                        description=str(definition["description"]),
                        category=str(definition.get("category") or "research"),
                        current_version=int(definition.get("version") or 1),
                        status="enabled",
                    )
                    db.add(template)
                    db.flush()
                template.name = str(definition["name"])
                template.description = str(definition["description"])
                template.category = str(definition.get("category") or "research")
                template.status = "enabled"
                existing = db.scalar(
                    select(AgentWorkflowTemplateVersion).where(
                        AgentWorkflowTemplateVersion.template_id == template.template_id,
                        AgentWorkflowTemplateVersion.content_sha256 == digest,
                    ).limit(1)
                )
                if existing is None:
                    latest = db.scalars(
                        select(AgentWorkflowTemplateVersion)
                        .where(AgentWorkflowTemplateVersion.template_id == template.template_id)
                        .order_by(AgentWorkflowTemplateVersion.version.desc())
                        .limit(1)
                    ).first()
                    version = max(int(definition.get("version") or 1), (latest.version + 1) if latest else 1)
                    existing = AgentWorkflowTemplateVersion(
                        version_id=uuid.uuid4().hex,
                        template_id=template.template_id,
                        version=version,
                        definition_json=payload,
                        content_sha256=digest,
                    )
                    db.add(existing)
                    db.flush()
                template.current_version = existing.version
            db.commit()

    def list_templates(self) -> list[dict[str, Any]]:
        self.sync_builtin_templates()
        with self.session_factory() as db:
            rows = db.scalars(
                select(AgentWorkflowTemplate)
                .where(AgentWorkflowTemplate.status == "enabled")
                .order_by(AgentWorkflowTemplate.category, AgentWorkflowTemplate.code)
            ).all()
            result = []
            for row in rows:
                version = db.scalar(
                    select(AgentWorkflowTemplateVersion).where(
                        AgentWorkflowTemplateVersion.template_id == row.template_id,
                        AgentWorkflowTemplateVersion.version == row.current_version,
                    ).limit(1)
                )
                definition = _loads(version.definition_json if version else None, {})
                result.append(
                    {
                        "code": row.code,
                        "name": row.name,
                        "description": row.description,
                        "category": row.category,
                        "version": row.current_version,
                        "variables": definition.get("variables", {}),
                        "nodes": [
                            {
                                "key": node["key"],
                                "role": node["role"],
                                "agent_code": node["agent_code"],
                                "depends_on": node.get("depends_on", []),
                                "expected_output": node.get("expected_output"),
                            }
                            for node in definition.get("nodes", [])
                        ],
                    }
                )
            return result

    def start_workflow(
        self,
        *,
        leader_run_id: str,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self.sync_builtin_templates()
        template_code = str(arguments.get("template_code") or "").strip()
        objective = str(arguments.get("objective") or "").strip()
        supplied_variables = arguments.get("variables")
        variables = dict(supplied_variables) if isinstance(supplied_variables, dict) else {}
        timeout_seconds = max(300, min(int(arguments.get("timeout_seconds") or 1800), 7200))
        deadline_at = _utcnow() + timedelta(seconds=timeout_seconds)

        with self.session_factory() as db:
            leader = db.scalar(
                select(AgentRun).where(AgentRun.run_id == leader_run_id).with_for_update()
            )
            if leader is None:
                raise ValueError("总调度 Agent Run 不存在。")
            if leader.status != "running" or leader.cancel_requested:
                raise RuntimeError("总调度任务已取消或不再运行。")
            request_payload = _loads(leader.request_json, {})
            if str(request_payload.get("digital_human_code") or "") != "stock-pm":
                raise PermissionError("只有总调度数字人可以启动 Swarm 工作流。")
            if int(leader.orchestration_depth or 0) >= 1:
                raise PermissionError("工作流子节点不能继续创建工作流。")
            existing = db.scalar(
                select(AgentWorkflowRun).where(
                    AgentWorkflowRun.leader_run_id == leader_run_id,
                    AgentWorkflowRun.tool_call_id == tool_call_id,
                ).limit(1)
            )
            if existing is not None:
                return {**self._workflow_dict(db, existing), "created": False}
            template = db.scalar(
                select(AgentWorkflowTemplate).where(
                    AgentWorkflowTemplate.code == template_code,
                    AgentWorkflowTemplate.status == "enabled",
                ).limit(1)
            )
            if template is None:
                raise ValueError(
                    f"未知工作流模板 {template_code}；可用模板："
                    + ", ".join(sorted(BUILTIN_WORKFLOW_TEMPLATES))
                )
            version = db.scalar(
                select(AgentWorkflowTemplateVersion).where(
                    AgentWorkflowTemplateVersion.template_id == template.template_id,
                    AgentWorkflowTemplateVersion.version == template.current_version,
                ).limit(1)
            )
            if version is None:
                raise RuntimeError("工作流模板当前版本不存在。")
            definition = _loads(version.definition_json, {})
            _validate_definition(definition)
            for name, spec in (definition.get("variables") or {}).items():
                if name not in variables and isinstance(spec, dict) and "default" in spec:
                    variables[name] = spec["default"]
                if isinstance(spec, dict) and spec.get("required") and not str(variables.get(name) or "").strip():
                    raise ValueError(f"工作流缺少必填变量：{name}")
            objective = objective or str(variables.get("target") or template.name)

            owner = db.get(User, leader.user_id)
            if owner is None or not owner.is_enabled:
                raise PermissionError("工作流所有者不存在或已禁用。")
            profile_codes = {str(node["agent_code"]) for node in definition["nodes"]}
            profiles = db.scalars(
                select(DigitalHumanProfile).where(
                    DigitalHumanProfile.code.in_(profile_codes),
                    DigitalHumanProfile.status == "enabled",
                    DigitalHumanProfile.delegation_enabled.is_(True),
                )
            ).all()
            profiles_by_code = {row.code: row for row in profiles}
            missing_profiles = sorted(profile_codes - set(profiles_by_code))
            if missing_profiles:
                raise ValueError("工作流所需数字人不可用：" + ", ".join(missing_profiles))

            workflow_run_id = uuid.uuid4().hex
            workflow = AgentWorkflowRun(
                workflow_run_id=workflow_run_id,
                template_id=template.template_id,
                template_version_id=version.version_id,
                template_code=template.code,
                template_version=version.version,
                leader_run_id=leader.run_id,
                root_run_id=leader.root_run_id or leader.run_id,
                user_id=leader.user_id,
                tool_call_id=tool_call_id[:160],
                objective=objective,
                variables_json=json.dumps(variables, ensure_ascii=False, default=str),
                status="running",
                result_json=json.dumps({"definition_name": template.name}, ensure_ascii=False),
                deadline_at=deadline_at,
            )
            db.add(workflow)
            leader.team_id = workflow_run_id
            # No ORM relationship is declared, so expose the workflow FK parent
            # before inserting node rows.
            db.flush()
            for index, node in enumerate(definition["nodes"], start=1):
                db.add(
                    AgentWorkflowNodeRun(
                        node_run_id=uuid.uuid4().hex,
                        workflow_run_id=workflow_run_id,
                        node_key=str(node["key"]),
                        node_index=index,
                        role_name=str(node["role"]),
                        target_profile_code=str(node["agent_code"]),
                        target_profile_id=profiles_by_code[str(node["agent_code"])].id,
                        instruction_template=str(node["instruction"]),
                        expected_output=str(node.get("expected_output") or "") or None,
                        dependencies_json=json.dumps(node.get("depends_on") or [], ensure_ascii=False),
                        status="waiting_dependency",
                        max_attempts=max(1, min(int(node.get("max_attempts") or 2), 3)),
                    )
                )
            db.flush()
            queued = self._unlock_ready_nodes_locked(
                db, workflow, definition, request_payload, profiles_by_code
            )
            db.commit()

        for item in queued:
            self._append_child_events(item, leader.engine)
        return {
            **self.workflow_for_run(workflow_run_id, user_id=leader.user_id),
            "queued_run_ids": [item["run_id"] for item in queued],
            "created": True,
        }

    def on_child_terminal(self, child_run_id: str) -> dict[str, Any] | None:
        parent_run_id: str | None = None
        parent_requeued = False
        events: list[dict[str, Any]] = []
        queued: list[dict[str, Any]] = []
        with self.session_factory() as db:
            node = db.scalar(
                select(AgentWorkflowNodeRun)
                .where(AgentWorkflowNodeRun.child_run_id == child_run_id)
                .with_for_update()
            )
            if node is None:
                return None
            child = db.get(AgentRun, child_run_id)
            if child is None or child.status not in {"completed", "failed", "cancelled"}:
                return None
            workflow = db.scalar(
                select(AgentWorkflowRun)
                .where(AgentWorkflowRun.workflow_run_id == node.workflow_run_id)
                .with_for_update()
            )
            if workflow is None:
                return None
            parent_run_id = workflow.leader_run_id
            if node.status not in TERMINAL_NODE_STATUSES:
                answer = None
                if child.result_message_id:
                    message = db.get(AiChatMessage, child.result_message_id)
                    answer = message.content if message else None
                truncated = bool(answer and len(answer) > MAX_NODE_RESULT_CHARS)
                if truncated:
                    answer = answer[:MAX_NODE_RESULT_CHARS]
                node.status = child.status
                node.error = child.error
                node.result_json = json.dumps(
                    {
                        "run_id": child.run_id,
                        "session_id": child.session_id,
                        "status": child.status,
                        "answer": answer,
                        "answer_truncated": truncated,
                        "completion_state": child.completion_state,
                        "completion": _loads(child.completion_json, {}),
                        "error": child.error,
                    },
                    ensure_ascii=False,
                    default=str,
                )
                node.finished_at = child.finished_at or _utcnow()
                events.append(
                    {
                        "type": "workflow_node_done",
                        "workflow_run_id": workflow.workflow_run_id,
                        "node_key": node.node_key,
                        "role_name": node.role_name,
                        "child_run_id": child.run_id,
                        "status": node.status,
                    }
                )

            definition = self._definition_locked(db, workflow)
            profiles_by_code = {
                row.code: row
                for row in db.scalars(
                    select(DigitalHumanProfile).where(
                        DigitalHumanProfile.code.in_(
                            {str(item["agent_code"]) for item in definition["nodes"]}
                        )
                    )
                ).all()
            }
            leader = db.get(AgentRun, workflow.leader_run_id)
            request_payload = _loads(leader.request_json if leader else None, {})
            self._block_failed_descendants_locked(db, workflow, definition)
            queued = self._unlock_ready_nodes_locked(
                db, workflow, definition, request_payload, profiles_by_code
            )
            events.extend(
                {
                    "type": "workflow_node_status",
                    "workflow_run_id": workflow.workflow_run_id,
                    "node_key": item["node_key"],
                    "role_name": item["role_name"],
                    "agent_code": item["agent_code"],
                    "child_run_id": item["run_id"],
                    "status": "queued",
                }
                for item in queued
            )
            completion = self._finalize_locked(db, workflow, allow_timeout=False)
            if completion:
                events.append(completion["event"])
                parent_requeued = completion["parent_requeued"]
            db.commit()

        for item in queued:
            self._append_child_events(item, item["engine"])
        event_seqs: list[int] = []
        for event in events:
            if parent_run_id:
                event_seqs.append(self.repository.append_event(parent_run_id, event).seq)
        return {
            "parent_run_id": parent_run_id,
            "parent_requeued": parent_requeued,
            "queued_run_ids": [item["run_id"] for item in queued],
            "events": events,
            "event_seqs": event_seqs,
        }

    def result_for_tool_call(self, leader_run_id: str, tool_call_id: str) -> dict[str, Any] | None:
        with self.session_factory() as db:
            workflow = db.scalar(
                select(AgentWorkflowRun).where(
                    AgentWorkflowRun.leader_run_id == leader_run_id,
                    AgentWorkflowRun.tool_call_id == tool_call_id,
                ).limit(1)
            )
            if workflow is None or workflow.status == "running":
                return None
            result = _loads(workflow.result_json, {})
            return {
                "workflow_run_id": workflow.workflow_run_id,
                "template_code": workflow.template_code,
                "template_version": workflow.template_version,
                "objective": workflow.objective,
                "status": workflow.status,
                "nodes": result.get("nodes", []),
                "summary": result.get("summary"),
                "final_outputs": result.get("final_outputs", []),
                "error": workflow.error,
            }

    def workflow_for_run(self, run_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
        with self.session_factory() as db:
            query = select(AgentWorkflowRun).where(
                (AgentWorkflowRun.workflow_run_id == run_id)
                | (AgentWorkflowRun.leader_run_id == run_id)
            )
            child_workflow_id = db.scalar(
                select(AgentWorkflowNodeRun.workflow_run_id).where(
                    AgentWorkflowNodeRun.child_run_id == run_id
                ).limit(1)
            )
            if child_workflow_id:
                query = select(AgentWorkflowRun).where(
                    AgentWorkflowRun.workflow_run_id == child_workflow_id
                )
            if user_id is not None:
                query = query.where(AgentWorkflowRun.user_id == user_id)
            workflow = db.scalar(query.order_by(AgentWorkflowRun.created_at.desc()).limit(1))
            return self._workflow_dict(db, workflow) if workflow else None

    def reconcile(self) -> list[str]:
        enqueue: set[str] = set()
        with self.session_factory() as db:
            terminal_children = list(
                db.scalars(
                    select(AgentWorkflowNodeRun.child_run_id)
                    .join(AgentRun, AgentRun.run_id == AgentWorkflowNodeRun.child_run_id)
                    .where(
                        AgentWorkflowNodeRun.status.not_in(TERMINAL_NODE_STATUSES),
                        AgentRun.status.in_(["completed", "failed", "cancelled"]),
                    )
                    .limit(100)
                ).all()
            )
        for child_run_id in terminal_children:
            if not child_run_id:
                continue
            result = self.on_child_terminal(child_run_id)
            if result:
                enqueue.update(str(item) for item in result.get("queued_run_ids") or [])
                if result.get("parent_requeued") and result.get("parent_run_id"):
                    enqueue.add(str(result["parent_run_id"]))

        events: list[tuple[str, dict[str, Any]]] = []
        with self.session_factory() as db:
            overdue = db.scalars(
                select(AgentWorkflowRun).where(
                    AgentWorkflowRun.status == "running",
                    AgentWorkflowRun.deadline_at <= _utcnow(),
                ).with_for_update()
            ).all()
            for workflow in overdue:
                completion = self._finalize_locked(db, workflow, allow_timeout=True)
                if completion:
                    events.append((workflow.leader_run_id, completion["event"]))
                    if completion["parent_requeued"]:
                        enqueue.add(workflow.leader_run_id)
            db.commit()
        for run_id, event in events:
            self.repository.append_event(run_id, event)
        return sorted(enqueue)

    def cancel_for_parent(self, leader_run_id: str) -> list[str]:
        children: list[str] = []
        with self.session_factory() as db:
            workflows = db.scalars(
                select(AgentWorkflowRun).where(
                    AgentWorkflowRun.leader_run_id == leader_run_id,
                    AgentWorkflowRun.status == "running",
                ).with_for_update()
            ).all()
            for workflow in workflows:
                workflow.status = "cancelled"
                workflow.finished_at = _utcnow()
                nodes = db.scalars(
                    select(AgentWorkflowNodeRun).where(
                        AgentWorkflowNodeRun.workflow_run_id == workflow.workflow_run_id
                    )
                ).all()
                for node in nodes:
                    if node.status in TERMINAL_NODE_STATUSES:
                        continue
                    node.status = "cancelled"
                    node.finished_at = _utcnow()
                    if node.child_run_id:
                        child = db.get(AgentRun, node.child_run_id)
                        if child and child.status not in {"completed", "failed", "cancelled"}:
                            child.cancel_requested = True
                            if child.status in {"queued", "waiting_approval", "waiting_children"}:
                                child.status = "cancelled"
                                child.finished_at = _utcnow()
                            children.append(child.run_id)
            db.commit()
        return children

    def retry_node(self, workflow_run_id: str, node_key: str, *, user_id: int) -> dict[str, Any]:
        queued: list[dict[str, Any]] = []
        with self.session_factory() as db:
            workflow = db.scalar(
                select(AgentWorkflowRun).where(
                    AgentWorkflowRun.workflow_run_id == workflow_run_id,
                    AgentWorkflowRun.user_id == user_id,
                ).with_for_update()
            )
            if workflow is None:
                raise KeyError(workflow_run_id)
            leader = db.scalar(
                select(AgentRun)
                .where(AgentRun.run_id == workflow.leader_run_id)
                .with_for_update()
            )
            if leader is None or leader.status in {"completed", "failed", "cancelled"}:
                raise ValueError("总调度任务已经结束，不能在原工作流中重试。")
            if leader.status == "running":
                raise ValueError("总调度正在恢复统稿，请在新的会话中重新启动工作流。")
            node = db.scalar(
                select(AgentWorkflowNodeRun).where(
                    AgentWorkflowNodeRun.workflow_run_id == workflow_run_id,
                    AgentWorkflowNodeRun.node_key == node_key,
                ).with_for_update()
            )
            if node is None:
                raise KeyError(node_key)
            if node.status not in {"failed", "blocked", "timed_out", "cancelled"}:
                raise ValueError("只有失败、阻断、超时或取消的节点可以重试。")
            if node.attempt >= node.max_attempts and node.status != "blocked":
                raise ValueError(f"节点已达到最大尝试次数 {node.max_attempts}。")
            definition = self._definition_locked(db, workflow)
            dependents: dict[str, set[str]] = {}
            for item in definition["nodes"]:
                for dependency in item.get("depends_on") or []:
                    dependents.setdefault(str(dependency), set()).add(str(item["key"]))
            reset_keys = {node.node_key}
            pending = [node.node_key]
            while pending:
                current = pending.pop()
                for dependent in dependents.get(current, set()):
                    dependent_node = db.scalar(
                        select(AgentWorkflowNodeRun).where(
                            AgentWorkflowNodeRun.workflow_run_id == workflow_run_id,
                            AgentWorkflowNodeRun.node_key == dependent,
                        ).limit(1)
                    )
                    if dependent_node and dependent_node.status == "blocked" and dependent not in reset_keys:
                        reset_keys.add(dependent)
                        pending.append(dependent)
            reset_nodes = db.scalars(
                select(AgentWorkflowNodeRun).where(
                    AgentWorkflowNodeRun.workflow_run_id == workflow_run_id,
                    AgentWorkflowNodeRun.node_key.in_(reset_keys),
                )
            ).all()
            for reset_node in reset_nodes:
                reset_node.status = "waiting_dependency"
                reset_node.child_run_id = None
                reset_node.error = None
                reset_node.result_json = None
                reset_node.finished_at = None
            workflow.status = "running"
            workflow.finished_at = None
            workflow.error = None
            if leader.status == "queued":
                leader.status = "waiting_children"
                leader.worker_id = None
                leader.heartbeat_at = _utcnow()
            profiles = {
                row.code: row
                for row in db.scalars(select(DigitalHumanProfile)).all()
            }
            queued = self._unlock_ready_nodes_locked(
                db, workflow, definition, _loads(leader.request_json if leader else None, {}), profiles
            )
            db.commit()
        for item in queued:
            self._append_child_events(item, item["engine"])
        return {
            **self.workflow_for_run(workflow_run_id, user_id=user_id),
            "queued_run_ids": [item["run_id"] for item in queued],
        }

    def _unlock_ready_nodes_locked(
        self,
        db: Session,
        workflow: AgentWorkflowRun,
        definition: dict[str, Any],
        leader_request: dict[str, Any],
        profiles_by_code: dict[str, DigitalHumanProfile],
    ) -> list[dict[str, Any]]:
        nodes = db.scalars(
            select(AgentWorkflowNodeRun)
            .where(AgentWorkflowNodeRun.workflow_run_id == workflow.workflow_run_id)
            .order_by(AgentWorkflowNodeRun.node_index)
        ).all()
        by_key = {node.node_key: node for node in nodes}
        definitions = {str(item["key"]): item for item in definition["nodes"]}
        queued: list[dict[str, Any]] = []
        for node in nodes:
            if node.status != "waiting_dependency":
                continue
            dependencies = _loads(node.dependencies_json, [])
            if not all(by_key[key].status == "completed" for key in dependencies):
                continue
            profile = profiles_by_code.get(node.target_profile_code)
            if profile is None:
                node.status = "blocked"
                node.error = f"数字人 {node.target_profile_code} 不可用。"
                node.finished_at = _utcnow()
                continue
            queued.append(
                self._create_child_locked(
                    db,
                    workflow,
                    node,
                    definitions[node.node_key],
                    profile,
                    leader_request,
                    by_key,
                )
            )
        return queued

    def _create_child_locked(
        self,
        db: Session,
        workflow: AgentWorkflowRun,
        node: AgentWorkflowNodeRun,
        node_definition: dict[str, Any],
        profile: DigitalHumanProfile,
        leader_request: dict[str, Any],
        nodes_by_key: dict[str, AgentWorkflowNodeRun],
    ) -> dict[str, Any]:
        variables = _loads(workflow.variables_json, {})
        upstream_parts: list[str] = []
        for dependency in _loads(node.dependencies_json, []):
            source = nodes_by_key[dependency]
            result = _loads(source.result_json, {})
            answer = str(result.get("answer") or result.get("error") or "无输出")
            upstream_parts.append(
                f"### 上游节点 {dependency} · {source.role_name}\n{answer[:MAX_NODE_RESULT_CHARS]}"
            )
        upstream_context = "\n\n".join(upstream_parts)
        if len(upstream_context) > MAX_UPSTREAM_CONTEXT_CHARS:
            upstream_context = upstream_context[:MAX_UPSTREAM_CONTEXT_CHARS] + "\n\n[上游内容已截断]"
        instruction = _render(str(node_definition["instruction"]), variables)
        child_question = "\n".join(
            [
                f"# Swarm 工作流子任务 · {workflow.template_code} · {node.node_key}",
                "",
                f"## 总目标\n{workflow.objective}",
                "",
                f"## 当前角色\n{node.role_name}",
                "",
                f"## 节点指令\n{instruction}",
                "",
                f"## 交付要求\n{node.expected_output or '给出结构化结论、证据、风险和缺口。'}",
                "",
                "## 上游结果",
                upstream_context or "这是根节点，无上游结果。",
                "",
                "只完成当前节点职责。关键事实必须有证据；证据不足时明确标记 incomplete。",
            ]
        )
        child_run_id = uuid.uuid4().hex
        external_id = f"{workflow.workflow_run_id}:{node.node_key}:attempt-{node.attempt + 1}"
        child_session = AiChatSession(
            user_id=workflow.user_id,
            digital_human_id=profile.id,
            model_config_id=leader_request.get("model_config_id"),
            title=f"Swarm · {node.role_name} · {workflow.objective}"[:160],
            status="active",
            chat_mode="research",
            source_type="workflow",
            external_conversation_id=external_id[:160],
            source_label="Swarm 工作流子任务",
        )
        db.add(child_session)
        db.flush()
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
                f"当前工作流角色：{node.role_name}",
                f"职责：{profile.description}",
                # 研究角色的提示词由角色段落统一注入（digital_human_skill_prompt），这里只给其他数字人带上
                *([] if is_role_profile(profile.code) else [f"系统提示：{profile.system_prompt}"]),
                "当前任务来自总调度 Swarm 工作流。只执行当前节点，不得继续委派。",
            ]
        )
        child_request = {
            "session_id": child_session.id,
            "user_id": workflow.user_id,
            "user_label": str(leader_request.get("user_label") or "workflow-owner"),
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
            "default_account_id": leader_request.get("default_account_id"),
            "allow_code_execution": bool(
                node.target_profile_code == "quant-analyst"
                and leader_request.get("allow_code_execution")
            ),
            "approval_policy": str(leader_request.get("approval_policy") or "full"),
            "delegation_read_only": True,
            "source_type": "workflow",
            "source_ref": workflow.workflow_run_id,
            "parent_run_id": workflow.leader_run_id,
            "root_run_id": workflow.root_run_id,
            "team_id": workflow.workflow_run_id,
            "workflow_run_id": workflow.workflow_run_id,
            "workflow_node_key": node.node_key,
            "orchestration_depth": 1,
        }
        leader = db.get(AgentRun, workflow.leader_run_id)
        engine = leader.engine if leader else "agentscope"
        child_run = AgentRun(
            run_id=child_run_id,
            session_id=child_session.id,
            user_id=workflow.user_id,
            engine=engine,
            chat_mode="research",
            source_type="workflow",
            source_ref=workflow.workflow_run_id,
            parent_run_id=workflow.leader_run_id,
            root_run_id=workflow.root_run_id,
            team_id=workflow.workflow_run_id,
            orchestration_depth=1,
            deadline_at=workflow.deadline_at,
            status="queued",
            priority=240,
            idempotency_key=f"workflow:{workflow.workflow_run_id}:{node.node_key}:{node.attempt + 1}"[:120],
            request_json=json.dumps(child_request, ensure_ascii=False, default=str),
            attempts=1,
            max_attempts=max(1, settings.agent_run_max_attempts),
        )
        db.add(child_run)
        db.flush()
        node.child_run_id = child_run_id
        node.target_profile_id = profile.id
        node.status = "queued"
        node.attempt += 1
        node.started_at = _utcnow()
        return {
            "run_id": child_run_id,
            "session_id": child_session.id,
            "workflow_run_id": workflow.workflow_run_id,
            "node_key": node.node_key,
            "role_name": node.role_name,
            "agent_code": profile.code,
            "agent_name": profile.name,
            "engine": engine,
        }

    def _block_failed_descendants_locked(
        self,
        db: Session,
        workflow: AgentWorkflowRun,
        definition: dict[str, Any],
    ) -> None:
        nodes = db.scalars(
            select(AgentWorkflowNodeRun).where(
                AgentWorkflowNodeRun.workflow_run_id == workflow.workflow_run_id
            )
        ).all()
        by_key = {node.node_key: node for node in nodes}
        changed = True
        while changed:
            changed = False
            for node in nodes:
                if node.status != "waiting_dependency":
                    continue
                failed = [
                    key
                    for key in _loads(node.dependencies_json, [])
                    if by_key[key].status in {"failed", "blocked", "cancelled", "timed_out"}
                ]
                if failed:
                    node.status = "blocked"
                    node.error = "上游节点未成功：" + ", ".join(failed)
                    node.finished_at = _utcnow()
                    changed = True

    def _finalize_locked(
        self,
        db: Session,
        workflow: AgentWorkflowRun,
        *,
        allow_timeout: bool,
    ) -> dict[str, Any] | None:
        if workflow.status != "running":
            return None
        nodes = db.scalars(
            select(AgentWorkflowNodeRun)
            .where(AgentWorkflowNodeRun.workflow_run_id == workflow.workflow_run_id)
            .order_by(AgentWorkflowNodeRun.node_index)
        ).all()
        unfinished = [node for node in nodes if node.status not in TERMINAL_NODE_STATUSES]
        if unfinished and not allow_timeout:
            return None
        if unfinished:
            for node in unfinished:
                node.status = "timed_out"
                node.error = "节点超过工作流截止时间。"
                node.finished_at = _utcnow()
                if node.child_run_id:
                    child = db.get(AgentRun, node.child_run_id)
                    if child and child.status not in {"completed", "failed", "cancelled"}:
                        child.cancel_requested = True
                        if child.status in {"queued", "waiting_approval", "waiting_children"}:
                            child.status = "cancelled"
                            child.error = node.error
                            child.finished_at = _utcnow()
        completed = [node for node in nodes if node.status == "completed"]
        failed = [node for node in nodes if node.status != "completed"]
        workflow.status = "completed" if not failed else ("partial" if completed else "failed")
        workflow.finished_at = _utcnow()
        downstream_keys = {
            dependency
            for node in nodes
            for dependency in _loads(node.dependencies_json, [])
        }
        final_nodes = [node for node in nodes if node.node_key not in downstream_keys]
        result_nodes = [
            {
                "node_key": node.node_key,
                "role_name": node.role_name,
                "agent_code": node.target_profile_code,
                "status": node.status,
                "attempt": node.attempt,
                "child_run_id": node.child_run_id,
                **_loads(node.result_json, {}),
                "error": node.error or _loads(node.result_json, {}).get("error"),
            }
            for node in nodes
        ]
        summary = f"{len(completed)}/{len(nodes)} 个工作流节点完成"
        workflow.result_json = json.dumps(
            {
                "summary": summary,
                "nodes": result_nodes,
                "final_outputs": [
                    {
                        "node_key": node.node_key,
                        "role_name": node.role_name,
                        "answer": _loads(node.result_json, {}).get("answer"),
                        "status": node.status,
                    }
                    for node in final_nodes
                ],
            },
            ensure_ascii=False,
            default=str,
        )
        workflow.error = None if workflow.status == "completed" else summary
        parent = db.scalar(
            select(AgentRun).where(AgentRun.run_id == workflow.leader_run_id).with_for_update()
        )
        parent_requeued = False
        if parent and parent.status == "waiting_children":
            active_team = db.scalar(
                select(AgentTeamRun.team_id).where(
                    AgentTeamRun.leader_run_id == parent.run_id,
                    AgentTeamRun.status == "running",
                ).limit(1)
            )
            active_workflow = db.scalar(
                select(AgentWorkflowRun.workflow_run_id).where(
                    AgentWorkflowRun.leader_run_id == parent.run_id,
                    AgentWorkflowRun.status == "running",
                    AgentWorkflowRun.workflow_run_id != workflow.workflow_run_id,
                ).limit(1)
            )
            if not active_team and not active_workflow:
                parent.status = "queued"
                parent.worker_id = None
                parent.heartbeat_at = _utcnow()
                parent_requeued = True
        return {
            "parent_requeued": parent_requeued,
            "event": {
                "type": "workflow_done",
                "workflow_run_id": workflow.workflow_run_id,
                "template_code": workflow.template_code,
                "status": workflow.status,
                "summary": summary,
                "completed": len(completed),
                "total": len(nodes),
            },
        }

    def _definition_locked(
        self,
        db: Session,
        workflow: AgentWorkflowRun,
    ) -> dict[str, Any]:
        version = db.get(AgentWorkflowTemplateVersion, workflow.template_version_id)
        if version is None:
            raise RuntimeError("工作流定义版本丢失。")
        return _loads(version.definition_json, {})

    def _workflow_dict(
        self,
        db: Session,
        workflow: AgentWorkflowRun,
    ) -> dict[str, Any]:
        nodes = db.scalars(
            select(AgentWorkflowNodeRun)
            .where(AgentWorkflowNodeRun.workflow_run_id == workflow.workflow_run_id)
            .order_by(AgentWorkflowNodeRun.node_index)
        ).all()
        return {
            "workflow_run_id": workflow.workflow_run_id,
            "template_code": workflow.template_code,
            "template_version": workflow.template_version,
            "objective": workflow.objective,
            "variables": _loads(workflow.variables_json, {}),
            "status": workflow.status,
            "deadline_at": workflow.deadline_at.isoformat() if workflow.deadline_at else None,
            "finished_at": workflow.finished_at.isoformat() if workflow.finished_at else None,
            "summary": _loads(workflow.result_json, {}).get("summary"),
            "nodes": [
                {
                    "node_key": node.node_key,
                    "node_index": node.node_index,
                    "role_name": node.role_name,
                    "agent_code": node.target_profile_code,
                    "depends_on": _loads(node.dependencies_json, []),
                    "child_run_id": node.child_run_id,
                    "status": node.status,
                    "attempt": node.attempt,
                    "max_attempts": node.max_attempts,
                    "result": _loads(node.result_json, {}),
                    "error": node.error,
                    "started_at": node.started_at.isoformat() if node.started_at else None,
                    "finished_at": node.finished_at.isoformat() if node.finished_at else None,
                }
                for node in nodes
            ],
            "error": workflow.error,
        }

    def _append_child_events(self, item: dict[str, Any], engine: str) -> None:
        self.repository.append_event(
            item["run_id"],
            {
                "type": "session",
                "session_id": item["session_id"],
                "chat_mode": "research",
                "source_type": "workflow",
                "workflow_run_id": item["workflow_run_id"],
                "node_key": item["node_key"],
            },
        )
        self.repository.append_event(
            item["run_id"],
            {
                "type": "queued",
                "engine": engine,
                "status": "queued",
                "workflow_run_id": item["workflow_run_id"],
                "node_key": item["node_key"],
            },
        )


agent_workflow_service = AgentWorkflowService()
