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
import threading
import time
from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from backend.app.core.database import SessionLocal
from backend.app.agent_runtime.channel_runs import (
    build_channel_request,
    create_channel_run,
    wait_for_channel_run,
)
from backend.app.agent_runtime.profile_access import target_roles_allow
from backend.app.feishu.feishu import send_feishu
from backend.app.core.models import (
    AgentReport,
    AiChatMessage,
    AiChatSession,
    DigitalHumanProfile,
    Heartbeat,
    JobRun,
    ScheduledJob,
    SystemLog,
    User,
)
from backend.app.core.settings import settings


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
_trade_day_cache: dict[str, bool] = {}
_position_price_sync_lock = threading.Lock()

_CARD_TEMPLATE_BY_JOB = {
    "topnews-morning-0630": "wathet",
    "premarket-brief-0830": "wathet",
    "open-report-0935": "turquoise",
    "close-review-1530": "blue",
    "postmarket-research-1900": "indigo",
    "evening-daily-2200": "indigo",
    "weekend-review": "purple",
    "month-end-review": "orange",
}


def now_sh() -> datetime:
    return datetime.now(_SHANGHAI).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# 时间与交易日判定
# --------------------------------------------------------------------------- #
def is_trading_day(day: date) -> bool:
    if day.isoweekday() >= 6:
        return False
    day_key = day.isoformat()
    if day_key in settings.stock_holidays:
        return False
    cached = _trade_day_cache.get(day_key)
    if cached is not None:
        return cached
    # TuShare is authoritative for exchange holidays and temporary closures. A
    # network/configuration failure deliberately falls back to the old weekday
    # rule so scheduled work remains available in offline development.
    try:
        from backend.app.market.stock_tools import get_trade_cal

        result = get_trade_cal(day.strftime("%Y%m%d"), day.strftime("%Y%m%d"))
        rows = result.get("rows") if isinstance(result, dict) else []
        if rows:
            value = rows[0].get("is_open")
            is_open = str(value).strip().lower() in {"1", "true", "yes"}
            _trade_day_cache[day_key] = is_open
            return is_open
    except Exception:  # noqa: BLE001
        pass
    return True


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


def _position_price_sync_session_open(now: datetime) -> bool:
    current = (now.hour, now.minute)
    return (9, 30) <= current <= (11, 30) or (13, 0) <= current <= (15, 0)


def _is_due(job: ScheduledJob, now: datetime) -> bool:
    schedule_type = job.schedule_type
    if schedule_type == "once":
        if job.run_once_at is None or job.run_once_at > now:
            return False
    elif schedule_type in ("daily", "weekly", "monthly_last"):
        if not job.run_time or job.run_time != now.strftime("%H:%M"):
            return False
        if schedule_type in ("daily", "weekly") and not _weekday_ok(job, now):
            return False
        if schedule_type == "monthly_last" and not _is_last_active_day_of_month(job, now):
            return False
    elif schedule_type == "interval":
        if not _interval_due(job, now):
            return False
        if job.job_kind == "position_price_sync" and not _position_price_sync_session_open(now):
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


