"""每日节律调度器：进程内 asyncio 循环，按 scheduled_jobs 定时用数字人生成报告并推送飞书。

设计要点（对齐甲方方案第六节与代码评审）：
- 时区统一 Asia/Shanghai 转 naive，与全库 naive datetime 惯例兼容。
- fire_key 唯一约束做认领锁，重启/多 tick 天然防重复触发。
- LLM 消费期间不持有数据库连接；写库/发送各用独立短会话。
- 健康检查纯规则、不经 LLM，异常才推告警群（零噪音纪律）。
- Agent 任务失败不推告警群，只落 system_logs，避免污染告警线。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from backend.app.core.database import SessionLocal
from backend.app.feishu.feishu import send_alert, send_feishu
from backend.app.core.models import (
    AgentReport,
    DataSourceConfig,
    DigitalHumanProfile,
    Heartbeat,
    JobRun,
    ScheduledJob,
    SystemLog,
)
from backend.app.core.settings import settings
from backend.app.agents.stock_agent import run_agent_events


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
HEARTBEAT_COMPONENT = "rhythm_scheduler"
_MAX_CONCURRENT_JOBS = 2

# 数字人 code -> agent_profiles.agent_name（AgentReport.agent_name 外键指向后者）
DH_TO_AGENT = {
    "stock-pm": "pm",
    "risk-officer": "risk",
    "market-sentinel": "sentinel",
    "research-analyst": "researcher",
    "intelligence-officer": "intelligence",
    "quant-analyst": "quant",
    "macro-strategist": "macro",
    "short-term-hunter": "hunter",
}

_scheduler_task: asyncio.Task | None = None
_background_tasks: set[asyncio.Task] = set()
_inflight_runs: set[str] = set()
_semaphore: asyncio.Semaphore | None = None


def now_sh() -> datetime:
    return datetime.now(_SHANGHAI).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# 时间与交易日判定
# --------------------------------------------------------------------------- #
def is_trading_day(day: date) -> bool:
    if day.isoweekday() >= 6:
        return False
    return day.isoformat() not in settings.stock_holidays


def _weekday_ok(job: ScheduledJob, now: datetime) -> bool:
    if not job.weekday_mask:
        return True
    allowed = {item.strip() for item in job.weekday_mask.split(",") if item.strip()}
    return str(now.isoweekday()) in allowed


def _is_last_active_day_of_month(job: ScheduledJob, now: datetime) -> bool:
    today = now.date()
    if job.trading_day_only:
        if not is_trading_day(today):
            return False
        cursor = today + timedelta(days=1)
        while cursor.month == today.month:
            if is_trading_day(cursor):
                return False
            cursor += timedelta(days=1)
        return True
    return (today + timedelta(days=1)).month != today.month


def _interval_due(job: ScheduledJob, now: datetime) -> bool:
    if not job.interval_minutes or job.interval_minutes <= 0:
        return False
    hhmm = now.strftime("%H:%M")
    if job.window_start and hhmm < job.window_start:
        return False
    if job.window_end and hhmm > job.window_end:
        return False
    anchor = job.window_start or "00:00"
    anchor_minutes = int(anchor[:2]) * 60 + int(anchor[3:5])
    now_minutes = now.hour * 60 + now.minute
    return (now_minutes - anchor_minutes) % job.interval_minutes == 0


def _is_due(job: ScheduledJob, now: datetime) -> bool:
    schedule_type = job.schedule_type
    if schedule_type in ("daily", "weekly", "monthly_last"):
        if not job.run_time or job.run_time != now.strftime("%H:%M"):
            return False
        if schedule_type in ("daily", "weekly") and not _weekday_ok(job, now):
            return False
        if schedule_type == "monthly_last" and not _is_last_active_day_of_month(job, now):
            return False
    elif schedule_type == "interval":
        if not _interval_due(job, now):
            return False
    else:
        return False

    if job.trading_day_only and not is_trading_day(now.date()):
        return False
    return True


# --------------------------------------------------------------------------- #
# 小工具：日志 / 心跳 / 模板渲染
# --------------------------------------------------------------------------- #
def _log(level: str, message: str, context: dict | None = None) -> None:
    from backend.app.core.applog import log_event

    log_event("scheduler", level, message, context=context)


def _upsert_heartbeat(session, component: str, status: str, now: datetime, detail: dict | None = None) -> None:
    heartbeat = session.get(Heartbeat, component)
    detail_json = json.dumps(detail, ensure_ascii=False) if detail else None
    if heartbeat is None:
        session.add(Heartbeat(component=component, status=status, last_seen_at=now, detail_json=detail_json))
    else:
        heartbeat.status = status
        heartbeat.last_seen_at = now
        if detail_json is not None:
            heartbeat.detail_json = detail_json


def _render(template: str | None, now: datetime) -> str:
    if not template:
        return ""
    return (
        template.replace("{date}", now.strftime("%Y-%m-%d"))
        .replace("{weekday}", _WEEKDAYS[now.weekday()])
        .replace("{time}", now.strftime("%H:%M"))
    )


def _build_digital_human_context(code: str | None) -> str | None:
    if not code:
        return None
    with SessionLocal() as session:
        profile = session.scalar(select(DigitalHumanProfile).where(DigitalHumanProfile.code == code).limit(1))
        if profile is None:
            return None
        return "\n".join(
            [
                "当前触发方: 每日节律调度器（系统自动任务，非人工提问）",
                f"当前数字人: {profile.name}",
                f"风格: {profile.avatar_tone}",
                f"职责: {profile.description}",
                f"提示词: {profile.system_prompt}",
            ]
        )


# --------------------------------------------------------------------------- #
# 无头 Agent 报告：消费 run_agent_events 生成器，收集最终答案
# --------------------------------------------------------------------------- #
def _consume_agent(question: str, context: str | None, code: str | None) -> tuple[str, str | None, str | None]:
    buffer: list[str] = []
    answer = ""
    model_name: str | None = None
    error: str | None = None
    generator = run_agent_events(question, [], context, None, "research", None, code)
    try:
        for event in generator:
            kind = event.get("type")
            if kind == "model_selected":
                model_name = (event.get("model") or {}).get("model_name")
            elif kind == "content_delta":
                buffer.append(str(event.get("content") or ""))
            elif kind == "content_end":
                if event.get("role") == "answer":
                    answer = "".join(buffer).strip()
                buffer.clear()
            elif kind == "error":
                error = str(event.get("content") or "模型响应失败")
                break
    finally:
        generator.close()
    return answer, model_name, error


def _run_agent_report(snap: dict, now: datetime) -> tuple[str, int | None, str]:
    question = _render(snap["prompt_template"], now) or snap["name"]
    context = _build_digital_human_context(snap["digital_human_code"])
    answer, model_name, agent_error = _consume_agent(question, context, snap["digital_human_code"])
    if agent_error:
        raise RuntimeError(agent_error)
    if not answer:
        raise RuntimeError("模型未返回有效内容")

    report_id = f"rhythm-{snap['job_key']}-{now:%Y%m%d}-{uuid4().hex[:8]}"
    agent_name = DH_TO_AGENT.get(snap["digital_human_code"] or "", "pm")
    title = f"{snap['name']}·{now:%Y-%m-%d}"
    notification_id: int | None = None
    with SessionLocal() as session:
        session.add(
            AgentReport(
                report_id=report_id,
                task_id=snap["job_key"],
                agent_name=agent_name,
                symbol=None,
                title=title[:300],
                conclusion=answer[:2000],
                action_suggestion="observe",
                content=answer,
                model_name=model_name,
            )
        )
        session.commit()
        if snap["push_channel_key"]:
            notification = send_feishu(session, snap["push_channel_key"], title, answer, report_id=report_id)
            notification_id = notification.id
    return report_id, notification_id, answer[:500]


def _run_health_check(now: datetime) -> str:
    problems: list[str] = []
    with SessionLocal() as session:
        try:
            session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"数据库连通性异常: {str(exc)[:120]}")
        stale = [
            hb.component
            for hb in session.scalars(select(Heartbeat)).all()
            if hb.component != HEARTBEAT_COMPONENT
            and hb.last_seen_at
            and (now - hb.last_seen_at).total_seconds() > 1800
        ]
        if stale:
            problems.append(f"组件心跳超时: {', '.join(stale)}")
        error_logs = (
            session.scalar(
                select(func.count())
                .select_from(SystemLog)
                .where(SystemLog.level == "ERROR", SystemLog.created_at >= now - timedelta(hours=12))
            )
            or 0
        )
        if error_logs:
            problems.append(f"近 12 小时有 {error_logs} 条 ERROR 日志")
        failed_runs = (
            session.scalar(
                select(func.count())
                .select_from(JobRun)
                .where(JobRun.status == "failed", JobRun.created_at >= now - timedelta(hours=24))
            )
            or 0
        )
        if failed_runs:
            problems.append(f"近 24 小时有 {failed_runs} 个定时任务执行失败")
        _upsert_heartbeat(session, HEARTBEAT_COMPONENT, "ok", now, {"checked_at": now.isoformat()})
        session.commit()

    # 借每日健康检查清理一次过期 system_logs，避免只增表无限膨胀
    from backend.app.core.applog import prune_system_logs

    prune_system_logs()

    if problems:
        body = "系统健康检查发现以下异常，请及时处理：\n" + "\n".join(f"- {item}" for item in problems)
        with SessionLocal() as session:
            send_alert(session, "【系统健康检查异常】", body)
        return f"发现 {len(problems)} 项异常，已推送告警群"
    return "健康检查通过，无异常"


# --------------------------------------------------------------------------- #
# 执行器
# --------------------------------------------------------------------------- #
def _job_snapshot(job: ScheduledJob) -> dict:
    return {
        "job_key": job.job_key,
        "name": job.name,
        "job_kind": job.job_kind,
        "digital_human_code": job.digital_human_code,
        "prompt_template": job.prompt_template,
        "push_channel_key": job.push_channel_key,
    }


def _finalize_run(
    run_id: str,
    status: str,
    *,
    report_id: str | None = None,
    notification_id: int | None = None,
    output_preview: str | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    with SessionLocal() as session:
        run = session.scalar(select(JobRun).where(JobRun.run_id == run_id).limit(1))
        if run is None:
            return
        run.status = status
        run.finished_at = now_sh()
        run.duration_ms = duration_ms
        run.report_id = report_id
        run.notification_id = notification_id
        run.output_preview = (output_preview or "")[:1000] or None
        run.error = error
        session.commit()


def _update_job_status(job_key: str, status: str, error: str | None, ran_at: datetime) -> None:
    with SessionLocal() as session:
        job = session.scalar(select(ScheduledJob).where(ScheduledJob.job_key == job_key).limit(1))
        if job is None:
            return
        job.last_run_at = ran_at
        job.last_status = status
        job.last_error = error
        session.commit()


def _execute_job(job_key: str, run_id: str) -> None:
    """同步执行一个任务，在线程中被调用；绝不抛出，所有结果落 job_runs。"""
    started = time.monotonic()
    ran_at = now_sh()
    with SessionLocal() as session:
        job = session.scalar(select(ScheduledJob).where(ScheduledJob.job_key == job_key).limit(1))
        snapshot = _job_snapshot(job) if job else None
    if snapshot is None:
        _finalize_run(run_id, "failed", error="任务不存在")
        return

    status = "success"
    report_id: str | None = None
    notification_id: int | None = None
    output_preview: str | None = None
    error: str | None = None
    try:
        if snapshot["job_kind"] == "health_check":
            output_preview = _run_health_check(ran_at)
        elif snapshot["job_kind"] == "agent_report":
            report_id, notification_id, output_preview = _run_agent_report(snapshot, ran_at)
        else:
            status = "skipped"
            output_preview = f"暂不支持的任务类型: {snapshot['job_kind']}"
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        error = str(exc)[:1000]
        # 任务失败只落日志，不推告警群（守零噪音纪律，由次日健康检查汇总）
        _log("ERROR", f"节律任务 {job_key} 执行失败", {"error": error})

    duration_ms = int((time.monotonic() - started) * 1000)
    _finalize_run(
        run_id,
        status,
        report_id=report_id,
        notification_id=notification_id,
        output_preview=output_preview,
        error=error,
        duration_ms=duration_ms,
    )
    _update_job_status(job_key, status, error, ran_at)


# --------------------------------------------------------------------------- #
# 认领与派发
# --------------------------------------------------------------------------- #
def _write_scheduler_heartbeat(now: datetime, detail: dict) -> None:
    with SessionLocal() as session:
        _upsert_heartbeat(session, HEARTBEAT_COMPONENT, "ok", now, detail)
        session.commit()


def _claim_due_jobs(now: datetime) -> list[tuple[str, str]]:
    claimed: list[tuple[str, str]] = []
    minute_key = now.strftime("%Y-%m-%dT%H:%M")
    with SessionLocal() as session:
        jobs = session.scalars(select(ScheduledJob).where(ScheduledJob.enabled.is_(True))).all()
        for job in jobs:
            if not _is_due(job, now):
                continue
            run_id = uuid4().hex
            session.add(
                JobRun(
                    run_id=run_id,
                    job_key=job.job_key,
                    fire_key=f"{job.job_key}@{minute_key}",
                    trigger_type="schedule",
                    status="running",
                    started_at=now,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                continue
            claimed.append((job.job_key, run_id))
    return claimed


def _claim_manual_run(job_key: str) -> str | None:
    with SessionLocal() as session:
        job = session.scalar(select(ScheduledJob).where(ScheduledJob.job_key == job_key).limit(1))
        if job is None:
            return None
        running = session.scalar(
            select(JobRun).where(JobRun.job_key == job_key, JobRun.status == "running").limit(1)
        )
        if running is not None:
            return None
        run_id = uuid4().hex
        session.add(
            JobRun(
                run_id=run_id,
                job_key=job_key,
                fire_key=f"{job_key}@manual-{run_id[:8]}",
                trigger_type="manual",
                status="running",
                started_at=now_sh(),
            )
        )
        session.commit()
        return run_id


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT_JOBS)
    return _semaphore


async def _run_job_async(job_key: str, run_id: str) -> None:
    try:
        async with _get_semaphore():
            await asyncio.to_thread(_execute_job, job_key, run_id)
    except Exception as exc:  # noqa: BLE001
        await asyncio.to_thread(_log, "ERROR", f"节律任务 {job_key} 线程异常", {"error": str(exc)[:500]})
    finally:
        _inflight_runs.discard(run_id)


def _dispatch(job_key: str, run_id: str) -> None:
    _inflight_runs.add(run_id)
    task = asyncio.create_task(_run_job_async(job_key, run_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def dispatch_manual_run(job_key: str) -> str | None:
    """手动触发：无视交易日/时间窗，立即后台执行；已在运行返回 None。"""
    run_id = await asyncio.to_thread(_claim_manual_run, job_key)
    if run_id is None:
        return None
    _dispatch(job_key, run_id)
    return run_id


# --------------------------------------------------------------------------- #
# 调度循环
# --------------------------------------------------------------------------- #
def _recover_stale_runs() -> int:
    with SessionLocal() as session:
        stale = session.scalars(select(JobRun).where(JobRun.status == "running")).all()
        for run in stale:
            run.status = "failed"
            run.error = "进程重启，任务被中断"
            run.finished_at = now_sh()
        if stale:
            session.commit()
        return len(stale)


async def _tick() -> None:
    now = now_sh()
    await asyncio.to_thread(
        _write_scheduler_heartbeat,
        now,
        {"tick_at": now.isoformat(), "inflight": len(_inflight_runs)},
    )
    claimed = await asyncio.to_thread(_claim_due_jobs, now)
    for job_key, run_id in claimed:
        if run_id in _inflight_runs:
            continue
        _dispatch(job_key, run_id)


async def _scheduler_loop() -> None:
    await asyncio.sleep(15)
    try:
        recovered = await asyncio.to_thread(_recover_stale_runs)
        if recovered:
            await asyncio.to_thread(_log, "WARNING", f"启动恢复：{recovered} 个中断的任务已标记为失败")
    except Exception as exc:  # noqa: BLE001
        await asyncio.to_thread(_log, "ERROR", "启动恢复失败", {"error": str(exc)[:500]})

    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_log, "ERROR", "调度器 tick 异常", {"error": str(exc)[:500]})
        await asyncio.sleep(max(5, settings.scheduler_tick_seconds))


# --------------------------------------------------------------------------- #
# 内置节律任务（幂等预置，只补缺行，绝不覆盖用户在前端的修改）
# --------------------------------------------------------------------------- #
DEFAULT_JOBS = [
    {
        "job_key": "health-check-0815",
        "name": "系统健康检查",
        "description": "盘前检查数据库、心跳、错误日志与失败任务；异常才推告警群。",
        "job_kind": "health_check",
        "schedule_type": "daily",
        "run_time": "08:15",
        "weekday_mask": "1,2,3,4,5",
        "trading_day_only": True,
        "enabled": True,
    },
    {
        "job_key": "premarket-brief-0830",
        "name": "盘前简报",
        "description": "情报官汇总隔夜要闻、公告与情绪，输出今日重点。",
        "job_kind": "agent_report",
        "schedule_type": "daily",
        "run_time": "08:30",
        "weekday_mask": "1,2,3,4,5",
        "trading_day_only": True,
        "digital_human_code": "intelligence-officer",
        "prompt_template": "请生成 {date}（{weekday}）的A股盘前简报：汇总影响持仓和自选池的隔夜要闻、公告、政策与情绪信号，区分事实/推断/风险，并给出今日重点关注清单。",
        "push_channel_key": "feishu-daily",
    },
    {
        "job_key": "open-report-0935",
        "name": "开盘快报",
        "description": "盘中哨兵播报集合竞价、异动与持仓关注点。",
        "job_kind": "agent_report",
        "schedule_type": "daily",
        "run_time": "09:35",
        "weekday_mask": "1,2,3,4,5",
        "trading_day_only": True,
        "digital_human_code": "market-sentinel",
        "prompt_template": "请生成 {date} 的A股开盘快报：结合集合竞价、涨跌幅异动和持仓/自选池，短句列出需要关注的标的与信号，只报异常。",
        "push_channel_key": "feishu-daily",
    },
    {
        "job_key": "close-review-1530",
        "name": "收盘快评",
        "description": "短线猎手复盘涨停、炸板、龙虎与情绪周期。",
        "job_kind": "agent_report",
        "schedule_type": "daily",
        "run_time": "15:30",
        "weekday_mask": "1,2,3,4,5",
        "trading_day_only": True,
        "digital_human_code": "short-term-hunter",
        "prompt_template": "请生成 {date} 的A股收盘快评：复盘持仓和自选池的当日表现、涨停炸板与情绪阶段，给出仓位上限与冷静期提醒，不输出下单指令。",
        "push_channel_key": "feishu-daily",
    },
    {
        "job_key": "postmarket-research-1900",
        "name": "盘后深研",
        "description": "研究员对持仓池做深度研报，落库不推送。",
        "job_kind": "agent_report",
        "schedule_type": "daily",
        "run_time": "19:00",
        "weekday_mask": "1,2,3,4,5",
        "trading_day_only": True,
        "digital_human_code": "research-analyst",
        "prompt_template": "请对持仓池逐一生成 {date} 的深度研究备忘录：整理事件、报告与证据，标注数据缺口，不得编造外部事实。",
        "push_channel_key": None,
    },
    {
        "job_key": "evening-daily-2200",
        "name": "晚间日报",
        "description": "总调度统稿当日各方输入，形成日报推日报群。",
        "job_kind": "agent_report",
        "schedule_type": "daily",
        "run_time": "22:00",
        "weekday_mask": None,
        "trading_day_only": False,
        "digital_human_code": "stock-pm",
        "prompt_template": "请统稿 {date}（{weekday}）的A股研究台晚间日报：汇总当日事件、持仓与自选变化、风控要点和明日计划，输出事实、推断、风险和下一步动作。",
        "push_channel_key": "feishu-daily",
    },
    {
        "job_key": "weekend-review",
        "name": "周末深研专题",
        "description": "量化分析师做因子更新与自选池梳理。",
        "job_kind": "agent_report",
        "schedule_type": "weekly",
        "run_time": "10:00",
        "weekday_mask": "6",
        "trading_day_only": False,
        "digital_human_code": "quant-analyst",
        "prompt_template": "请生成 {date} 的周末深研专题：更新持仓和自选池的量化信号、因子与筹码分析，列出下周需跟踪的验证指标与数据缺口。",
        "push_channel_key": "feishu-daily",
    },
    {
        "job_key": "month-end-review",
        "name": "月末出池提名",
        "description": "风控官做月末出池提名与记忆抽查。",
        "job_kind": "agent_report",
        "schedule_type": "monthly_last",
        "run_time": "16:30",
        "weekday_mask": None,
        "trading_day_only": False,
        "digital_human_code": "risk-officer",
        "prompt_template": "请生成 {date} 的月末复盘：对持仓和自选池做出池提名，检查止损纪律与风险事件，给出反方观点与可验证条件。",
        "push_channel_key": "feishu-daily",
    },
]

_JOB_SORT = {item["job_key"]: index * 10 for index, item in enumerate(DEFAULT_JOBS)}


def ensure_default_jobs(session) -> int:
    """预置内置节律任务；已存在的行不动（保留用户改过的时间/提示词/启停）。"""
    created = 0
    for item in DEFAULT_JOBS:
        existing = session.scalar(select(ScheduledJob).where(ScheduledJob.job_key == item["job_key"]).limit(1))
        if existing is not None:
            continue
        session.add(
            ScheduledJob(
                job_key=item["job_key"],
                name=item["name"],
                description=item.get("description"),
                job_kind=item["job_kind"],
                schedule_type=item["schedule_type"],
                run_time=item.get("run_time"),
                weekday_mask=item.get("weekday_mask"),
                interval_minutes=item.get("interval_minutes"),
                window_start=item.get("window_start"),
                window_end=item.get("window_end"),
                trading_day_only=item.get("trading_day_only", True),
                digital_human_code=item.get("digital_human_code"),
                agent_name=DH_TO_AGENT.get(item.get("digital_human_code") or ""),
                prompt_template=item.get("prompt_template"),
                push_channel_key=item.get("push_channel_key"),
                enabled=item.get("enabled", False),
                sort_order=_JOB_SORT[item["job_key"]],
            )
        )
        created += 1
    if created:
        session.flush()
    return created


def start_rhythm_scheduler() -> None:
    global _scheduler_task
    if not settings.scheduler_enabled:
        return
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    _scheduler_task = asyncio.create_task(_scheduler_loop())


async def stop_rhythm_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is None:
        return
    _scheduler_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _scheduler_task
    _scheduler_task = None
