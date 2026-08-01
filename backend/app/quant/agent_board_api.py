"""Agent 任务看板 API

看板不创建新的任务存储，只读取和展示 AgentRun、AgentTeamRun、AgentDelegation、
ScheduledJob、JobRun 的关联数据。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select, func, desc, asc
from sqlalchemy.orm import joinedload

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    AgentRun,
    AgentDelegation,
    AgentTeamRun,
    DigitalHumanProfile,
    JobRun,
    ScheduledJob,
)

router = APIRouter(prefix="/api/agent-board", tags=["agent-board"])


def _get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# 看板状态分类
BOARD_COLUMNS = {
    "queued": ["queued", "waiting_dependency"],
    "running": ["running"],
    "approval": ["waiting_approval"],
    "completed": ["completed"],
    "abnormal": ["failed", "timed_out", "blocked"],
}


def _status_to_column(status: str) -> str:
    """把数据库状态映射到看板列"""
    for col, statuses in BOARD_COLUMNS.items():
        if status in statuses:
            return col
    return "queued"


def _get_status_counts(session, user_id: int | None = None) -> dict[str, int]:
    """查询各状态数量"""
    query = select(AgentRun.status, func.count(AgentRun.run_id)).where(
        AgentRun.board_visible == True,
    )
    if user_id is not None:
        query = query.where(AgentRun.user_id == user_id)
    query = query.group_by(AgentRun.status)
    rows = session.execute(query).all()

    counts = {"queued": 0, "running": 0, "approval": 0, "completed": 0, "abnormal": 0}
    for status, count in rows:
        col = _status_to_column(status)
        counts[col] = counts.get(col, 0) + count
    return counts


def _query_tasks(
    session,
    *,
    user_id: int | None = None,
    status: str | None = None,
    agent_code: str | None = None,
    source_type: str | None = None,
    scheduled_job_key: str | None = None,
    page: int = 1,
    page_size: int = 30,
) -> tuple[list[dict], int]:
    """分页查询看板任务"""
    query = (
        select(AgentRun)
        .outerjoin(DigitalHumanProfile, AgentRun.assignee_profile_id == DigitalHumanProfile.id)
        .where(AgentRun.board_visible == True)
    )
    if user_id is not None:
        query = query.where(AgentRun.user_id == user_id)

    # 状态筛选：把看板列名展开为具体状态
    if status:
        statuses = BOARD_COLUMNS.get(status, [status])
        query = query.where(AgentRun.status.in_(statuses))

    if agent_code:
        query = query.where(DigitalHumanProfile.code == agent_code)

    if source_type:
        query = query.where(AgentRun.source_type == source_type)

    if scheduled_job_key:
        query = query.where(AgentRun.source_ref == scheduled_job_key)

    # 总数
    count_query = select(func.count()).select_from(query.subquery())
    total = session.execute(count_query).scalar() or 0

    # 排序：优先级 DESC，queued_at ASC，run_id ASC
    query = query.order_by(
        desc(AgentRun.priority),
        asc(AgentRun.queued_at),
        asc(AgentRun.run_id),
    ).offset((page - 1) * page_size).limit(page_size)

    rows = session.execute(query).scalars().all()

    items = []
    for run in rows:
        agent_name = None
        agent_code = None
        agent_avatar = None
        if run.assignee_profile_id:
            profile = session.get(DigitalHumanProfile, run.assignee_profile_id)
            if profile:
                agent_name = profile.name
                agent_code = profile.code
                agent_avatar = profile.avatar_url

        # 获取关联信息
        job_run_info = None
        scheduled_job_info = None
        team_info = None

        if run.source_ref:
            jr = session.execute(
                select(JobRun).where(JobRun.run_id == run.source_ref)
            ).scalar()
            if jr:
                job_run_info = {"job_key": jr.job_key, "status": jr.status}
                sj = session.execute(
                    select(ScheduledJob).where(ScheduledJob.job_key == jr.job_key)
                ).scalar()
                if sj:
                    scheduled_job_info = {"name": sj.name, "job_key": sj.job_key}

        if run.team_id:
            team = session.execute(
                select(AgentTeamRun).where(AgentTeamRun.team_id == run.team_id)
            ).scalar()
            if team:
                team_info = {"team_id": team.team_id, "objective": team.objective[:100] if team.objective else ""}

        # 计算等待/运行时间
        wait_seconds = None
        run_seconds = None
        if run.status in ("queued", "waiting_dependency") and run.queued_at:
            wait_seconds = int((datetime.now() - run.queued_at).total_seconds())
        elif run.status == "running" and run.started_at:
            run_seconds = int((datetime.now() - run.started_at).total_seconds())

        # 当前步骤
        current_step = None
        recent_tool = None

        items.append({
            "run_id": run.run_id,
            "task_title": run.task_title or f"Agent 任务 {run.run_id[:8]}",
            "status": run.status,
            "board_column": _status_to_column(run.status),
            "priority": run.priority,
            "agent_name": agent_name,
            "agent_code": agent_code,
            "agent_avatar": agent_avatar,
            "wait_seconds": wait_seconds,
            "run_seconds": run_seconds,
            "attempts": run.attempts,
            "max_attempts": run.max_attempts,
            "error": run.error[:200] if run.error else None,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "job_run": job_run_info,
            "scheduled_job": scheduled_job_info,
            "team": team_info,
            "current_step": current_step,
            "recent_tool": recent_tool,
        })

    return items, total


@router.get("/summary")
def get_board_summary(
    context: AuthContext = Depends(require_permission("agent:use")),
    session=Depends(_get_session),
) -> dict:
    """查询看板各状态数量"""
    counts = _get_status_counts(session, user_id=context.user.id)
    return {"counts": counts}


@router.get("/tasks")
def get_board_tasks(
    status: Optional[str] = Query(None, description="看板列：queued/running/approval/completed/abnormal"),
    agent_code: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None),
    scheduled_job_key: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    context: AuthContext = Depends(require_permission("agent:use")),
    session=Depends(_get_session),
) -> dict:
    """分页查询看板任务"""
    items, total = _query_tasks(
        session,
        user_id=context.user.id,
        status=status,
        agent_code=agent_code,
        source_type=source_type,
        scheduled_job_key=scheduled_job_key,
        page=page,
        page_size=page_size,
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/tasks/{run_id}")
def get_task_detail(
    run_id: str,
    context: AuthContext = Depends(require_permission("agent:use")),
    session=Depends(_get_session),
) -> dict:
    """查询任务详情"""
    run = session.get(AgentRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"任务不存在：{run_id}")

    # 权限检查
    if run.user_id != context.user.id and not context.user.is_super_admin:
        raise HTTPException(status_code=403, detail="无权查看此任务")

    agent_name = None
    agent_code = None
    if run.assignee_profile_id:
        profile = session.get(DigitalHumanProfile, run.assignee_profile_id)
        if profile:
            agent_name = profile.name
            agent_code = profile.code

    # 获取委派信息
    delegation = session.execute(
        select(AgentDelegation).where(AgentDelegation.child_run_id == run_id)
    ).scalar()
    delegation_info = None
    if delegation:
        delegation_info = {
            "delegation_id": delegation.delegation_id,
            "title": delegation.title,
            "action_key": delegation.action_key,
            "instruction": delegation.instruction,
            "expected_output": delegation.expected_output,
            "required": delegation.required,
            "extraction_source": delegation.extraction_source,
        }

    # 获取团队信息
    team_info = None
    if run.team_id:
        team = session.execute(
            select(AgentTeamRun).where(AgentTeamRun.team_id == run.team_id)
        ).scalar()
        if team:
            team_info = {
                "team_id": team.team_id,
                "leader_run_id": team.leader_run_id,
                "objective": team.objective,
                "status": team.status,
                "execution_mode": team.execution_mode,
                "completion_policy": team.completion_policy,
            }

    # 获取定时任务来源
    job_info = None
    if run.source_ref:
        jr = session.execute(select(JobRun).where(JobRun.run_id == run.source_ref)).scalar()
        if jr:
            job_info = {
                "job_run_id": jr.run_id,
                "job_key": jr.job_key,
                "status": jr.status,
                "started_at": jr.started_at.isoformat() if jr.started_at else None,
            }
            sj = session.execute(select(ScheduledJob).where(ScheduledJob.job_key == jr.job_key)).scalar()
            if sj:
                job_info["scheduled_job_name"] = sj.name

    return {
        "run_id": run.run_id,
        "task_title": run.task_title,
        "status": run.status,
        "priority": run.priority,
        "agent_name": agent_name,
        "agent_code": agent_code,
        "source_type": run.source_type,
        "source_ref": run.source_ref,
        "parent_run_id": run.parent_run_id,
        "team_id": run.team_id,
        "attempts": run.attempts,
        "max_attempts": run.max_attempts,
        "error": run.error,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "queued_at": run.queued_at.isoformat() if run.queued_at else None,
        "delegation": delegation_info,
        "team": team_info,
        "job": job_info,
    }


@router.get("/tasks/{run_id}/events")
def get_task_events(
    run_id: str,
    limit: int = Query(50, ge=1, le=200),
    context: AuthContext = Depends(require_permission("agent:use")),
    session=Depends(_get_session),
) -> dict:
    """查询任务执行事件"""
    run = session.get(AgentRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"任务不存在：{run_id}")

    from backend.app.core.models import AgentRunEvent
    events = session.execute(
        select(AgentRunEvent)
        .where(AgentRunEvent.run_id == run_id)
        .order_by(AgentRunEvent.created_at.desc())
        .limit(limit)
    ).scalars().all()

    return {
        "events": [
            {
                "event_id": e.id,
                "seq": e.seq,
                "event_type": e.event_type,
                "payload_json": e.payload_json,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
    }


@router.post("/tasks/{run_id}/retry")
def retry_task(
    run_id: str,
    context: AuthContext = Depends(require_permission("agent:manage")),
    session=Depends(_get_session),
) -> dict:
    """重试失败任务"""
    run = session.get(AgentRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"任务不存在：{run_id}")

    if run.status not in ("failed", "timed_out", "blocked"):
        raise HTTPException(status_code=400, detail=f"只有失败/超时/阻塞的任务可以重试，当前状态：{run.status}")

    if run.attempts >= run.max_attempts:
        raise HTTPException(status_code=400, detail=f"已达最大重试次数 {run.max_attempts}")

    run.status = "queued"
    run.attempts = run.attempts + 1
    run.error = None
    run.cancel_requested = False
    run.queued_at = datetime.now()
    run.started_at = None
    run.finished_at = None
    session.commit()

    return {"status": "queued", "attempts": run.attempts, "message": "任务已重新排队"}


@router.post("/tasks/{run_id}/cancel")
def cancel_task(
    run_id: str,
    context: AuthContext = Depends(require_permission("agent:manage")),
    session=Depends(_get_session),
) -> dict:
    """取消任务"""
    run = session.get(AgentRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"任务不存在：{run_id}")

    if run.status in ("completed", "cancelled"):
        raise HTTPException(status_code=400, detail=f"任务已完成或已取消，无法再取消")

    run.cancel_requested = True
    if run.status in ("queued", "waiting_dependency", "waiting_approval"):
        run.status = "cancelled"
        run.finished_at = datetime.now()
    session.commit()

    return {"status": "cancelled" if run.status == "cancelled" else "cancelling", "message": "取消请求已提交"}


@router.post("/tasks/{run_id}/priority")
def update_task_priority(
    run_id: str,
    priority: int = Query(..., ge=1, le=999),
    context: AuthContext = Depends(require_permission("agent:manage")),
    session=Depends(_get_session),
) -> dict:
    """调整任务优先级"""
    run = session.get(AgentRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"任务不存在：{run_id}")

    old_priority = run.priority
    run.priority = priority
    session.commit()

    return {"priority": priority, "old_priority": old_priority, "message": f"优先级已更新：{old_priority} -> {priority}"}


@router.get("/batches/{batch_id}")
def get_batch_detail(
    batch_id: str,
    context: AuthContext = Depends(require_permission("agent:use")),
    session=Depends(_get_session),
) -> dict:
    """查询 Team 或 Workflow 批次"""
    team = session.get(AgentTeamRun, batch_id)
    if not team:
        raise HTTPException(status_code=404, detail=f"批次不存在：{batch_id}")

    delegations = session.execute(
        select(AgentDelegation).where(AgentDelegation.team_id == batch_id)
    ).scalars().all()

    children = []
    for d in delegations:
        child_run = session.get(AgentRun, d.child_run_id)
        agent_name = None
        if d.target_profile_id:
            profile = session.get(DigitalHumanProfile, d.target_profile_id)
            if profile:
                agent_name = profile.name

        children.append({
            "delegation_id": d.delegation_id,
            "title": d.title,
            "action_key": d.action_key,
            "agent_name": agent_name,
            "agent_code": d.target_profile_code,
            "status": child_run.status if child_run else d.status,
            "priority": d.priority,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        })

    return {
        "team_id": team.team_id,
        "leader_run_id": team.leader_run_id,
        "objective": team.objective,
        "status": team.status,
        "execution_mode": team.execution_mode,
        "completion_policy": team.completion_policy,
        "deadline_at": team.deadline_at.isoformat() if team.deadline_at else None,
        "finished_at": team.finished_at.isoformat() if team.finished_at else None,
        "children": children,
    }


@router.get("/agents")
def get_board_agents(
    context: AuthContext = Depends(require_permission("agent:use")),
    session=Depends(_get_session),
) -> dict:
    """查询看板涉及的数字人列表"""
    subquery = (
        select(AgentRun.assignee_profile_id)
        .where(AgentRun.board_visible == True, AgentRun.assignee_profile_id != None)
        .distinct()
    ).subquery()

    profiles = session.execute(
        select(DigitalHumanProfile).where(DigitalHumanProfile.id.in_(select(subquery)))
    ).scalars().all()

    return {
        "agents": [
            {
                "id": p.id,
                "code": p.code,
                "name": p.name,
                "avatar_url": p.avatar_url,
            }
            for p in profiles
        ]
    }
