from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.database import get_db
from backend.app.feishu.feishu import (
    BUILTIN_CHANNEL_KEYS,
    SCHEDULE_CHANNEL_KEY,
    decrypt_value,
    encrypt_value,
    get_channel,
    is_cipher_invalid,
    mask_webhook,
    test_channel,
    validate_webhook_url,
)
from backend.app.core.models import AgentReport, Heartbeat, JobRun, NotifyChannel, ScheduledJob, SystemLog
from backend.app.scheduler.rhythm_scheduler import DH_TO_AGENT, HEARTBEAT_COMPONENT, dispatch_manual_run, is_trading_day, now_sh
from backend.app.core.settings import settings


router = APIRouter(prefix="/api", tags=["rhythm"])

VALID_DIGITAL_HUMAN_CODES = set(DH_TO_AGENT)


class NotifyChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    webhook_url: str | None = Field(default=None, max_length=500)
    secret: str | None = Field(default=None, max_length=200)
    status: Literal["enabled", "disabled"] = "enabled"
    remark: str | None = None


class NotifyChannelTestIn(BaseModel):
    channel_key: str | None = Field(default=None, max_length=80)
    webhook_url: str | None = Field(default=None, max_length=500)
    secret: str | None = Field(default=None, max_length=200)


def _channel_out(row: NotifyChannel) -> dict:
    webhook_url = decrypt_value(row.webhook_url_cipher)
    cipher_invalid = is_cipher_invalid(row.webhook_url_cipher)
    return {
        "channel_key": row.channel_key,
        "name": row.name,
        "channel_type": row.channel_type,
        "purpose": row.purpose,
        "webhook_masked": mask_webhook(webhook_url) if webhook_url else ("密文解密失败，请重新保存" if cipher_invalid else None),
        "has_webhook": bool(webhook_url),
        "cipher_invalid": cipher_invalid,
        "has_secret": bool(decrypt_value(row.secret_cipher)),
        "status": row.status,
        "remark": row.remark,
        "last_ok_at": row.last_ok_at.isoformat() if row.last_ok_at else None,
        "last_error": row.last_error,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/notify-channels")
def notify_channels(
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("system:view")),
) -> list[dict]:
    rows = {
        row.channel_key: row
        for row in db.scalars(select(NotifyChannel).where(NotifyChannel.channel_key.in_(BUILTIN_CHANNEL_KEYS))).all()
    }
    ordered = [rows[key] for key in BUILTIN_CHANNEL_KEYS if key in rows]
    return [_channel_out(row) for row in ordered]


