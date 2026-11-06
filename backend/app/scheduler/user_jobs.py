"""给用户开通一套定时任务：从系统通道主人的任务复制提示词，任务归这位用户，数据和推送都按他自己的。

默认只开早盘、午盘、晚间计划和持仓消息。盘中短提醒和全自动选股要的算力多，需要的话再单独建。
数据同步、新闻入库、影子结算这些系统任务全局只跑一份，不复制。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.models import ScheduledJob
from backend.app.feishu.feishu import SCHEDULE_CHANNEL_KEY, system_channel_owner
from backend.app.plans.rules import plan_source_for_job
from backend.app.plans.service import open_holdings

REPORT_SOURCES = ("premarket", "midday", "evening")
MESSAGE_WATCH_KEY = "message-watch-15m"
MESSAGE_WATCH = "message-watch"
_COPIED_FIELDS = (
    "name",
    "job_kind",
    "schedule_type",
    "run_time",
    "weekday_mask",
    "interval_minutes",
    "window_start",
    "window_end",
    "timezone",
    "trading_day_only",
    "digital_human_code",
    "agent_name",
    "allow_code_execution",
    "approval_policy",
    "sort_order",
    "delegation_mode",
    "task_extraction_mode",
    "completion_policy",
    "child_timeout_seconds",
    "max_delegated_tasks",
)


def job_key_for(user_id: int, kind: str) -> str:
    return f"user-{user_id}-{kind}"


def _templates(db: Session) -> dict[str, ScheduledJob]:
    """系统通道主人的任务当模板：每种计划取启用的、排最前的一个。"""
    owner = system_channel_owner(db)
    jobs = db.scalars(
        select(ScheduledJob)
        .where(ScheduledJob.owner_user_id == owner, ScheduledJob.job_kind.in_(("agent_report", "agent_task")), ScheduledJob.schedule_type == "daily")
        .order_by(ScheduledJob.enabled.desc(), ScheduledJob.sort_order, ScheduledJob.id)
    ).all()
    found: dict[str, ScheduledJob] = {}
    for job in jobs:
        source = plan_source_for_job(job.job_key, job.name)
        if source in REPORT_SOURCES and source not in found:
            found[source] = job
    watch = db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == MESSAGE_WATCH_KEY).limit(1))
    if watch is not None:
        found[MESSAGE_WATCH] = watch
    return found


def _personal_prompt(prompt: str | None) -> str | None:
    """模板是按管理员的账户数写的，别人的账户数不一定一样。"""
    return prompt.replace("三个账户", "每个账户") if prompt else prompt


def default_jobs_status(db: Session, user_id: int) -> dict[str, Any]:
    if user_id == system_channel_owner(db):
        return {"available": False, "jobs": []}
    kinds = (*REPORT_SOURCES, MESSAGE_WATCH)
    existing = {
        row.job_key: row
        for row in db.scalars(select(ScheduledJob).where(ScheduledJob.job_key.in_([job_key_for(user_id, kind) for kind in kinds]))).all()
    }
    templates = _templates(db)
    jobs = []
    for kind in kinds:
        row = existing.get(job_key_for(user_id, kind)) or templates.get(kind)
        if row is None:
            continue
        opened = job_key_for(user_id, kind) in existing
        jobs.append(
            {
                "kind": kind,
                "name": row.name,
                "run_time": row.run_time,
                "interval_minutes": row.interval_minutes,
                "opened": opened,
                "enabled": bool(opened and row.enabled),
                "last_status": row.last_status if opened else None,
            }
        )
    return {"available": True, "jobs": jobs, "has_holdings": bool(open_holdings(db, user_id))}


def open_default_jobs(db: Session, user_id: int) -> list[str]:
    """开通默认任务，已有的不动（用户可能改过）。返回新建的任务名。"""
    if user_id == system_channel_owner(db):
        raise ValueError("你已经有这些任务了")
    if not open_holdings(db, user_id):
        raise ValueError("先录入持仓再开通：没有持仓时，这些任务跑出来也没有内容")
    created: list[str] = []
    for kind, template in _templates(db).items():
        key = job_key_for(user_id, kind)
        if db.scalar(select(ScheduledJob.id).where(ScheduledJob.job_key == key).limit(1)) is not None:
            continue
        job = ScheduledJob(
            job_key=key,
            owner_user_id=user_id,
            description=template.description,
            prompt_template=_personal_prompt(template.prompt_template),
            push_channel_key=SCHEDULE_CHANNEL_KEY,  # 非空只表示要推送，实际通道按所有者定
            created_via="default",
            enabled=True,
            **{field: getattr(template, field) for field in _COPIED_FIELDS},
        )
        db.add(job)
        created.append(template.name)
    db.commit()
    return created
