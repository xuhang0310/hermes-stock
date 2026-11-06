from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.agent_runtime.profile_access import target_roles_allow
from backend.app.core.database import get_db
from backend.app.feishu.feishu import (
    BUILTIN_CHANNEL_KEYS,
    SCHEDULE_CHANNEL_KEY,
    decrypt_value,
    own_channel_key,
    encrypt_value,
    get_channel,
    is_cipher_invalid,
    mask_webhook,
    test_channel,
    validate_webhook_url,
)
from backend.app.core.models import AgentReport, DigitalHumanProfile, Heartbeat, JobRun, NotifyChannel, ScheduledJob, SystemLog
from backend.app.scheduler.rhythm_scheduler import (
    DH_TO_AGENT,
    HEARTBEAT_COMPONENT,
    POSITION_PRICE_SYNC_JOB_KEY,
    dispatch_manual_run,
    is_trading_day,
    now_sh,
)
from backend.app.core.response import fail, ok
from backend.app.scheduler.user_jobs import default_jobs_status, open_default_jobs
from backend.app.core.settings import settings


router = APIRouter(prefix="/api", tags=["rhythm"])
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

VALID_DIGITAL_HUMAN_CODES = set(DH_TO_AGENT)


def _shanghai_iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=SHANGHAI_TZ).isoformat() if value else None


def _utc_iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


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