@router.put("/notify-channels/{channel_key}")
def update_notify_channel(
    channel_key: str,
    payload: NotifyChannelIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    key = channel_key.strip().lower()
    if key not in BUILTIN_CHANNEL_KEYS:
        raise HTTPException(status_code=404, detail="通道不存在，当前仅支持一个飞书任务群机器人")
    row = get_channel(db, key)
    if row is None:
        raise HTTPException(status_code=404, detail="通道未初始化，请先执行数据库种子")
    row.name = payload.name
    row.status = payload.status
    row.remark = payload.remark
    if payload.webhook_url is not None and payload.webhook_url.strip():
        url_error = validate_webhook_url(payload.webhook_url)
        if url_error:
            raise HTTPException(status_code=400, detail=url_error)
        row.webhook_url_cipher = encrypt_value(payload.webhook_url)
        if not settings.webhook_enc_key:
            db.add(
                SystemLog(
                    level="WARNING",
                    module="feishu",
                    message="未配置 STOCK_WEBHOOK_ENC_KEY，Webhook 密文使用派生密钥加密；轮换 STOCK_AUTH_SECRET 或数据库密码会导致已存密文失效",
                )
            )
    if payload.secret is not None and payload.secret.strip():
        row.secret_cipher = encrypt_value(payload.secret)
    db.commit()
    db.refresh(row)
    return _channel_out(row)


@router.post("/notify-channels/test")
def test_notify_channel(
    payload: NotifyChannelTestIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    if not payload.channel_key and not (payload.webhook_url or "").strip():
        raise HTTPException(status_code=400, detail="请提供通道标识或 Webhook 地址")
    return test_channel(
        db,
        channel_key=payload.channel_key,
        webhook_url=payload.webhook_url,
        secret=payload.secret,
    )


# --------------------------------------------------------------------------- #
# 每日节律任务
# --------------------------------------------------------------------------- #
class ScheduleJobIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    schedule_type: Literal["once", "daily", "weekly", "monthly_last", "interval"]
    run_time: str | None = Field(default=None, max_length=5)
    run_once_at: datetime | None = None
    weekday_mask: str | None = Field(default=None, max_length=20)
    interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    window_start: str | None = Field(default=None, max_length=5)
    window_end: str | None = Field(default=None, max_length=5)
    trading_day_only: bool = True
    digital_human_code: str | None = Field(default=None, max_length=80)
    prompt_template: str | None = None
    push_channel_key: str | None = Field(default=None, max_length=80)
    enabled: bool = True
    sort_order: int = 0
    allow_code_execution: bool = False


class ScheduleJobCreateIn(ScheduleJobIn):
    job_key: str | None = Field(default=None, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    job_kind: Literal["agent_task", "agent_report"] = "agent_task"


def _looks_like_utf8_mojibake(value: str | None) -> bool:
    """Detect UTF-8 text that was decoded as Latin-1 before being submitted."""
    if not value:
        return False
    if "\ufffd" in value or any("\x80" <= char <= "\x9f" for char in value):
        return True
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False
    return repaired != value and any("\u3400" <= char <= "\u9fff" for char in repaired)


def _validate_schedule_text_encoding(payload: ScheduleJobIn) -> None:
    values = (payload.name, payload.description, payload.prompt_template)
    if any(_looks_like_utf8_mojibake(value) for value in values):
        raise HTTPException(
            status_code=422,
            detail="检测到任务文本编码异常，请刷新页面后重试，勿回写乱码内容",
        )


def _validate_schedule_shape(payload: ScheduleJobIn) -> None:
    if payload.schedule_type == "once":
        if payload.run_once_at is None:
            raise HTTPException(status_code=422, detail="一次任务必须提供 run_once_at")
        run_once_at = payload.run_once_at
        if run_once_at.tzinfo is not None:
            run_once_at = run_once_at.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
            payload.run_once_at = run_once_at
        if run_once_at <= now_sh():
            raise HTTPException(status_code=422, detail="一次任务执行时间必须晚于当前时间")
        return
    if payload.schedule_type in {"daily", "weekly", "monthly_last"}:
        try:
            datetime.strptime(payload.run_time or "", "%H:%M")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="执行时间必须是 HH:MM") from exc
        if payload.schedule_type == "weekly" and not payload.weekday_mask:
            raise HTTPException(status_code=422, detail="每周任务必须选择星期")
        return
    if not payload.interval_minutes:
        raise HTTPException(status_code=422, detail="间隔任务必须提供 interval_minutes")
    for field_name, value in (("window_start", payload.window_start), ("window_end", payload.window_end)):
        if value is None:
            continue
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"{field_name} 必须是 HH:MM") from exc


def _activate_agent_report_jobs(db: Session) -> list[ScheduledJob]:
    """Enable report jobs without round-tripping their human-readable fields."""
    jobs = list(
        db.scalars(
            select(ScheduledJob)
            .where(ScheduledJob.job_kind == "agent_report")
            .order_by(ScheduledJob.sort_order, ScheduledJob.id)
        ).all()
    )
    for job in jobs:
        job.enabled = True
        job.push_channel_key = SCHEDULE_CHANNEL_KEY
    db.commit()
    for job in jobs:
        db.refresh(job)
    return jobs


def _job_out(job: ScheduledJob) -> dict:
    return {
        "job_key": job.job_key,
        "name": job.name,
        "description": job.description,
        "job_kind": job.job_kind,
        "schedule_type": job.schedule_type,
        "run_time": job.run_time,
        "run_once_at": job.run_once_at.isoformat() if job.run_once_at else None,
        "timezone": job.timezone,
        "weekday_mask": job.weekday_mask,
        "interval_minutes": job.interval_minutes,
        "window_start": job.window_start,
        "window_end": job.window_end,
        "trading_day_only": job.trading_day_only,
        "digital_human_code": job.digital_human_code,
        "agent_name": job.agent_name,
        "prompt_template": job.prompt_template,
        "push_channel_key": job.push_channel_key,
        "owner_user_id": job.owner_user_id,
        "created_by_run_id": job.created_by_run_id,
        "created_via": job.created_via,
        "allow_code_execution": bool(job.allow_code_execution),
        "enabled": job.enabled,
        "sort_order": job.sort_order,
        "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
        "last_status": job.last_status,
        "last_error": job.last_error,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def _run_out(run: JobRun, *, output_content: str | None = None) -> dict:
    payload = {
        "run_id": run.run_id,
        "job_key": run.job_key,
        "trigger_type": run.trigger_type,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_ms": run.duration_ms,
        "report_id": run.report_id,
        "notification_id": run.notification_id,
        "output_preview": run.output_preview,
        "error": run.error,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }
    if output_content is not None:
        payload["output_content"] = output_content
    return payload


def _run_output_content(db: Session, run: JobRun) -> str | None:
    if run.output_content:
        return run.output_content
    if run.report_id:
        report_content = db.scalar(
            select(AgentReport.content).where(AgentReport.report_id == run.report_id).limit(1)
        )
        if report_content:
            return report_content
    return run.output_preview


@router.get("/schedule/jobs")
def schedule_jobs(
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("system:view")),
) -> list[dict]:
    rows = db.scalars(select(ScheduledJob).order_by(ScheduledJob.sort_order, ScheduledJob.id)).all()
    return [_job_out(row) for row in rows]


@router.post("/schedule/jobs")
def create_schedule_job(
    payload: ScheduleJobCreateIn,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    if payload.digital_human_code and payload.digital_human_code not in VALID_DIGITAL_HUMAN_CODES:
        raise HTTPException(status_code=400, detail="数字人 code 无效")
    if payload.push_channel_key and payload.push_channel_key not in BUILTIN_CHANNEL_KEYS:
        raise HTTPException(status_code=400, detail="定时任务只能推送到统一的飞书任务群")
    _validate_schedule_text_encoding(payload)
    _validate_schedule_shape(payload)
    job_key = payload.job_key or f"admin-{uuid4().hex[:16]}"
    if db.scalar(select(ScheduledJob.id).where(ScheduledJob.job_key == job_key).limit(1)):
        raise HTTPException(status_code=409, detail="任务标识已存在")
    job = ScheduledJob(
        job_key=job_key,
        name=payload.name,
        description=payload.description,
        job_kind=payload.job_kind,
        schedule_type=payload.schedule_type,
        run_time=payload.run_time,
        run_once_at=payload.run_once_at,
        weekday_mask=payload.weekday_mask,
        interval_minutes=payload.interval_minutes,
        window_start=payload.window_start,
        window_end=payload.window_end,
        timezone="Asia/Shanghai",
        trading_day_only=payload.trading_day_only,
        digital_human_code=payload.digital_human_code,
        agent_name=DH_TO_AGENT.get(payload.digital_human_code or ""),
        prompt_template=payload.prompt_template,
        push_channel_key=payload.push_channel_key,
        owner_user_id=context.user.id,
        created_via="admin",
        allow_code_execution=payload.allow_code_execution,
        approval_policy="full",
        enabled=payload.enabled,
        sort_order=payload.sort_order,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return _job_out(job)


@router.put("/schedule/jobs/{job_key}")
def update_schedule_job(
    job_key: str,
    payload: ScheduleJobIn,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    job = db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == job_key).limit(1))
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if payload.digital_human_code and payload.digital_human_code not in VALID_DIGITAL_HUMAN_CODES:
        raise HTTPException(status_code=400, detail="数字人 code 无效")
    if payload.push_channel_key and payload.push_channel_key not in BUILTIN_CHANNEL_KEYS:
        raise HTTPException(status_code=400, detail="定时任务只能推送到统一的飞书任务群")
    _validate_schedule_text_encoding(payload)
    _validate_schedule_shape(payload)

    job.name = payload.name
    job.description = payload.description
    job.schedule_type = payload.schedule_type
    job.run_time = payload.run_time
    job.run_once_at = payload.run_once_at
    job.weekday_mask = payload.weekday_mask
    job.interval_minutes = payload.interval_minutes
    job.window_start = payload.window_start
    job.window_end = payload.window_end
    job.trading_day_only = payload.trading_day_only
    job.digital_human_code = payload.digital_human_code
    job.agent_name = DH_TO_AGENT.get(payload.digital_human_code or "")
    job.prompt_template = payload.prompt_template
    job.push_channel_key = payload.push_channel_key
    job.enabled = payload.enabled
    job.sort_order = payload.sort_order
    job.allow_code_execution = payload.allow_code_execution
    db.commit()
    db.refresh(job)
    return _job_out(job)


@router.delete("/schedule/jobs/{job_key}")
def delete_schedule_job(
    job_key: str,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    job = db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == job_key).limit(1))
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    db.delete(job)
    db.commit()
    return {"deleted": True, "job_key": job_key}


@router.post("/schedule/jobs/agent-reports/activate")
def activate_agent_report_jobs(
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    jobs = _activate_agent_report_jobs(db)
    return {"updated": len(jobs), "jobs": [_job_out(job) for job in jobs]}


@router.post("/schedule/jobs/{job_key}/toggle")
def toggle_schedule_job(
    job_key: str,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    job = db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == job_key).limit(1))
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    job.enabled = not job.enabled
    db.commit()
    db.refresh(job)
    return _job_out(job)


@router.post("/schedule/jobs/{job_key}/run")
async def run_schedule_job(
    job_key: str,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    job = db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == job_key).limit(1))
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    run_id = await dispatch_manual_run(job_key)
    if run_id is None:
        raise HTTPException(status_code=409, detail="任务正在运行，请稍后再试")
    return {"run_id": run_id, "job_key": job_key}