def _run_scheduled_agent(
    snap: dict,
    question: str,
    job_run_id: str,
) -> tuple[str, str | None]:
    with SessionLocal() as db:
        profile = db.scalar(
            select(DigitalHumanProfile)
            .where(
                DigitalHumanProfile.code == snap["digital_human_code"],
                DigitalHumanProfile.status == "enabled",
            )
            .limit(1)
        )
        if profile is None:
            raise RuntimeError("Scheduled digital human does not exist or is disabled.")
        owner = db.get(User, snap.get("owner_user_id")) if snap.get("owner_user_id") else None
        if snap.get("owner_user_id") and (owner is None or not owner.is_enabled):
            raise RuntimeError("The scheduled Agent owner is missing or disabled.")
        if owner is None:
            owner = db.scalar(
                select(User).where(User.is_enabled.is_(True)).order_by(User.id).limit(1)
            )
        if owner is None:
            raise RuntimeError("No enabled user can own the scheduled Agent session.")
        if not target_roles_allow(profile.target_roles, owner.role):
            raise RuntimeError("The scheduled Agent is not available to its owner role.")
        chat_session = db.scalar(
            select(AiChatSession)
            .where(
                AiChatSession.digital_human_id == profile.id,
                AiChatSession.source_type == "scheduler",
                AiChatSession.external_conversation_id == snap["job_key"],
            )
            .limit(1)
        )
        if chat_session is None:
            chat_session = AiChatSession(
                user_id=owner.id,
                digital_human_id=profile.id,
                title=f"定时任务 · {snap['name']}"[:160],
                status="active",
                chat_mode="research",
                source_type="scheduler",
                external_conversation_id=snap["job_key"][:160],
                source_label="定时任务",
            )
            db.add(chat_session)
            db.flush()
        history_rows = db.scalars(
            select(AiChatMessage)
            .where(AiChatMessage.session_id == chat_session.id, AiChatMessage.error.is_(None))
            .order_by(AiChatMessage.id.desc())
            .limit(16)
        ).all()
        history = [
            {"role": row.role, "content": row.content}
            for row in reversed(history_rows)
            if row.role in {"user", "assistant"} and row.content
        ]
        session_id = chat_session.id
        owner_id = owner.id
        owner_label = owner.display_name
        db.commit()

    request_payload = build_channel_request(
        session_id=session_id,
        question=question,
        history=history,
        digital_human_code=snap["digital_human_code"],
        source_type="scheduler",
        source_ref=job_run_id,
        user_id=owner_id,
        user_label=owner_label,
        allow_code_execution=bool(snap.get("allow_code_execution")),
        approval_policy=str(snap.get("approval_policy") or "full"),
    )
    run = create_channel_run(request_payload, idempotency_key=f"schedule:{job_run_id}")
    if run["created"]:
        with SessionLocal() as db:
            db.add(AiChatMessage(session_id=session_id, role="user", content=question, event_count=0))
            db.commit()
    answer, model_name, error = wait_for_channel_run(run["run_id"])
    if error:
        raise RuntimeError(error)
    if not answer:
        raise RuntimeError("The scheduled Agent did not return a valid answer.")
    return answer, model_name


