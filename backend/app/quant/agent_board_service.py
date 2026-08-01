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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    AgentDelegation,
    AgentRun,
    AgentTeamRun,
    DigitalHumanProfile,
    JobRun,
)

logger = logging.getLogger(__name__)


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
    _from_legacy_report: bool = False,
) -> dict[str, Any]:
    """创建任务批次（原子事务）

    1. 锁定当前总调度 AgentRun
    2. 校验数字人存在且启用
    3. 校验 action_key 唯一性
    4. 创建 AgentTeamRun
    5. 创建 AgentDelegation + 子 AgentRun
    6. 更新父运行状态为 waiting_children（legacy_report 模式跳过此步）
    7. 提交后发送 Redis 唤醒消息（外部调用方负责）

    _from_legacy_report=True 时：
    - 跳过 leader status=="running" 校验（调度器调用时 leader 已完成）
    - 不更新 leader 状态为 waiting_children（fire-and-forget 语义）

    Returns:
        {"team_id": ..., "created_count": N, "task_ids": [...]}
    """
    if not batch.tasks:
        raise ValueError("任务批次不能为空")

    session = SessionLocal()
    try:
        # 校验 leader run 并获取 session_id（子任务需要继承）
        leader_session_id: int | None = None
        if leader_run_id:
            leader = session.get(AgentRun, leader_run_id)
            if not leader:
                raise ValueError(f"父运行不存在：{leader_run_id}")
            # legacy_report 模式：调度器调用时 leader 已完成，跳过状态校验
            if not _from_legacy_report and leader.status != "running":
                raise ValueError(f"父运行状态不是 running：{leader.status}")
            leader_session_id = leader.session_id
        elif not _from_legacy_report:
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
        intra_dups = [k for k in batch_keys if batch_keys.count(k) > 1]
        if intra_dups:
            raise ValueError(f"任务 action_key 批次内重复：{', '.join(set(intra_dups))}")

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
        deadline = datetime.now() + timedelta(seconds=batch.child_timeout_seconds * len(batch.tasks))
        team_run = AgentTeamRun(
            team_id=team_id,
            leader_run_id=leader_run_id,
            root_run_id=leader_run_id,
            user_id=user_id,
            tool_call_id=f"batch_{team_id}",
            objective=batch.objective,
            status="running",
            max_members=len(batch.tasks),
            deadline_at=deadline,
            execution_mode=batch.execution_mode,
            completion_policy=batch.completion_policy,
        )
        session.add(team_run)

        # 创建 delegations 和子 AgentRun
        created_ids: list[str] = []
        for i, task in enumerate(batch.tasks):
            profile = profile_map.get(task.agent_code)
            if not profile:
                logger.warning("数字人不存在或不可用: %s，跳过任务: %s", task.agent_code, task.action_key)
                continue

            child_run_id = uuid.uuid4().hex
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
            session.add(delegation)

            child_run = AgentRun(
                run_id=child_run_id,
                session_id=leader_session_id,
                user_id=user_id,
                engine="task_board",
                chat_mode="stock",
                source_type="task_batch",
                source_ref=job_run_id,
                parent_run_id=leader_run_id,
                root_run_id=leader_run_id,
                team_id=team_id,
                orchestration_depth=1,
                deadline_at=deadline,
                status="queued",
                priority=task.priority,
                request_json=json.dumps(
                    {"action_key": task.action_key, "title": task.title, "instruction": task.instruction},
                    ensure_ascii=False,
                ),
                board_visible=True,
                queued_at=datetime.now(),
                assignee_profile_id=profile.id,
                task_title=task.title,
            )
            session.add(child_run)
            created_ids.append(child_run_id)

        # 更新父运行状态（legacy_report 模式跳过：leader 已完成，fire-and-forget）
        if leader_run_id and not _from_legacy_report:
            leader = session.get(AgentRun, leader_run_id)
            if leader is not None:
                leader.status = "waiting_children"
                leader.completion_state = "waiting_children"

        session.commit()

        logger.info(
            "任务批次创建完成: team=%s, tasks=%d, leader=%s",
            team_id, len(created_ids), leader_run_id,
        )

        return {
            "team_id": team_id,
            "created_count": len(created_ids),
            "task_ids": created_ids,
        }

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def retry_failed_task(
    run_id: str,
    *,
    max_retries: int = 3,
) -> dict[str, Any]:
    """重试失败的任务

    只有 failed/timed_out/blocked 状态的任务可以重试。
    检查重试次数限制，重置状态为 queued。
    """
    session = SessionLocal()
    try:
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
        run.queued_at = datetime.now()
        run.started_at = None
        run.finished_at = None
        session.commit()

        logger.info("任务重试: run_id=%s, attempt=%d/%d", run_id, run.attempts, max_retries)

        return {
            "status": "queued",
            "attempts": run.attempts,
            "max_attempts": max_retries,
        }

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def cancel_task(
    run_id: str,
) -> dict[str, Any]:
    """取消任务"""
    session = SessionLocal()
    try:
        run = session.get(AgentRun, run_id)
        if not run:
            raise ValueError(f"任务不存在：{run_id}")

        if run.status in ("completed", "cancelled"):
            raise ValueError(f"任务已完成或已取消，无法再取消")

        run.cancel_requested = True
        if run.status in ("queued", "waiting_dependency", "waiting_approval"):
            run.status = "cancelled"
            run.finished_at = datetime.now()
        session.commit()

        logger.info("任务取消: run_id=%s, status=%s", run_id, run.status)

        return {
            "status": run.status,
            "message": "取消请求已提交",
        }

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def update_task_priority(
    run_id: str,
    priority: int,
) -> dict[str, Any]:
    """调整任务优先级"""
    session = SessionLocal()
    try:
        run = session.get(AgentRun, run_id)
        if not run:
            raise ValueError(f"任务不存在：{run_id}")

        old_priority = run.priority
        run.priority = priority
        session.commit()

        logger.info("任务优先级调整: run_id=%s, %d -> %d", run_id, old_priority, priority)

        return {
            "priority": priority,
            "old_priority": old_priority,
        }

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_task_queue_for_agent(
    agent_profile_id: int,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """获取指定数字人的待执行任务队列

    按优先级降序、queued_at升序排列。
    同一数字人串行执行，只返回第一条 running 或所有 queued。
    """
    session = SessionLocal()
    try:
        # 检查是否有正在运行的任务
        running = session.execute(
            select(AgentRun).where(
                AgentRun.assignee_profile_id == agent_profile_id,
                AgentRun.status == "running",
            ).limit(1)
        ).scalar()

        if running:
            # 有正在运行的，返回它和它的 queued 任务
            tasks = session.execute(
                select(AgentRun)
                .where(
                    AgentRun.assignee_profile_id == agent_profile_id,
                    AgentRun.status.in_(["running", "queued", "waiting_dependency"]),
                )
                .order_by(AgentRun.priority.desc(), AgentRun.queued_at.asc())
                .limit(limit)
            ).scalars().all()
        else:
            # 没有正在运行的，返回所有 queued
            tasks = session.execute(
                select(AgentRun)
                .where(
                    AgentRun.assignee_profile_id == agent_profile_id,
                    AgentRun.status.in_(["queued", "waiting_dependency"]),
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
    finally:
        session.close()
