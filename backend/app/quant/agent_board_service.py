"""Agent 任务编排服务

负责：
- 从定时任务报告中提取并创建任务批次
- 管理任务的创建、重试、取消
- 支持 serial_items（逐项执行）和 merge_by_agent（合并执行）两种模式
- 同一数字人串行，不同数字人并行
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.core.database import session_scope
from backend.app.market.digital_human_tools import is_role_profile
from backend.app.core.models import (
    AiChatMessage,
    AiChatSession,
    AgentDelegation,
    AgentRun,
    AgentTeamRun,
    DigitalHumanProfile,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.utcnow()


@dataclass
class TaskSpec:
    """单个任务规范"""
    action_key: str
    title: str
    agent_code: str
    instruction: str
    expected_output: str | None = None
    required: bool = True
    priority: int = 100
    depends_on: list[str] = field(default_factory=list)


@dataclass
class TaskBatchSpec:
    """任务批次规范"""
    objective: str
    execution_mode: str = "serial_items"  # serial_items | merge_by_agent
    completion_policy: str = "all_required"  # all_required | best_effort
    child_timeout_seconds: int = 600
    tasks: list[TaskSpec] = field(default_factory=list)


def create_task_batch(
    batch: TaskBatchSpec,
    *,
    leader_run_id: str | None = None,
    user_id: int | None = None,
    job_run_id: str | None = None,
    tool_call_id: str | None = None,
    skip_leader_validation: bool = False,
) -> dict[str, Any]:
    """创建任务批次（原子事务）

    1. 校验 leader run（skip_leader_validation=True 时跳过）
    2. 校验数字人存在且启用
    3. 校验 action_key 唯一性
    4. 创建 AgentTeamRun
    5. 创建 AgentDelegation + 子 AgentRun
    6. 提交后由外部调用方发送 Redis 唤醒消息

    注意：leader 状态更新为 waiting_children 由 AgentRunExecutor 负责，
    本函数不修改 leader 状态。

    skip_leader_validation=True 用于 legacy_report 模式：
    调度器调用时 leader 已完成，无法通过 running 状态校验。

    Returns:
        {"team_id": ..., "created_count": N, "task_ids": [...]}
    """
    if not batch.tasks:
        raise ValueError("任务批次不能为空")

    with session_scope() as session:
        # 校验 leader run 并获取 session_id（子任务需要继承）
        leader_request: dict[str, Any] = {}
        leader_engine = "agentscope"
        root_run_id = leader_run_id
        if leader_run_id:
            leader = session.get(AgentRun, leader_run_id)
            if not leader:
                raise ValueError(f"父运行不存在：{leader_run_id}")
            # skip_leader_validation 模式：调度器调用时 leader 已完成，跳过状态校验
            if not skip_leader_validation and leader.status != "running":
                raise ValueError(f"父运行状态不是 running：{leader.status}")
            leader_engine = leader.engine
            root_run_id = leader.root_run_id or leader.run_id
            try:
                loaded_request = json.loads(leader.request_json or "{}")
                leader_request = loaded_request if isinstance(loaded_request, dict) else {}
            except json.JSONDecodeError:
                leader_request = {}
        elif not skip_leader_validation:
            raise ValueError("创建任务批次必须提供 leader_run_id")

        if not user_id:
            raise ValueError("创建任务批次必须提供 user_id")

        # 预查所有数字人
        agent_codes = [t.agent_code for t in batch.tasks]
        profiles = session.execute(
            select(DigitalHumanProfile).where(DigitalHumanProfile.code.in_(agent_codes))
        ).scalars().all()

        profile_map: dict[str, DigitalHumanProfile] = {}
        for p in profiles:
            if p.status != "enabled":
                logger.warning("数字人未启用，跳过: %s", p.code)
                continue
            if not p.delegation_enabled:
                logger.warning("数字人未开启委派，跳过: %s", p.code)
                continue
            profile_map[p.code] = p

        # 校验批次内部 action_key 唯一性
        batch_keys = [t.action_key for t in batch.tasks]
        intra_dups = {k for k, v in Counter(batch_keys).items() if v > 1}
        if intra_dups:
            raise ValueError(f"任务 action_key 批次内重复：{', '.join(intra_dups)}")

        # 校验 action_key 与数据库已有记录的唯一性
        existing_keys = set(
            session.execute(
                select(AgentDelegation.action_key).where(
                    AgentDelegation.action_key.in_(batch_keys)
                )
            ).scalars().all()
        )
        if existing_keys:
            raise ValueError(f"任务 action_key 已存在：{', '.join(existing_keys)}")

        # 创建 TeamRun
        team_id = uuid.uuid4().hex
        deadline = _utcnow() + timedelta(seconds=batch.child_timeout_seconds * len(batch.tasks))
        team_run = AgentTeamRun(
            team_id=team_id,
            leader_run_id=leader_run_id,
            root_run_id=leader_run_id,
            user_id=user_id,
            # Batches created by an Agent tool call must keep that exact call
            # id.  The leader checkpoint resumes by tool_call_id after every
            # child finishes; replacing it with an internal batch id makes a
            # completed Team look unfinished and fails the parent run.
            tool_call_id=tool_call_id or f"batch_{team_id}",
            objective=batch.objective,
            status="running",
            max_members=len(batch.tasks),
            deadline_at=deadline,
            execution_mode=batch.execution_mode,
            completion_policy=batch.completion_policy,
        )
        session.add(team_run)
        # No ORM relationship links these rows, so flush the parent before
        # MySQL validates the Delegation foreign keys.
        session.flush()

        # 创建 delegations 和子 AgentRun
        created_ids: list[str] = []
        for i, task in enumerate(batch.tasks):
            profile = profile_map.get(task.agent_code)
            if not profile:
                logger.warning("数字人不存在或不可用: %s，跳过任务: %s", task.agent_code, task.action_key)
                continue

            child_run_id = uuid.uuid4().hex
            child_question = f"协同目标：{batch.objective}\n当前任务：{task.instruction}"
            if task.expected_output:
                child_question += f"\n期望输出：{task.expected_output}"
            child_session = AiChatSession(
                user_id=user_id,
                digital_human_id=profile.id,
                model_config_id=leader_request.get("model_config_id"),
                title=f"任务 · {profile.name} · {task.title}"[:160],
                status="active",
                chat_mode="research",
                source_type="task_batch",
                external_conversation_id=f"{team_id}:{i}"[:160],
                source_label="Agent 任务看板",
            )
            session.add(child_session)
            session.flush()
            session.add(
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
                    # 研究角色的提示词由角色段落统一注入（digital_human_skill_prompt），这里只给其他数字人带上
                    *([] if is_role_profile(profile.code) else [f"系统提示：{profile.system_prompt}"]),
                    "当前任务来自 Agent 任务看板，只完成分配给你的任务，不要继续委派。",
                ]
            )
            child_request = {
                "session_id": child_session.id,
                "user_id": user_id,
                "user_label": str(leader_request.get("user_label") or "task-owner"),
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
                "delegation_read_only": profile.code
                not in {"position-entry-agent", "watchlist-entry-agent"},
                "delegation_config": {"mode": "none"},
                "source_type": "task_batch",
                "source_ref": job_run_id or team_id,
                "parent_run_id": leader_run_id,
                "root_run_id": root_run_id,
                "team_id": team_id,
                "orchestration_depth": 1,
                "action_key": task.action_key,
            }
            delegation = AgentDelegation(
                delegation_id=uuid.uuid4().hex,
                team_id=team_id,
                parent_run_id=leader_run_id,
                child_run_id=child_run_id,
                member_index=i,
                target_profile_id=profile.id,
                target_profile_code=profile.code,
                target_profile_name=profile.name,
                instruction=task.instruction,
                expected_output=task.expected_output,
                required=task.required,
                status="queued",
                deadline_at=deadline,
                title=task.title,
                action_key=task.action_key,
                priority=task.priority,
                extraction_source="tool_call",
            )
            child_run = AgentRun(
                run_id=child_run_id,
                session_id=child_session.id,
                user_id=user_id,
                engine=leader_engine,
                chat_mode="research",
                source_type="task_batch",
                source_ref=job_run_id,
                parent_run_id=leader_run_id,
                root_run_id=leader_run_id,
                team_id=team_id,
                orchestration_depth=1,
                deadline_at=deadline,
                status="queued",
                priority=task.priority,
                request_json=json.dumps(child_request, ensure_ascii=False, default=str),
                board_visible=True,
                queued_at=_utcnow(),
                assignee_profile_id=profile.id,
                task_title=task.title,
            )
            session.add(child_run)
            # Delegation has a foreign key to the child run and there is no
            # ORM relationship that lets SQLAlchemy order these inserts.
            session.flush()
            session.add(delegation)
            created_ids.append(child_run_id)

        # 注意：leader 状态更新为 waiting_children 由 AgentRunExecutor 负责，本函数不处理

        # 显式 commit 以捕获 UNIQUE 约束冲突（session_scope 退出时二次 commit 为 no-op）
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if "uq_delegation_action_key" in str(exc.orig) or "action_key" in str(exc.orig):
                raise ValueError("任务 action_key 与已有记录冲突（并发写入）") from exc
            raise

        logger.info(
            "任务批次创建完成: team=%s, tasks=%d, leader=%s",
            team_id, len(created_ids), leader_run_id,
        )

        return {
            "team_id": team_id,
            "created_count": len(created_ids),
            "task_ids": created_ids,
        }


def retry_failed_task(
    run_id: str,
    *,
    max_retries: int = 3,
) -> dict[str, Any]:
    """重试失败的任务

    只有 failed/timed_out/blocked 状态的任务可以重试。
    检查重试次数限制，重置状态为 queued。
    """
    with session_scope() as session:
        run = session.get(AgentRun, run_id)
        if not run:
            raise ValueError(f"任务不存在：{run_id}")

        if run.status not in ("failed", "timed_out", "blocked"):
            raise ValueError(f"只有失败/超时/阻塞的任务可以重试，当前状态：{run.status}")

        if run.attempts >= max_retries:
            raise ValueError(f"已达最大重试次数 {max_retries}")

        run.status = "queued"
        run.attempts += 1
        run.error = None
        run.cancel_requested = False
        run.queued_at = _utcnow()
        run.started_at = None
        run.finished_at = None

        logger.info("任务重试: run_id=%s, attempt=%d/%d", run_id, run.attempts, max_retries)

        return {
            "status": "queued",
            "attempts": run.attempts,
            "max_attempts": max_retries,
        }


def cancel_task(
    run_id: str,
) -> dict[str, Any]:
    """取消任务"""
    with session_scope() as session:
        run = session.get(AgentRun, run_id)
        if not run:
            raise ValueError(f"任务不存在：{run_id}")

        if run.status in ("completed", "cancelled"):
            raise ValueError("任务已完成或已取消，无法再取消")

        run.cancel_requested = True
        if run.status in ("queued", "waiting_dependency", "waiting_approval"):
            run.status = "cancelled"
            run.finished_at = _utcnow()

        logger.info("任务取消: run_id=%s, status=%s", run_id, run.status)

        return {
            "status": run.status,
            "message": "取消请求已提交",
        }


def update_task_priority(
    run_id: str,
    priority: int,
) -> dict[str, Any]:
    """调整任务优先级"""
    with session_scope() as session:
        run = session.get(AgentRun, run_id)
        if not run:
            raise ValueError(f"任务不存在：{run_id}")

        old_priority = run.priority
        run.priority = priority

        logger.info("任务优先级调整: run_id=%s, %d -> %d", run_id, old_priority, priority)

        return {
            "priority": priority,
            "old_priority": old_priority,
        }


def get_task_queue_for_agent(
    agent_profile_id: int,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """获取指定数字人的待执行任务队列

    按优先级降序、queued_at升序排列。
    同一数字人串行执行，有 running 任务时包含 running+queued，否则只返回 queued。
    """
    with session_scope() as session:
        # 先查是否有正在运行的任务
        has_running = session.execute(
            select(AgentRun.run_id).where(
                AgentRun.assignee_profile_id == agent_profile_id,
                AgentRun.status == "running",
            ).limit(1)
        ).scalar()

        status_filter = ["running", "queued", "waiting_dependency"] if has_running else ["queued", "waiting_dependency"]

        tasks = session.execute(
            select(AgentRun)
            .where(
                AgentRun.assignee_profile_id == agent_profile_id,
                AgentRun.status.in_(status_filter),
            )
            .order_by(AgentRun.priority.desc(), AgentRun.queued_at.asc())
            .limit(limit)
        ).scalars().all()

        return [
            {
                "run_id": t.run_id,
                "task_title": t.task_title,
                "status": t.status,
                "priority": t.priority,
                "queued_at": t.queued_at.isoformat() if t.queued_at else None,
            }
            for t in tasks
        ]