def _run_agent_report(
    snap: dict,
    now: datetime,
    job_run_id: str,
) -> tuple[str, int | None, str, str | None]:
    question = _render(snap["prompt_template"], now) or snap["name"]
    answer, model_name = _run_scheduled_agent(snap, question, job_run_id)
    agent_error = None
    if agent_error:
        raise RuntimeError(agent_error)
    if not answer:
        raise RuntimeError("模型未返回有效内容")

    report_id = f"rhythm-{job_run_id}"
    agent_name = DH_TO_AGENT.get(snap["digital_human_code"] or "", "pm")
    title = f"{snap['name']} | {now:%Y-%m-%d}"
    notification_id: int | None = None
    notification_error: str | None = None
    with SessionLocal() as session:
        profile = session.scalar(
            select(DigitalHumanProfile).where(DigitalHumanProfile.code == snap["digital_human_code"]).limit(1)
        )
        report = session.get(AgentReport, report_id)
        if report is None:
            report = AgentReport(
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
            session.add(report)
        else:
            report.task_id = snap["job_key"]
            report.agent_name = agent_name
            report.title = title[:300]
            report.conclusion = answer[:2000]
            report.action_suggestion = "observe"
            report.content = answer
            report.model_name = model_name
        session.commit()
        if snap["push_channel_key"]:
            trigger_label = "手动执行" if snap.get("trigger_type") == "manual" else "自动调度"
            notification = send_feishu(
                session,
                snap["push_channel_key"],
                title,
                answer,
                report_id=report_id,
                card_template=_CARD_TEMPLATE_BY_JOB.get(snap["job_key"], "blue"),
                card_metadata=[
                    ("任务", snap["name"]),
                    ("数字人", profile.name if profile else (snap["digital_human_code"] or "系统")),
                    ("触发方式", trigger_label),
                    ("生成时间", now.strftime("%Y-%m-%d %H:%M:%S")),
                ],
                card_footer="SixRabbits Stock | 自动生成，仅供研究与复盘，不构成投资建议",
            )
            notification_id = notification.id
            if notification.status != "sent":
                notification_error = notification.error or "飞书未确认发送成功"
    return report_id, notification_id, answer, notification_error


def _run_topnews_brief(snapshot: dict, ran_at: datetime) -> str:
    """采集全球市场数据→分析→生成早盘舆情简报→推送飞书。"""
    period = "pre_market"
    if snapshot.get("job_key", "").startswith("topnews-morning"):
        period = "pre_market"
    from backend.app.topnews.handler import run_topnews_brief

    report = run_topnews_brief(period=period)
    title = f"早盘舆情简报 | {ran_at.strftime('%Y-%m-%d')}"
    with SessionLocal() as session:
        send_feishu(
            session,
            snapshot.get("push_channel_key") or "feishu-daily",
            title,
            report,
            card_template=_CARD_TEMPLATE_BY_JOB.get(snapshot["job_key"], "wathet"),
            card_metadata=[
                ("任务", snapshot.get("name", "早盘舆情简报")),
                ("触发方式", "手动执行" if snapshot.get("trigger_type") == "manual" else "自动调度"),
                ("生成时间", ran_at.strftime("%Y-%m-%d %H:%M:%S")),
            ],
            card_footer="SixRabbits Stock | 数据采集自动生成，仅供参考",
        )
    return report


def _run_health_check(
    now: datetime,
    push_channel_key: str | None,
    job_run_id: str,
) -> tuple[str, int | None, str | None]:
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
        if not push_channel_key:
            return f"发现 {len(problems)} 项异常，任务未配置推送通道", None, None
        with SessionLocal() as session:
            notification = send_feishu(
                session,
                push_channel_key,
                "系统健康检查 | 发现异常",
                body,
                event_id=f"rhythm-health-{job_run_id}",
                card_kind="alert",
                card_template="red",
                card_metadata=[
                    ("任务", "系统健康检查"),
                    ("异常项", str(len(problems))),
                    ("检查时间", now.strftime("%Y-%m-%d %H:%M:%S")),
                    ("处理状态", "待核查"),
                ],
                card_footer="SixRabbits Stock | 系统告警，请及时核查",
            )
            push_error = None if notification.status == "sent" else (notification.error or "飞书未确认发送成功")
            return f"发现 {len(problems)} 项异常，已尝试推送任务群", notification.id, push_error
    return "健康检查通过，无异常", None, None


def _run_position_price_sync(now: datetime) -> str:
    if not _position_price_sync_lock.acquire(blocking=False):
        return "上一轮持仓价格同步仍在执行，本轮已跳过"
    try:
        return _run_position_price_sync_locked(now)
    finally:
        _position_price_sync_lock.release()


def _run_position_price_sync_locked(now: datetime) -> str:
    """规则任务（不经 LLM）：批量同步全部 open 持仓价格，重算市值/浮盈与账户资金。

    交易时段每分钟调用一次 TuShare rt_k 批量接口；外部请求期间不持有数据库连接。
    整批请求失败时保留上一轮价格并令任务失败，避免用空值覆盖有效行情。
    """
    from backend.app.core.models import Instrument, Position, PositionAccount, PositionHoldingSnapshot
    from backend.app.data.repository import StockRepository
    from backend.app.market.stock_tools import get_realtime_quotes

    # 会话一（短）：确定同步对象并提交。绝不带着未提交事务去做外部 HTTP——
    # 补建快照的 FK 会给 instruments 父行持共享锁，而行情工具的缓存写入要更新同一行，
    # 长事务会把对方锁到超时（模块纪律：外部调用期间不持有数据库连接）。
    with SessionLocal() as session:
        latest_ids = (
            select(func.max(PositionHoldingSnapshot.id))
            .group_by(PositionHoldingSnapshot.account_id, PositionHoldingSnapshot.symbol)
        )
        snaps = [
            s
            for s in session.scalars(
                select(PositionHoldingSnapshot).where(PositionHoldingSnapshot.id.in_(latest_ids))
            ).all()
            if (s.status or "open") == "open" and (s.position_quantity or 0) > 0
        ]

        # 只在 positions 表、还没有任何快照的 open 持仓（如 seed/历史数据）：补建一条快照，
        # 否则展示层（读最新快照）的最新价/市值永远是空。
        covered = {(s.account_id, s.symbol) for s in snaps}
        orphan_positions = [
            p
            for p in session.scalars(select(Position).where(Position.status == "open")).all()
            if (p.quantity or 0) > 0 and (p.account_id, p.symbol) not in covered
        ]
        for pos in orphan_positions:
            instrument = session.get(Instrument, pos.symbol)
            snap = PositionHoldingSnapshot(
                account_id=pos.account_id,
                symbol=pos.symbol,
                symbol_name=instrument.name if instrument else pos.symbol,
                as_of_date=now.date().isoformat(),
                position_quantity=pos.quantity,
                cost_price=pos.cost_price,
                cost_amount=round(float(pos.cost_price) * float(pos.quantity), 4) if pos.cost_price else None,
                position_pct=pos.position_pct,
                stop_price=pos.stop_price,
                source_type="price_sync",
                status="open",
            )
            session.add(snap)
            snaps.append(snap)
        session.flush()
        snap_ids = [s.id for s in snaps]
        symbols = sorted({s.symbol for s in snaps})
        session.commit()

    if not snap_ids:
        return "无 open 持仓，跳过价格同步"

    # 无会话阶段：一次批量拉取全部持仓行情，不占用数据库连接。
    batch = get_realtime_quotes(symbols)
    if not isinstance(batch, dict) or batch.get("error"):
        reason = batch.get("error") if isinstance(batch, dict) else "返回格式错误"
        raise RuntimeError(f"实时行情同步失败: {reason}")
    quotes = batch.get("quotes") or {}
    failures = batch.get("missing_symbols") or []
    if not quotes:
        raise RuntimeError("实时行情同步失败: TuShare rt_k 未返回任何有效持仓行情")

    # 会话二（短）：按 id 重载快照写价，并重算账户资金
    updated = 0
    touched_accounts: set[int] = set()
    with SessionLocal() as session:
        snaps = session.scalars(
            select(PositionHoldingSnapshot).where(PositionHoldingSnapshot.id.in_(snap_ids))
        ).all()
        for snap in snaps:
            quote = quotes.get(snap.symbol)
            if quote is None:
                continue
            price = float(quote["price"])
            qty = float(snap.position_quantity or 0)
            snap.latest_price = price
            snap.market_value = round(price * qty, 4)
            cost_amount = snap.cost_amount
            if cost_amount is None and snap.cost_price is not None:
                cost_amount = round(float(snap.cost_price) * qty, 4)
            if cost_amount is not None:
                snap.floating_pnl = round(snap.market_value - float(cost_amount), 4)
                if float(cost_amount):
                    snap.floating_pnl_pct = round(snap.floating_pnl / float(cost_amount) * 100, 4)
            pre_close = quote.get("pre_close")
            if pre_close:
                snap.day_pnl = round((price - float(pre_close)) * qty, 4)
            updated += 1
            if snap.account_id:
                touched_accounts.add(snap.account_id)

        repo = StockRepository(session)
        for acc_id in sorted(touched_accounts):
            account = session.get(PositionAccount, acc_id)
            if account is not None:
                repo._recompute_account_funds(account)
                account.fund_updated_at = now
        session.commit()

    preview = (
        f"实时同步 {len(quotes)} 个标的行情，更新 {updated} 条持仓快照，"
        f"重算 {len(touched_accounts)} 个账户资金；来源 {batch.get('source')}，"
        f"耗时 {batch.get('ms', 0)}ms"
    )
    if failures:
        preview += f"；{len(failures)} 个标的取价失败: {', '.join(failures[:8])}"
    return preview


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
        "owner_user_id": job.owner_user_id,
        "allow_code_execution": bool(job.allow_code_execution),
        "approval_policy": job.approval_policy,
    }