@router.get("/schedule/runs")
def schedule_runs(
    job_key: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("system:view")),
) -> list[dict]:
    query = select(JobRun)
    if job_key:
        query = query.where(JobRun.job_key == job_key)
    if status:
        query = query.where(JobRun.status == status)
    rows = db.scalars(query.order_by(JobRun.created_at.desc(), JobRun.id.desc()).limit(limit)).all()
    return [_run_out(row) for row in rows]


@router.get("/schedule/runs/{run_id}")
def schedule_run_detail(
    run_id: str,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("system:view")),
) -> dict:
    run = db.scalar(select(JobRun).where(JobRun.run_id == run_id).limit(1))
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return _run_out(run, output_content=_run_output_content(db, run) or "")


@router.get("/schedule/status")
def schedule_status(
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("system:view")),
) -> dict:
    now = now_sh()
    heartbeat = db.get(Heartbeat, HEARTBEAT_COMPONENT)
    heartbeat_at = heartbeat.last_seen_at if heartbeat else None
    alive = bool(heartbeat_at and (now - heartbeat_at).total_seconds() < 90)
    today_start = datetime(now.year, now.month, now.day)
    counts = {"success": 0, "failed": 0, "running": 0}
    for status_value, total in db.execute(
        select(JobRun.status, func.count())
        .where(JobRun.created_at >= today_start)
        .group_by(JobRun.status)
    ).all():
        counts[status_value] = total
    channel = get_channel(db, SCHEDULE_CHANNEL_KEY)
    channel_configured = bool(channel and decrypt_value(channel.webhook_url_cipher))
    return {
        "scheduler_enabled": settings.scheduler_enabled,
        "scheduler_alive": alive,
        "heartbeat_at": heartbeat_at.isoformat() if heartbeat_at else None,
        "is_trading_day": is_trading_day(now.date()),
        "server_time": now.isoformat(),
        "today": counts,
        "notify_channel": {
            "name": channel.name if channel else "飞书任务群",
            "configured": channel_configured,
            "signed": bool(channel and decrypt_value(channel.secret_cipher)),
            "enabled": bool(channel and channel.status == "enabled"),
            "last_ok_at": channel.last_ok_at.isoformat() if channel and channel.last_ok_at else None,
            "last_error": channel.last_error if channel else None,
        },
    }