@router.get("/my-notify-channel")
def my_notify_channel(
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    """我自己的飞书通道。只看得到自己的，别人的地址这里拿不到。"""
    row = get_channel(db, own_channel_key(db, context.user.id))
    return ok({"channel": _channel_out(row) if row else None})


@router.put("/my-notify-channel")
def save_my_notify_channel(
    payload: NotifyChannelIn,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> Any:
    key = own_channel_key(db, context.user.id)
    url = (payload.webhook_url or "").strip()
    if url:
        url_error = validate_webhook_url(url)
        if url_error:
            return JSONResponse(fail(url_error), status_code=400)
    row = get_channel(db, key)
    if row is None and not url:
        return JSONResponse(fail("第一次保存要填 Webhook 地址"), status_code=400)
    if row is None:
        row = NotifyChannel(channel_key=key, name=payload.name, purpose="schedule")
        db.add(row)
    row.name = payload.name
    row.status = payload.status
    row.remark = payload.remark
    if url:
        row.webhook_url_cipher = encrypt_value(url)
    if (payload.secret or "").strip():
        row.secret_cipher = encrypt_value(payload.secret)
    db.commit()
    db.refresh(row)
    return ok({"channel": _channel_out(row)})


class MyNotifyChannelTestIn(BaseModel):
    webhook_url: str | None = Field(default=None, max_length=500)
    secret: str | None = Field(default=None, max_length=200)


@router.post("/my-notify-channel/test")
def test_my_notify_channel(
    payload: MyNotifyChannelTestIn,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    """测试推送。通道只能是自己的，不接受别的 channel_key。"""
    key = own_channel_key(db, context.user.id)
    saved = get_channel(db, key)
    return ok(test_channel(db, channel_key=key if saved else None, webhook_url=payload.webhook_url, secret=payload.secret))


@router.get("/my-default-jobs")
def my_default_jobs(
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    return ok(default_jobs_status(db, context.user.id))


@router.post("/my-default-jobs")
def open_my_default_jobs(
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> Any:
    try:
        created = open_default_jobs(db, context.user.id)
    except ValueError as exc:
        return JSONResponse(fail(str(exc)), status_code=400)
    return ok({"created": created, **default_jobs_status(db, context.user.id)})


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


def _validate_agent_job(db: Session, payload: ScheduleJobIn, context: AuthContext) -> None:
    code = (payload.digital_human_code or "").strip()
    if not code:
        raise HTTPException(status_code=422, detail="Agent 定时任务必须选择可用的数字人")
    profile = db.scalar(
        select(DigitalHumanProfile)
        .where(DigitalHumanProfile.code == code, DigitalHumanProfile.status == "enabled")
        .limit(1)
    )
    if profile is None:
        raise HTTPException(status_code=422, detail="所选数字人不存在或已停用")
    if not target_roles_allow(profile.target_roles, context.user.role):
        raise HTTPException(status_code=403, detail="当前用户角色无权使用所选数字人")


def _activate_agent_report_jobs(db: Session, owner_user_id: int) -> list[ScheduledJob]:
    """Enable the caller's report jobs without round-tripping their human-readable fields."""
    jobs = list(
        db.scalars(
            select(ScheduledJob)
            .where(ScheduledJob.job_kind == "agent_report", ScheduledJob.owner_user_id == owner_user_id)
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
        "run_once_at": _shanghai_iso(job.run_once_at),
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
        "is_shared": job.job_key == POSITION_PRICE_SYNC_JOB_KEY,
        "created_by_run_id": job.created_by_run_id,
        "created_via": job.created_via,
        "allow_code_execution": bool(job.allow_code_execution),
        "enabled": job.enabled,
        "sort_order": job.sort_order,
        "last_run_at": _shanghai_iso(job.last_run_at),
        "last_status": job.last_status,
        "last_error": job.last_error,
        "updated_at": _utc_iso(job.updated_at),
    }


def _run_out(
    run: JobRun,
    *,
    output_content: str | None = None,
    job_name: str | None = None,
) -> dict:
    payload = {
        "run_id": run.run_id,
        "job_key": run.job_key,
        "job_name": job_name,
        "trigger_type": run.trigger_type,
        "status": run.status,
        "started_at": _shanghai_iso(run.started_at),
        "finished_at": _shanghai_iso(run.finished_at),
        "duration_ms": run.duration_ms,
        "report_id": run.report_id,
        "notification_id": run.notification_id,
        "output_preview": run.output_preview,
        "error": run.error,
        "created_at": _utc_iso(run.created_at),
    }
    if output_content is not None:
        payload["output_content"] = output_content
    return payload


def _page_out(items: list[dict], *, page: int, page_size: int, total: int, **extra: int) -> dict:
    pages = (total + page_size - 1) // page_size if total else 0
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
        **extra,
    }


def _run_output_content(db: Session, run: JobRun) -> str | None:
    if run.output_content:
        return run.output_content
    if run.report_id:
        report_content = db.scalar(
            select(AgentReport.content)
            .where(
                AgentReport.report_id == run.report_id,
                AgentReport.owner_user_id == run.owner_user_id,
            )
            .limit(1)
        )
        if report_content:
            return report_content
    return run.output_preview


def _visible_job_clause(user_id: int):
    return or_(
        ScheduledJob.owner_user_id == user_id,
        ScheduledJob.job_key == POSITION_PRICE_SYNC_JOB_KEY,
    )


def _visible_run_clause(user_id: int):
    return or_(
        JobRun.owner_user_id == user_id,
        JobRun.job_key == POSITION_PRICE_SYNC_JOB_KEY,
    )


def _owned_job(db: Session, *, user_id: int, job_key: str) -> ScheduledJob:
    job = db.scalar(
        select(ScheduledJob)
        .where(
            ScheduledJob.job_key == job_key,
            ScheduledJob.owner_user_id == user_id,
        )
        .limit(1)
    )
    if job is None:
        # Hide foreign task identifiers behind the same response as a missing
        # task. Personal schedules are not an administrator-wide namespace.
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.get("/schedule/jobs")
def schedule_jobs(
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> list[dict]:
    rows = db.scalars(
        select(ScheduledJob)
        .where(_visible_job_clause(context.user.id))
        .order_by(ScheduledJob.sort_order, ScheduledJob.id)
    ).all()
    return [_job_out(row) for row in rows]


@router.get("/schedule/jobs/paged")
def schedule_jobs_paged(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=8, ge=1, le=50),
    enabled: bool | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    filters = [_visible_job_clause(context.user.id)]
    if enabled is not None:
        filters.append(ScheduledJob.enabled.is_(enabled))
    if keyword and keyword.strip():
        filters.append(ScheduledJob.name.ilike(f"%{keyword.strip()}%"))

    query = select(ScheduledJob)
    count_query = select(func.count()).select_from(ScheduledJob)
    if filters:
        query = query.where(*filters)
        count_query = count_query.where(*filters)

    total = int(db.scalar(count_query) or 0)
    enabled_total = int(
        db.scalar(
            select(func.count()).select_from(ScheduledJob).where(
                _visible_job_clause(context.user.id),
                ScheduledJob.enabled.is_(True),
            )
        ) or 0
    )
    rows = db.scalars(
        query.order_by(ScheduledJob.sort_order, ScheduledJob.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return _page_out(
        [_job_out(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
        enabled_total=enabled_total,
    )


@router.post("/schedule/jobs")
def create_schedule_job(
    payload: ScheduleJobCreateIn,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    if payload.digital_human_code and payload.digital_human_code not in VALID_DIGITAL_HUMAN_CODES:
        raise HTTPException(status_code=400, detail="数字人 code 无效")
    if payload.push_channel_key and payload.push_channel_key not in BUILTIN_CHANNEL_KEYS:
        raise HTTPException(status_code=400, detail="定时任务只能推送到自己的飞书")
    _validate_schedule_text_encoding(payload)
    _validate_schedule_shape(payload)
    _validate_agent_job(db, payload, context)
    job_key = payload.job_key or f"user-{context.user.id}-{uuid4().hex[:12]}"
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
        created_via="user",
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
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    job = _owned_job(db, user_id=context.user.id, job_key=job_key)
    is_agent_job = job.job_kind in ("agent_task", "agent_report")
    if payload.digital_human_code and payload.digital_human_code not in VALID_DIGITAL_HUMAN_CODES:
        raise HTTPException(status_code=400, detail="数字人 code 无效")
    if payload.push_channel_key and payload.push_channel_key not in BUILTIN_CHANNEL_KEYS:
        raise HTTPException(status_code=400, detail="定时任务只能推送到自己的飞书")
    _validate_schedule_text_encoding(payload)
    _validate_schedule_shape(payload)
    if not is_agent_job:
        # 规则任务（入库、选股计算等）不经数字人，只允许改名称、说明、时间和启停
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
        job.enabled = payload.enabled
        job.sort_order = payload.sort_order
        db.commit()
        db.refresh(job)
        return _job_out(job)
    _validate_agent_job(db, payload, context)

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
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    job = _owned_job(db, user_id=context.user.id, job_key=job_key)
    running = db.scalar(
        select(JobRun.id)
        .where(
            JobRun.job_key == job_key,
            JobRun.owner_user_id == context.user.id,
            JobRun.status == "running",
        )
        .limit(1)
    )
    if running is not None:
        raise HTTPException(status_code=409, detail="任务正在运行，结束后才能删除")
    db.delete(job)
    db.commit()
    return {"deleted": True, "job_key": job_key}


@router.post("/schedule/jobs/agent-reports/activate")
def activate_agent_report_jobs(
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("model:manage")),
) -> dict:
    jobs = _activate_agent_report_jobs(db, context.user.id)
    return {"updated": len(jobs), "jobs": [_job_out(job) for job in jobs]}


@router.post("/schedule/jobs/{job_key}/toggle")
def toggle_schedule_job(
    job_key: str,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    job = _owned_job(db, user_id=context.user.id, job_key=job_key)
    job.enabled = not job.enabled
    db.commit()
    db.refresh(job)
    return _job_out(job)


@router.post("/schedule/jobs/{job_key}/run")
async def run_schedule_job(
    job_key: str,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    _owned_job(db, user_id=context.user.id, job_key=job_key)
    run_id = await dispatch_manual_run(
        job_key,
        actor_user_id=context.user.id,
    )
    if run_id is None:
        raise HTTPException(status_code=409, detail="任务正在运行，请稍后再试")
    return {"run_id": run_id, "job_key": job_key}


@router.get("/schedule/runs")
def schedule_runs(
    job_key: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> list[dict]:
    query = select(JobRun).where(_visible_run_clause(context.user.id))
    if job_key:
        query = query.where(JobRun.job_key == job_key)
    if status:
        query = query.where(JobRun.status == status)
    rows = db.scalars(query.order_by(JobRun.created_at.desc(), JobRun.id.desc()).limit(limit)).all()
    return [_run_out(row) for row in rows]


@router.get("/schedule/runs/paged")
def schedule_runs_paged(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=12, ge=1, le=100),
    job_key: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    filters = [_visible_run_clause(context.user.id)]
    if job_key:
        filters.append(JobRun.job_key == job_key)
    if status:
        filters.append(JobRun.status == status)

    query = select(JobRun)
    count_query = select(func.count()).select_from(JobRun)
    if filters:
        query = query.where(*filters)
        count_query = count_query.where(*filters)

    total = int(db.scalar(count_query) or 0)
    rows = list(
        db.scalars(
            query.order_by(JobRun.created_at.desc(), JobRun.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    job_keys = {row.job_key for row in rows}
    job_names = {}
    if job_keys:
        job_names = dict(
            db.execute(
                select(ScheduledJob.job_key, ScheduledJob.name).where(
                    ScheduledJob.job_key.in_(job_keys),
                    _visible_job_clause(context.user.id),
                )
            ).all()
        )
    return _page_out(
        [_run_out(row, job_name=job_names.get(row.job_key)) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.delete("/schedule/runs")
def clear_schedule_runs(
    job_key: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    filters = [JobRun.owner_user_id == context.user.id, JobRun.status != "running"]
    running_filters = [JobRun.owner_user_id == context.user.id, JobRun.status == "running"]
    if job_key:
        filters.append(JobRun.job_key == job_key)
        running_filters.append(JobRun.job_key == job_key)
    if status:
        filters.append(JobRun.status == status)
        running_filters.append(JobRun.status == status)

    skipped_running = int(
        db.scalar(select(func.count()).select_from(JobRun).where(*running_filters)) or 0
    )
    result = db.execute(delete(JobRun).where(*filters))
    db.commit()
    return {
        "deleted": int(result.rowcount or 0),
        "skipped_running": skipped_running,
        "job_key": job_key,
        "status": status,
    }


@router.get("/schedule/runs/{run_id}")
def schedule_run_detail(
    run_id: str,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    run = db.scalar(
        select(JobRun).where(
            JobRun.run_id == run_id,
            _visible_run_clause(context.user.id),
        ).limit(1)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return _run_out(run, output_content=_run_output_content(db, run) or "")


@router.get("/schedule/status")
def schedule_status(
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    now = now_sh()
    heartbeat = db.get(Heartbeat, HEARTBEAT_COMPONENT)
    heartbeat_at = heartbeat.last_seen_at if heartbeat else None
    alive = bool(heartbeat_at and (now - heartbeat_at).total_seconds() < 90)
    today_start = datetime(now.year, now.month, now.day)
    counts = {"success": 0, "failed": 0, "running": 0}
    for status_value, total in db.execute(
        select(JobRun.status, func.count())
        .where(
            _visible_run_clause(context.user.id),
            JobRun.started_at >= today_start,
        )
        .group_by(JobRun.status)
    ).all():
        counts[status_value] = total
    channel = get_channel(db, own_channel_key(db, context.user.id))
    channel_configured = bool(channel and decrypt_value(channel.webhook_url_cipher))
    return {
        "scheduler_enabled": settings.scheduler_enabled,
        "scheduler_alive": alive,
        "heartbeat_at": _shanghai_iso(heartbeat_at),
        "is_trading_day": is_trading_day(now.date()),
        "server_time": _shanghai_iso(now),
        "today": counts,
        "notify_channel": {
            "name": channel.name if channel else "我的飞书",
            "configured": channel_configured,
            "signed": bool(channel and decrypt_value(channel.secret_cipher)),
            "enabled": bool(channel and channel.status == "enabled"),
            "last_ok_at": channel.last_ok_at.isoformat() if channel and channel.last_ok_at else None,
            "last_error": channel.last_error if channel else None,
        },
    }