def _finalize_run(
    run_id: str,
    status: str,
    *,
    report_id: str | None = None,
    notification_id: int | None = None,
    output_content: str | None = None,
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
        run.output_preview = (output_content or "")[:1000] or None
        run.output_content = output_content or None
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
        run = session.scalar(select(JobRun).where(JobRun.run_id == run_id).limit(1))
        if snapshot is not None:
            snapshot["trigger_type"] = run.trigger_type if run else "schedule"
    if snapshot is None:
        _finalize_run(run_id, "failed", error="任务不存在")
        return

    status = "success"
    report_id: str | None = None
    notification_id: int | None = None
    output_content: str | None = None
    error: str | None = None
    try:
        if snapshot["job_kind"] == "health_check":
            output_content, notification_id, push_error = _run_health_check(
                ran_at,
                snapshot["push_channel_key"],
                run_id,
            )
            if push_error:
                status = "failed"
                error = f"健康检查已完成，但飞书任务群推送失败: {push_error}"
        elif snapshot["job_kind"] == "position_price_sync":
            output_content = _run_position_price_sync(ran_at)
        elif snapshot["job_kind"] == "rag_ingest":
            from backend.app.ai.rag_ingest import run_ingest

            output_content = run_ingest()
        elif snapshot["job_kind"] in {"agent_report", "agent_task"}:
            report_id, notification_id, output_content, push_error = _run_agent_report(
                snapshot,
                ran_at,
                run_id,
            )
            if push_error:
                status = "failed"
                error = f"报告已生成，但飞书任务群推送失败: {push_error}"
        elif snapshot["job_kind"] == "topnews_brief":
            output_content = _run_topnews_brief(snapshot, ran_at)
        else:
            status = "skipped"
            output_content = f"暂不支持的任务类型: {snapshot['job_kind']}"
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        error = str(exc)[:1000]
        # 任务失败只落日志，由次日健康检查统一汇总，避免同群刷屏。
        _log("ERROR", f"节律任务 {job_key} 执行失败", {"error": error})

    duration_ms = int((time.monotonic() - started) * 1000)
    _finalize_run(
        run_id,
        status,
        report_id=report_id,
        notification_id=notification_id,
        output_content=output_content,
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
            if job.job_kind == "position_price_sync":
                running = session.scalar(
                    select(JobRun).where(
                        JobRun.job_key == job.job_key,
                        JobRun.status == "running",
                        JobRun.started_at >= now - timedelta(minutes=2),
                    ).limit(1)
                )
                if running is not None:
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
                if job.schedule_type == "once":
                    job.enabled = False
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
def _recover_stale_runs() -> list[tuple[str, str]]:
    recovered: list[tuple[str, str]] = []
    with SessionLocal() as session:
        stale = session.scalars(select(JobRun).where(JobRun.status == "running")).all()
        for run in stale:
            job = session.scalar(
                select(ScheduledJob).where(ScheduledJob.job_key == run.job_key).limit(1)
            )
            if job is None:
                run.status = "failed"
                run.error = "进程重启后未找到对应定时任务，无法恢复"
                run.finished_at = now_sh()
                continue
            run.error = None
            run.finished_at = None
            recovered.append((run.job_key, run.run_id))
        if stale:
            session.commit()
    return recovered


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
            for job_key, run_id in recovered:
                if run_id not in _inflight_runs:
                    _dispatch(job_key, run_id)
            await asyncio.to_thread(
                _log,
                "WARNING",
                f"启动恢复：{len(recovered)} 个中断任务已重新接管执行",
            )
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
        "job_key": "topnews-morning-0630",
        "name": "早盘舆情简报",
        "description": "采集美股、期货、财联社、宏观与资金面数据，生成结构化早盘简报。",
        "job_kind": "topnews_brief",
        "schedule_type": "daily",
        "run_time": "06:30",
        "weekday_mask": "1,2,3,4,5",
        "trading_day_only": True,
        "push_channel_key": "feishu-daily",
        "enabled": True,
    },
    {
        "job_key": "health-check-0815",
        "name": "系统健康检查",
        "description": "盘前检查数据库、心跳、错误日志与失败任务；仅发现异常时推送任务群。",
        "job_kind": "health_check",
        "schedule_type": "daily",
        "run_time": "08:15",
        "weekday_mask": "1,2,3,4,5",
        "trading_day_only": True,
        "push_channel_key": "feishu-daily",
        "enabled": True,
    },
    {
        "job_key": "position-price-sync-1505",
        "name": "持仓价格分钟同步",
        "description": "交易时段每分钟批量拉取实时行情，刷新全部持仓的最新价/市值/浮盈并重算账户资金（不经 LLM、不推送）。",
        "job_kind": "position_price_sync",
        "schedule_type": "interval",
        "run_time": None,
        "interval_minutes": 1,
        "window_start": "09:30",
        "window_end": "15:00",
        "weekday_mask": "1,2,3,4,5",
        "trading_day_only": True,
        "enabled": True,
    },
    {
        "job_key": "rag-ingest-1630",
        "name": "知识库增量索引",
        "description": "收盘后把新增文档/报告切块嵌入进知识库（规则任务不经 LLM、不推送；失败自动重试）。",
        "job_kind": "rag_ingest",
        "schedule_type": "daily",
        "run_time": "16:30",
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
        "description": "总调度统稿当日各方输入，形成日报推送任务群。",
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
    """补齐内置任务；仅基础设施型价格同步任务会随版本迁移调度配置。"""
    created = 0
    for item in DEFAULT_JOBS:
        existing = session.scalar(select(ScheduledJob).where(ScheduledJob.job_key == item["job_key"]).limit(1))
        if existing is not None:
            if existing.job_kind == "position_price_sync":
                # 系统内置行情任务属于基础设施配置；版本升级时迁移为分钟同步。
                existing.name = item["name"]
                existing.description = item.get("description")
                existing.schedule_type = item["schedule_type"]
                existing.run_time = item.get("run_time")
                existing.interval_minutes = item.get("interval_minutes")
                existing.window_start = item.get("window_start")
                existing.window_end = item.get("window_end")
                existing.weekday_mask = item.get("weekday_mask")
                existing.trading_day_only = item.get("trading_day_only", True)
                existing.enabled = item.get("enabled", True)
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
    pending = [task for task in _background_tasks if not task.done()]
    if not pending:
        return
    grace = max(1, settings.scheduler_shutdown_grace_seconds)
    done, still_pending = await asyncio.wait(pending, timeout=grace)
    for task in done:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
    if still_pending:
        await asyncio.to_thread(
            _log,
            "WARNING",
            f"关闭等待超时：{len(still_pending)} 个调度任务将在下次启动时恢复",
        )
        for task in still_pending:
            task.cancel()
