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
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from backend.app.core.database import SessionLocal
from backend.app.agent_runtime.channel_runs import (
    build_channel_request,
    create_channel_run,
    wait_for_channel_run,
)
from backend.app.agent_runtime.profile_access import target_roles_allow
from backend.app.ai.plain_rewrite import rewrite_plain
from backend.app.data.share_service import ShareSpec
from backend.app.feishu.feishu import send_feishu
from backend.app.plans.rules import MARKET_CLOSE, plan_source_for_job
from backend.app.plans.service import (
    PLAN_SAVE_INSTRUCTION,
    ActivationResult,
    activate_for_report,
    current_plan_meta,
    reconciliation_text,
    repair_question,
    repair_request,
)
from backend.app.core.models import (
    AgentReport,
    AgentRun,
    AgentTeamRun,
    AiChatMessage,
    AiChatSession,
    DigitalHumanProfile,
    Heartbeat,
    JobRun,
    MarketTradingCalendar,
    ScheduledJob,
    SystemLog,
    User,
)
from backend.app.core.settings import settings
from backend.app.market.digital_human_tools import is_role_profile


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
HEARTBEAT_COMPONENT = "rhythm_scheduler"
POSITION_PRICE_SYNC_JOB_KEY = "position-price-sync-1505"
AUTOMATIC_SELECTION_DATA_JOB_KEY = "automatic-selection-data-1545"
AUTOMATIC_SELECTION_JOB_KEY = "automatic-selection-1610"
_AUTOMATIC_SELECTION_CLAIM_GROUP = tuple(
    sorted(
        (
            AUTOMATIC_SELECTION_DATA_JOB_KEY,
            AUTOMATIC_SELECTION_JOB_KEY,
        )
    )
)
_SAME_DAY_CATCHUP_JOB_KEYS = frozenset(_AUTOMATIC_SELECTION_CLAIM_GROUP)
# TuShare 当天数据 17:00 前后才发齐；数据入库失败后每 30 分钟重试，22:00 后当天不再重试。
AUTOMATIC_SELECTION_DATA_RETRY_MINUTES = 30
AUTOMATIC_SELECTION_DATA_RETRY_UNTIL = "22:00"
PLAN_REPAIR_TIMEOUT_SECONDS = 240
# 旧版默认 15:45/16:10 早于数据发布，未被用户改过的内置任务升级时迁移到新默认时间。
# 旧版选股任务不推送飞书；仍是这段默认描述（没人改过）的任务在启动时补上任务群推送，只迁移一次。
_LEGACY_AUTOMATIC_SELECTION_DESCRIPTION = (
    "数据入库成功后运行确定性多因子选股、保存不可变证据并更新前瞻评估；只做研究，不自动交易。"
)
_LEGACY_AUTOMATIC_SELECTION_RUN_TIMES = {
    AUTOMATIC_SELECTION_DATA_JOB_KEY: "15:45",
    AUTOMATIC_SELECTION_JOB_KEY: "16:10",
}
_MANUAL_TRIGGER_TYPES = frozenset(("manual", "manual_pipeline"))

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


def _should_extract_report_tasks(
    *,
    extraction_mode: str,
    output_content: str,
    has_task_batch: bool,
) -> bool:
    content = str(output_content or "").strip()
    if not content:
        return False
    if extraction_mode == "legacy_report":
        return True
    if extraction_mode != "structured_tool" or has_task_batch:
        return False
    has_pending_action = "待完成" in content
    has_agent_assignment = any(
        marker in content
        for marker in ("负责 Agent", "负责人 Agent", "负责Agent", "负责人Agent")
    )
    return has_pending_action and has_agent_assignment


async def _enqueue_agent_tasks(task_ids: list[str]) -> None:
    from backend.app.agent_runtime.redis_broker import redis_broker

    for task_id in task_ids:
        await redis_broker.enqueue(task_id)

_CARD_TEMPLATE_BY_JOB = {
    "topnews-morning-0630": "wathet",
    "premarket-brief-0830": "wathet",
    "open-report-0935": "turquoise",
    "close-review-1530": "blue",
    "postmarket-research-1900": "indigo",
    "evening-daily-2200": "indigo",
    "weekend-review": "purple",
    "month-end-review": "orange",
    "automatic-selection-1610": "green",
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
        if not job.run_time:
            return False
        current_hhmm = now.strftime("%H:%M")
        allows_catchup = (
            schedule_type == "daily"
            and job.job_key in _SAME_DAY_CATCHUP_JOB_KEYS
        )
        if allows_catchup:
            if current_hhmm < job.run_time:
                return False
        elif current_hhmm != job.run_time:
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

    if (
        job.trading_day_only
        and job.job_key != AUTOMATIC_SELECTION_JOB_KEY
        and not is_trading_day(now.date())
    ):
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


def _scheduled_execution_constraints(job_key: str, plan_source: str | None = None) -> str:
    common = (
        "本轮是独立的定时任务，只处理提示中的当日范围；历史报告仅可作为证据，"
        "不得补做往日任务。工具已返回的数据直接复用，同一工具与同一参数不要重复调用，"
        "同一事实和计算不要在推理中反复核验；数据缺失时继续完成报告，缺了什么在文末集中说一次，"
        "并说明影响哪些操作。报告写完由系统自动推送飞书，你不用自己推送，正文也不提推送情况。"
        "任务里的核对要求和限制是给你做事用的，照做即可，不要在报告里逐条复述"
        "（如“不跨账户挪用”“不构成授权”“不冒充实时数据”）。"
    )
    if plan_source:
        common += PLAN_SAVE_INSTRUCTION
    if job_key == "close-review-1530":
        return (
            common
            + "先各调用一次持仓快照和止损检查等批量工具，优先使用快照中的最新价、市值、"
            "浮盈亏和仓位结果，禁止按账户或按标的重复手算组合总额。完成必要查询后直接成稿，"
            "正文控制在 1600 字以内。"
        )
    if job_key == "postmarket-research-1900":
        return (
            common
            + "先一次取得完整持仓清单；每只标的优先使用公司研究聚合工具获取底稿，"
            "仅补查底稿明确缺失且会改变结论的维度。每只持仓只输出一份合并备忘录。"
        )
    return common


def _scheduled_agent_hard_timeout_seconds(snap: dict) -> int:
    """Cover leader work, delegated-child wait, and leader resume phases."""

    run_timeout = max(30, int(settings.agent_run_timeout_seconds))
    mode = str(snap.get("delegation_mode") or "none")
    child_timeout = (
        max(0, int(snap.get("child_timeout_seconds") or 0))
        if mode == "execute_and_wait"
        else 0
    )
    leader_phases = 2 if mode == "execute_and_wait" else 1
    return leader_phases * run_timeout + child_timeout + 60


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
                # 研究角色的提示词由角色段落统一注入（digital_human_skill_prompt），这里只给其他数字人带上
                *([] if is_role_profile(profile.code) else [f"提示词: {profile.system_prompt}"]),
            ]
        )


def _run_scheduled_agent(
    snap: dict,
    question: str,
    job_run_id: str,
    *,
    repair_timeout_seconds: int | None = None,
) -> tuple[str, str | None]:
    """跑一次定时 Agent。传 repair_timeout_seconds 表示这是报告定稿后的补存计划，不是主运行。"""
    is_repair = repair_timeout_seconds is not None
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
        # Keep one scheduler session as an audit container, but every fire is
        # an independent job.  Feeding old messages back into the model makes
        # failed daily prompts accumulate as a fake backlog (for example, a
        # close review starts trying to regenerate several previous dates) and
        # eventually sends the model into a reasoning loop.
        history: list[dict] = []
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
        delegation_config={
            "mode": str(snap.get("delegation_mode") or "none"),
            "extraction_mode": str(snap.get("task_extraction_mode") or "structured_tool"),
            "completion_policy": str(snap.get("completion_policy") or "all_required"),
            "child_timeout_seconds": int(snap.get("child_timeout_seconds") or 600),
            "max_delegated_tasks": int(snap.get("max_delegated_tasks") or 20),
        },
    )
    if snap.get("trade_plan_source"):
        request_payload["trade_plan_source"] = snap["trade_plan_source"]
        request_payload["trade_plan_context"] = {
            "job_key": snap["job_key"],
            "push_channel_key": snap.get("push_channel_key"),
        }
    idempotency_key = f"schedule:{job_run_id}:plan-repair" if is_repair else f"schedule:{job_run_id}"
    run = create_channel_run(request_payload, idempotency_key=idempotency_key)
    # 关联 leader AgentRun 到 JobRun，便于任务提取和审计追溯
    if not is_repair:
        with SessionLocal() as db:
            job_run = db.scalar(
                select(JobRun).where(JobRun.run_id == job_run_id).limit(1)
            )
            if job_run is not None:
                job_run.leader_agent_run_id = run["run_id"]
                db.commit()
    if run["created"]:
        with SessionLocal() as db:
            db.add(AiChatMessage(session_id=session_id, role="user", content=question, event_count=0))
            db.commit()
    answer, model_name, error = wait_for_channel_run(
        run["run_id"],
        timeout_seconds=(
            repair_timeout_seconds if is_repair else max(30, int(settings.agent_run_timeout_seconds)) + 30
        ),
        hard_timeout_seconds=(
            repair_timeout_seconds if is_repair else _scheduled_agent_hard_timeout_seconds(snap)
        ),
    )
    if error:
        raise RuntimeError(error)
    if not answer:
        raise RuntimeError("The scheduled Agent did not return a valid answer.")
    return answer, model_name


def _plain_report(snap: dict, answer: str) -> str:
    """推送前改成白话；改稿没做成或没过数字核对就沿用原文（原文仍在定时任务会话记录里）。"""
    result = rewrite_plain(answer)
    if result.applied:
        _log(
            "INFO",
            "定时报告已改成白话",
            {"job_key": snap["job_key"], "chars_before": len(answer), "chars_after": len(result.text)},
        )
    elif result.reason:
        _log("WARNING", f"定时报告沿用原文：{result.reason}", {"job_key": snap["job_key"]})
    return result.text


def _plan_reconciliation_block(snap: dict, now: datetime) -> str:
    """晚间日报的“计划和实际对照”直接用系统算好的结果，不用数字人再去翻文件比对。"""
    owner_user_id = snap.get("owner_user_id")
    if not owner_user_id:
        return ""
    try:
        with SessionLocal() as session:
            return reconciliation_text(session, int(owner_user_id), now.date().isoformat())
    except Exception as exc:  # noqa: BLE001 - 对账结果只是素材，拿不到时数字人还能按原来的办法写
        _log("WARNING", "计划对账结果生成失败", {"error": str(exc)[:300]})
        return ""


def _current_plan_meta(snap: dict, now: datetime) -> dict[str, Any]:
    owner_user_id = snap.get("owner_user_id")
    if not owner_user_id:
        return {}
    try:
        with SessionLocal() as session:
            return current_plan_meta(session, int(owner_user_id), now.date().isoformat())
    except Exception as exc:  # noqa: BLE001 - 手机页上的计划入口只是附加内容
        _log("WARNING", "读取今天的计划入口失败", {"error": str(exc)[:300]})
        return {}


def _finish_trade_plan(
    snap: dict,
    answer: str,
    now: datetime,
    job_run_id: str,
    report_id: str,
) -> ActivationResult:
    """报告定稿后：计划和正文对不上就让数字人补存一次，然后让计划生效。

    补存有时限，不能拖住报告；这里任何失败都不影响报告送达。
    """
    try:
        with SessionLocal() as session:
            problem = repair_request(session, job_run_id, answer)
        if problem:
            _log("WARNING", "计划和正文对不上，让数字人补存一次", {"job_key": snap["job_key"], "problem": problem[:300]})
            try:
                _run_scheduled_agent(
                    {**snap, "delegation_mode": "none"},
                    repair_question(problem, str(snap["name"]), answer),
                    job_run_id,
                    repair_timeout_seconds=PLAN_REPAIR_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - 补存失败就按现状生效，并在报告末尾注明
                _log("WARNING", "计划补存失败", {"job_key": snap["job_key"], "error": str(exc)[:300]})
        with SessionLocal() as session:
            return activate_for_report(
                session,
                job_run_id=job_run_id,
                report_id=report_id,
                text=answer,
                generated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
                today=now.date().isoformat(),
                session_live=is_trading_day(now.date()) and now.time() < MARKET_CLOSE,
            )
    except Exception as exc:  # noqa: BLE001
        _log("ERROR", "计划生效失败", {"job_key": snap["job_key"], "error": str(exc)[:300]})
        return ActivationResult(note="这份清单的计划没能生效，这次不会有到价提醒。")


def _run_agent_report(
    snap: dict,
    now: datetime,
    job_run_id: str,
) -> tuple[str, int | None, str, str | None]:
    plan_source = plan_source_for_job(snap.get("job_key"), snap.get("name"))
    if plan_source:
        snap = {**snap, "trade_plan_source": plan_source}
    question = _render(snap["prompt_template"], now) or snap["name"]
    question = f"{question}\n\n执行约束：{_scheduled_execution_constraints(str(snap['job_key']), plan_source)}"
    if plan_source == "evening":
        reconciliation = _plan_reconciliation_block(snap, now)
        if reconciliation:
            question = f"{question}\n\n{reconciliation}"
    answer, model_name = _run_scheduled_agent(snap, question, job_run_id)
    agent_error = None
    if agent_error:
        raise RuntimeError(agent_error)
    if not answer:
        raise RuntimeError("模型未返回有效内容")
    answer = _plain_report(snap, answer)

    report_id = f"rhythm-{job_run_id}"
    plan_meta: dict[str, Any] = {}
    if plan_source:
        plan_result = _finish_trade_plan(snap, answer, now, job_run_id, report_id)
        if plan_result.plan_url:
            plan_meta = {"plan_url": plan_result.plan_url, "plan_stats": plan_result.stats}
        if plan_result.note:
            answer = f"{answer}\n\n---\n\n{plan_result.note}"
    else:
        plan_meta = _current_plan_meta(snap, now)
    agent_name = DH_TO_AGENT.get(snap["digital_human_code"] or "", "pm")
    title = f"{snap['name']} | {now:%Y-%m-%d}"
    notification_id: int | None = None
    notification_error: str | None = None
    with SessionLocal() as session:
        profile = session.scalar(
            select(DigitalHumanProfile).where(DigitalHumanProfile.code == snap["digital_human_code"]).limit(1)
        )
        report = session.scalar(
            select(AgentReport).where(AgentReport.report_id == report_id).limit(1)
        )
        if report is None:
            report = AgentReport(
                owner_user_id=snap.get("owner_user_id"),
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
            if report.owner_user_id != snap.get("owner_user_id"):
                raise RuntimeError("Scheduled report ownership mismatch.")
            report.owner_user_id = snap.get("owner_user_id")
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
            digital_human_name = profile.name if profile else (snap["digital_human_code"] or "系统")
            notification = send_feishu(
                session,
                snap["push_channel_key"],
                title,
                answer,
                report_id=report_id,
                card_template=_CARD_TEMPLATE_BY_JOB.get(snap["job_key"], "blue"),
                card_metadata=[
                    ("任务", snap["name"]),
                    ("数字人", digital_human_name),
                    ("触发方式", trigger_label),
                    ("生成时间", now.strftime("%Y-%m-%d %H:%M:%S")),
                ],
                card_footer="SixRabbits Stock | 自动生成，仅供研究与复盘，不构成投资建议",
                share=ShareSpec(
                    source_type="agent_report",
                    source_ref=report_id,
                    owner_user_id=snap.get("owner_user_id"),
                    meta={
                        "digital_human": digital_human_name,
                        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                        **plan_meta,
                    },
                ),
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
            share=ShareSpec(
                source_type="topnews_brief",
                owner_user_id=snapshot.get("owner_user_id"),
                meta={"generated_at": ran_at.strftime("%Y-%m-%d %H:%M:%S")},
            ),
        )
    return report


def _run_health_check(
    now: datetime,
    push_channel_key: str | None,
    job_run_id: str,
) -> tuple[str, int | None, str | None]:
    # 调度器/心跳使用北京时间，数据库 created_at 使用 UTC 裸时间。
    # 无时区输入沿用调度器语义；带时区输入先换算，避免查询窗口少算 8 小时。
    local_now = now.replace(tzinfo=_SHANGHAI) if now.tzinfo is None else now.astimezone(_SHANGHAI)
    utc_now = local_now.astimezone(timezone.utc).replace(tzinfo=None)
    now = local_now.replace(tzinfo=None)
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
                .where(
                    SystemLog.level == "ERROR",
                    SystemLog.created_at >= utc_now - timedelta(hours=12),
                    SystemLog.created_at <= utc_now,
                )
            )
            or 0
        )
        if error_logs:
            problems.append(f"近 12 小时有 {error_logs} 条 ERROR 日志")
        failed_runs = (
            session.scalar(
                select(func.count())
                .select_from(JobRun)
                .where(
                    JobRun.status == "failed",
                    JobRun.created_at >= utc_now - timedelta(hours=24),
                    JobRun.created_at <= utc_now,
                )
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


def sync_position_prices(now: datetime | None = None) -> dict[str, Any]:
    """「更新价格」按钮与定时任务共享的同步入口。

    线程锁防同进程并发；_claim_manual_run 占用 job_runs 运行记录，与定时任务
    的认领检查互为互斥（跨进程/跨副本也不会双跑）。任务行被删除时降级为
    不落运行记录直接同步。返回 {"skipped", "status", "message"}。
    """
    ran_at = now or now_sh()
    if not _position_price_sync_lock.acquire(blocking=False):
        return {
            "skipped": True,
            "status": "skipped",
            "message": "上一轮持仓价格同步仍在执行，请稍后再试",
        }
    try:
        claim = _claim_manual_run(
            POSITION_PRICE_SYNC_JOB_KEY,
            expected_job_kind="position_price_sync",
        )
        claim_status = str(claim.get("status") or "")
        if claim_status == "already_running":
            return {
                "skipped": True,
                "status": "skipped",
                "message": "定时任务正在同步持仓价格，请稍候刷新页面",
            }
        if claim_status == "invalid_kind":
            return {
                "skipped": True,
                "status": "failed",
                "message": "持仓价格同步任务类型异常，请联系管理员",
            }
        if claim_status not in {"claimed", "missing"}:
            return {
                "skipped": True,
                "status": "failed",
                "message": "持仓价格同步任务认领失败，请稍后重试",
            }
        run_id = (
            str(claim["run_id"])
            if claim_status == "claimed" and claim.get("run_id")
            else None
        )
        if claim_status == "claimed" and run_id is None:
            return {
                "skipped": True,
                "status": "failed",
                "message": "持仓价格同步任务认领失败，请稍后重试",
            }
        return _sync_prices_and_finalize(ran_at, run_id=run_id)
    finally:
        _position_price_sync_lock.release()


def _sync_prices_and_finalize(ran_at: datetime, *, run_id: str | None) -> dict[str, Any]:
    started = time.monotonic()
    status = "success"
    error: str | None = None
    message: str | None = None
    try:
        message = _run_position_price_sync_locked(ran_at)
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        error = str(exc)[:1000]
        _log("ERROR", "手动持仓价格同步失败", {"error": error})
    duration_ms = int((time.monotonic() - started) * 1000)
    if run_id is not None:
        _finalize_run(run_id, status, output_content=message, error=error, duration_ms=duration_ms)
        _update_job_status(POSITION_PRICE_SYNC_JOB_KEY, status, error, ran_at)
    return {
        "skipped": False,
        "status": status,
        "message": message if status == "success" else (error or "持仓价格同步失败"),
    }


def _run_position_price_sync_locked(now: datetime) -> str:
    """规则任务（不经 LLM）：批量同步全部 open 持仓和活跃自选行情。

    持仓会重算市值/浮盈与账户资金；自选只刷新共享行情缓存。交易时段每分钟调用一次
    TuShare rt_k 批量接口；外部请求期间不持有数据库连接。
    整批请求失败时保留上一轮价格并令任务失败，避免用空值覆盖有效行情。
    """
    from backend.app.core.models import (
        Instrument,
        Position,
        PositionAccount,
        PositionHoldingSnapshot,
        WatchlistItem,
    )
    from backend.app.data.repository import StockRepository
    from backend.app.market.stock_tools import get_realtime_quotes

    # 会话一（短）：确定同步对象并提交。绝不带着未提交事务去做外部 HTTP——
    # 补建快照的 FK 会给 instruments 父行持共享锁，而行情工具的缓存写入要更新同一行，
    # 长事务会把对方锁到超时（模块纪律：外部调用期间不持有数据库连接）。
    with SessionLocal() as session:
        latest_ids = (
            select(func.max(PositionHoldingSnapshot.id))
            .group_by(
                PositionHoldingSnapshot.owner_user_id,
                PositionHoldingSnapshot.account_id,
                PositionHoldingSnapshot.symbol,
            )
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
        covered = {(s.owner_user_id, s.account_id, s.symbol) for s in snaps}
        orphan_positions = [
            p
            for p in session.scalars(select(Position).where(Position.status == "open")).all()
            if (p.quantity or 0) > 0 and (p.owner_user_id, p.account_id, p.symbol) not in covered
        ]
        for pos in orphan_positions:
            instrument = session.get(Instrument, pos.symbol)
            snap = PositionHoldingSnapshot(
                owner_user_id=pos.owner_user_id,
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
        watchlist_symbols = set(
            session.scalars(
                select(WatchlistItem.symbol).where(WatchlistItem.status == "active")
            ).all()
        )
        symbols = {s.symbol for s in snaps} | watchlist_symbols
        session.commit()

    symbols = sorted(symbols | _plan_watch_symbols(now))
    if not symbols:
        return "无 open 持仓或自选标的，跳过价格同步"

    # 无会话阶段：一次批量拉取全部持仓与自选行情，不占用数据库连接。
    batch = get_realtime_quotes(symbols)
    if not isinstance(batch, dict) or batch.get("error"):
        reason = batch.get("error") if isinstance(batch, dict) else "返回格式错误"
        raise RuntimeError(f"实时行情同步失败: {reason}")
    quotes = batch.get("quotes") or {}
    failures = batch.get("missing_symbols") or []
    if not quotes:
        raise RuntimeError("实时行情同步失败: TuShare rt_k 未返回任何有效持仓或自选行情")

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
        f"实时同步 {len(quotes)} 个持仓/自选标的行情，更新 {updated} 条持仓快照，"
        f"重算 {len(touched_accounts)} 个账户资金；来源 {batch.get('source')}，"
        f"耗时 {batch.get('ms', 0)}ms"
    )
    if failures:
        preview += f"；{len(failures)} 个标的取价失败: {', '.join(failures[:8])}"
    return preview + _check_trade_plans(quotes, now)


def _plan_watch_symbols(now: datetime) -> set[str]:
    try:
        from backend.app.plans.watcher import watch_symbols

        return watch_symbols(now.date().isoformat())
    except Exception as exc:  # noqa: BLE001 - 盯价出问题不能影响价格同步
        _log("WARNING", "读取计划盯价代码失败", {"error": str(exc)[:300]})
        return set()


def _check_trade_plans(quotes: dict[str, Any], now: datetime) -> str:
    """价格刷新完顺带查计划价，不调模型。只在开盘时段查，手动更新价格时也一样。"""
    if not _position_price_sync_session_open(now) or not is_trading_day(now.date()):
        return ""
    try:
        from backend.app.plans.watcher import check_plan_triggers

        result = check_plan_triggers(quotes, now)
    except Exception as exc:  # noqa: BLE001 - 盯价出问题不能影响价格同步
        _log("ERROR", "计划盯价失败", {"error": str(exc)[:300]})
        return "；计划盯价失败"
    return f"；{result['triggered']} 条计划碰到计划价" if result["triggered"] else ""


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
        "delegation_mode": getattr(job, "delegation_mode", "none"),
        "task_extraction_mode": getattr(job, "task_extraction_mode", "structured_tool"),
        "completion_policy": getattr(job, "completion_policy", "all_required"),
        "child_timeout_seconds": getattr(job, "child_timeout_seconds", 600),
        "max_delegated_tasks": getattr(job, "max_delegated_tasks", 20),
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


def _run_automatic_selection_data(
    snapshot: dict[str, Any],
    run_id: str,
) -> tuple[str, int | None, str, str | None]:
    from backend.app.notify.notification_service import create_notification
    from backend.app.quant.automatic_selection_service import (
        sync_automatic_selection_data,
    )

    result = sync_automatic_selection_data()
    factor_sync = result.get("factor_sync") or {}
    market_sync = result.get("market_sync") or {}
    summary = {
        "target_trade_date": result.get("target_trade_date"),
        "ready": bool(result.get("ready")),
        "strategy_count": int(result.get("strategy_count") or 0),
        "bar_rows_upserted": int(
            market_sync.get("bars_upserted") or 0
        ),
        "basic_rows_upserted": int(
            market_sync.get("basic_upserted") or 0
        ),
        "calendar_rows_upserted": int(
            market_sync.get("calendar_rows_upserted") or 0
        ),
        "factor_rows_stored": int(
            factor_sync.get("stored_rows") or 0
        ),
        "errors": [str(item) for item in result.get("errors") or []],
    }
    output_content = json.dumps(
        summary,
        ensure_ascii=False,
        sort_keys=True,
    )
    if summary["ready"]:
        return output_content, None, "success", None

    error = "；".join(summary["errors"])[:1000] or "自动选股数据入库未就绪"
    owner_user_id = snapshot.get("owner_user_id")
    # 定时任务失败会每 30 分钟自动重试；只在当天首次失败时提醒，避免重试刷屏。
    if owner_user_id is None or "~retry" in str(snapshot.get("fire_key") or ""):
        return output_content, None, "failed", error
    retry_note = (
        f"系统将每 {AUTOMATIC_SELECTION_DATA_RETRY_MINUTES} 分钟自动重试，"
        f"直到 {AUTOMATIC_SELECTION_DATA_RETRY_UNTIL}；"
        if snapshot.get("trigger_type") == "schedule"
        else ""
    )
    notification = create_notification(
        int(owner_user_id),
        "自动选股数据入库失败",
        content=(
            f"目标交易日 {summary['target_trade_date'] or '未知'}；"
            f"{retry_note}{error}"
        ),
        level="error",
        category="automatic_selection",
        link="/automatic-selection",
    )
    return output_content, int(notification["id"]), "failed", error


def _run_automatic_selection(
    snapshot: dict[str, Any],
    run_id: str,
) -> tuple[str, int | None, str, str | None]:
    from backend.app.notify.notification_service import create_notification
    from backend.app.quant.automatic_selection_service import run_automatic_selection_cycle

    result = run_automatic_selection_cycle(
        trigger_type=str(snapshot.get("trigger_type") or "scheduled"),
        job_run_id=run_id,
    )
    evaluations = result.get("evaluation_updates") or {}
    summary = {
        "target_trade_date": result.get("target_trade_date"),
        "strategy_count": int(result.get("strategy_count") or 0),
        "completed_count": int(result.get("completed_count") or 0),
        "blocked_count": int(result.get("blocked_count") or 0),
        "failed_count": int(result.get("failed_count") or 0),
        "selected_count": int(result.get("selected_count") or 0),
        "evaluation_updates": {
            "completed": int(evaluations.get("completed") or 0),
            "insufficient_data": int(evaluations.get("insufficient_data") or 0),
            "pending": int(evaluations.get("pending") or 0),
            "failed": int(evaluations.get("failed") or 0),
        },
    }
    output_content = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    task_status = "success"
    task_error: str | None = None
    if summary["failed_count"] > 0:
        task_status = "failed"
        task_error = f"{summary['failed_count']} 个自动选股策略执行失败"
    elif summary["evaluation_updates"]["failed"] > 0:
        task_status = "failed"
        task_error = (
            f"{summary['evaluation_updates']['failed']} 个前瞻评估执行失败，已保留等待重试"
        )
    elif (
        summary["strategy_count"] > 0
        and summary["completed_count"] == 0
        and summary["blocked_count"] == summary["strategy_count"]
    ):
        task_status = "skipped"
        task_error = "所有启用策略均被质量门禁阻断"

    has_warning = (
        task_status != "success"
        or summary["blocked_count"] > 0
        or summary["failed_count"] > 0
    )
    digest = _automatic_selection_digest(summary)
    if digest is not None and snapshot.get("push_channel_key"):
        push_error = _push_automatic_selection_digest(snapshot, digest)
        if push_error and task_status == "success":
            task_status = "failed"
            task_error = f"选股已完成，但飞书任务群推送失败: {push_error}"
    owner_user_id = snapshot.get("owner_user_id")
    if owner_user_id is None:
        return output_content, None, task_status, task_error
    # 站内信发选股结果；运行状态只在出问题或没有结果可发时单独提醒。
    notification_id: int | None = None
    if digest is not None:
        notification = create_notification(
            int(owner_user_id),
            digest["inbox_title"],
            content=digest["content"],
            level="success",
            category="automatic_selection",
            link="/automatic-selection",
        )
        notification_id = int(notification["id"])
    if has_warning or digest is None:
        notification = create_notification(
            int(owner_user_id),
            "自动选股需要关注" if has_warning else "自动选股运行完成",
            content=_automatic_selection_status_text(summary),
            level="error" if task_status == "failed" else ("warning" if has_warning else "success"),
            category="automatic_selection",
            link="/automatic-selection",
        )
        notification_id = int(notification["id"])
    return output_content, notification_id, task_status, task_error


def _automatic_selection_digest(summary: dict[str, Any]) -> dict[str, Any] | None:
    if not summary["completed_count"] or not summary["target_trade_date"]:
        return None
    from backend.app.quant.automatic_selection_presentation import daily_digest

    try:
        return daily_digest(trade_date=str(summary["target_trade_date"]))
    except Exception as exc:  # noqa: BLE001 - 摘要只是通知内容，生成失败不影响选股结果
        _log("WARNING", "每日选股摘要生成失败", {"error": str(exc)[:300]})
        return None


def _push_automatic_selection_digest(snapshot: dict[str, Any], digest: dict[str, Any]) -> str | None:
    now = now_sh()
    trigger_label = "自动调度" if snapshot.get("trigger_type") == "schedule" else "手动执行"
    with SessionLocal() as session:
        notification = send_feishu(
            session,
            snapshot["push_channel_key"],
            digest["title"],
            digest["content"],
            event_id=f"automatic-selection:{digest['run_id']}" if digest.get("run_id") else None,
            card_template=_CARD_TEMPLATE_BY_JOB.get(str(snapshot.get("job_key")), "green"),
            card_metadata=[
                ("任务", str(snapshot.get("name") or "全自动选股")),
                ("触发方式", trigger_label),
                ("生成时间", now.strftime("%Y-%m-%d %H:%M:%S")),
            ],
            card_footer="SixRabbits Stock | 规则选股，仅供研究与复盘，不构成投资建议",
            share=ShareSpec(
                source_type="selection_digest",
                source_ref=str(digest["run_id"]) if digest.get("run_id") else None,
                owner_user_id=snapshot.get("owner_user_id"),
                meta={"generated_at": now.strftime("%Y-%m-%d %H:%M:%S")},
            ),
        )
        if notification.status != "sent":
            return notification.error or "飞书未确认发送成功"
    return None


def _automatic_selection_status_text(summary: dict[str, Any]) -> str:
    parts = [f"目标交易日 {summary['target_trade_date'] or '未知'}"]
    if summary["completed_count"]:
        parts.append(f"{summary['completed_count']} 个策略出了结果")
    if summary["blocked_count"]:
        parts.append(f"{summary['blocked_count']} 个策略因数据不全暂停（不会拿残缺数据硬算）")
    if summary["failed_count"]:
        parts.append(f"{summary['failed_count']} 个策略计算出错")
    if summary["evaluation_updates"]["failed"]:
        parts.append(f"{summary['evaluation_updates']['failed']} 个历史战绩没算出来，会自动重试")
    if not summary["strategy_count"]:
        parts.append("没有启用的选股策略")
    return "；".join(parts) + "。详情见「自动选股」页面。"


def _run_manual_automatic_selection_followup(
    snapshot: dict[str, Any],
    status: str,
) -> dict[str, Any] | None:
    if (
        snapshot.get("job_kind") != "automatic_selection_data"
        or snapshot.get("trigger_type") != "manual_pipeline"
        or status != "success"
    ):
        return None
    result = _claim_manual_run(
        AUTOMATIC_SELECTION_JOB_KEY,
        actor_user_id=snapshot.get("owner_user_id"),
        expected_job_kind="automatic_selection",
    )
    run_id = result.get("run_id")
    if result.get("status") == "claimed" and run_id:
        _execute_job(AUTOMATIC_SELECTION_JOB_KEY, str(run_id))
    return result


def _execute_job(job_key: str, run_id: str) -> None:
    """同步执行一个任务，在线程中被调用；绝不抛出，所有结果落 job_runs。"""
    started = time.monotonic()
    ran_at = now_sh()
    with SessionLocal() as session:
        job = session.scalar(select(ScheduledJob).where(ScheduledJob.job_key == job_key).limit(1))
        snapshot = _job_snapshot(job) if job else None
        run = session.scalar(select(JobRun).where(JobRun.run_id == run_id).limit(1))
        if snapshot is not None:
            snapshot["trigger_type"] = (
                run.trigger_type
                if run
                else "schedule"
            )
            if run is not None:
                snapshot["owner_user_id"] = run.owner_user_id
                snapshot["fire_key"] = run.fire_key
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
        elif snapshot["job_kind"] == "automatic_selection_data":
            (
                output_content,
                notification_id,
                status,
                error,
            ) = _run_automatic_selection_data(snapshot, run_id)
        elif snapshot["job_kind"] == "automatic_selection":
            (
                output_content,
                notification_id,
                status,
                error,
            ) = _run_automatic_selection(snapshot, run_id)
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
            else:
                extraction_mode = str(
                    snapshot.get("task_extraction_mode") or "structured_tool"
                )
                leader_run_id = None
                leader_user_id = None
                has_task_batch = False
                with SessionLocal() as db:
                    jr = db.scalar(
                        select(JobRun).where(JobRun.run_id == run_id).limit(1)
                    )
                    leader_run_id = jr.leader_agent_run_id if jr else None
                    if leader_run_id:
                        leader_run = db.get(AgentRun, leader_run_id)
                        leader_user_id = leader_run.user_id if leader_run else None
                        has_task_batch = db.scalar(
                            select(AgentTeamRun.team_id)
                            .where(AgentTeamRun.leader_run_id == leader_run_id)
                            .limit(1)
                        ) is not None

            if not push_error and _should_extract_report_tasks(
                extraction_mode=extraction_mode,
                output_content=output_content or "",
                has_task_batch=has_task_batch,
            ):
                try:
                    from backend.app.quant.task_extractor import ScheduledTaskExtractor

                    extractor = ScheduledTaskExtractor()
                    plan = extractor.extract(output_content, job_run_id=run_id)
                    if plan.get("has_tasks") and plan.get("confidence", 0) >= 0.8:
                        from backend.app.quant.agent_board_service import (
                            create_task_batch,
                        )

                        if leader_run_id:
                            batch = extractor.to_task_batch(
                                plan,
                                objective=f"定时任务 [{snapshot['name']}] 产出协同任务",
                                completion_policy=str(snapshot.get("completion_policy") or "all_required"),
                                child_timeout_seconds=int(snapshot.get("child_timeout_seconds") or 600),
                            )
                            board_result = create_task_batch(
                                batch,
                                leader_run_id=leader_run_id,
                                user_id=leader_user_id or snapshot.get("owner_user_id"),
                                job_run_id=run_id,
                                skip_leader_validation=True,
                            )
                            with SessionLocal() as db:
                                jr = db.scalar(
                                    select(JobRun)
                                    .where(JobRun.run_id == run_id)
                                    .limit(1)
                                )
                                if jr is not None:
                                    jr.orchestration_type = "task_batch"
                                    jr.orchestration_id = board_result["team_id"]
                                    jr.handoff_status = "created"
                                    db.commit()
                            asyncio.run(_enqueue_agent_tasks(board_result["task_ids"]))
                            _log(
                                "INFO",
                                f"定时任务 {snapshot['job_key']} 提取了 {len(board_result['task_ids'])} 个协同任务",
                                {"confidence": plan.get("confidence"), "team_id": board_result["team_id"]},
                            )
                except Exception as extract_exc:  # noqa: BLE001
                    _log("WARNING", f"定时任务 {snapshot['job_key']} 协同任务提取失败", {"error": str(extract_exc)[:500]})
        elif snapshot["job_kind"] == "topnews_brief":
            output_content = _run_topnews_brief(snapshot, ran_at)
        elif snapshot["job_kind"] == "news_ingest":
            from backend.app.data.tushare_news_ingest import run_news_ingest

            output_content = run_news_ingest(snapshot["job_key"])
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
    try:
        followup = _run_manual_automatic_selection_followup(snapshot, status)
        if followup is not None and followup.get("status") != "claimed":
            _log(
                "ERROR",
                "自动选股数据入库成功，但后续排名任务未能认领",
                {"status": followup.get("status"), "run_id": followup.get("run_id")},
            )
    except Exception as exc:  # noqa: BLE001
        _log(
            "ERROR",
            "自动选股数据入库成功，但后续排名任务派发失败",
            {"error": str(exc)[:500]},
        )


# --------------------------------------------------------------------------- #
# 认领与派发
# --------------------------------------------------------------------------- #
def _write_scheduler_heartbeat(now: datetime, detail: dict) -> None:
    with SessionLocal() as session:
        _upsert_heartbeat(session, HEARTBEAT_COMPONENT, "ok", now, detail)
        session.commit()


def _max_concurrent_jobs() -> int:
    return max(1, settings.scheduler_max_concurrent_jobs)


def _claim_group_job_keys(job_key: str) -> tuple[str, ...]:
    if job_key in _AUTOMATIC_SELECTION_CLAIM_GROUP:
        return _AUTOMATIC_SELECTION_CLAIM_GROUP
    return (job_key,)


def _lock_claim_group(session, job_key: str) -> dict[str, ScheduledJob]:
    """Lock every conflict-group row in one deterministic order."""
    keys = _claim_group_job_keys(job_key)
    rows = session.scalars(
        select(ScheduledJob)
        .where(ScheduledJob.job_key.in_(keys))
        .order_by(ScheduledJob.job_key)
        .with_for_update()
    ).all()
    return {row.job_key: row for row in rows}


def _find_running_claim(
    session,
    job_key: str,
    *,
    started_after: datetime | None = None,
) -> JobRun | None:
    stmt = select(JobRun).where(
        JobRun.job_key.in_(_claim_group_job_keys(job_key)),
        JobRun.status == "running",
    )
    if started_after is not None:
        stmt = stmt.where(JobRun.started_at >= started_after)
    return session.scalar(
        stmt.order_by(
            JobRun.job_key,
            JobRun.started_at,
            JobRun.id,
        ).limit(1).with_for_update()
    )


def _hhmm_minutes(value: str) -> int:
    hour, minute = value.split(":")[:2]
    return int(hour) * 60 + int(minute)


def _scheduled_fire_key(job: ScheduledJob, now: datetime) -> str:
    if (
        job.schedule_type == "daily"
        and job.job_key in _SAME_DAY_CATCHUP_JOB_KEYS
        and job.run_time
    ):
        fire_minute = f"{now:%Y-%m-%d}T{job.run_time}"
        if job.job_key == AUTOMATIC_SELECTION_DATA_JOB_KEY:
            # 每个 30 分钟槽位最多一次；超过重试截止时间后槽位不再增长，当天不再重试。
            elapsed = min(
                _hhmm_minutes(now.strftime("%H:%M")),
                _hhmm_minutes(AUTOMATIC_SELECTION_DATA_RETRY_UNTIL),
            ) - _hhmm_minutes(job.run_time)
            slot = max(0, elapsed) // AUTOMATIC_SELECTION_DATA_RETRY_MINUTES
            if slot:
                fire_minute += f"~retry{slot}"
    else:
        fire_minute = now.strftime("%Y-%m-%dT%H:%M")
    return f"{job.job_key}@{fire_minute}"


def _scheduled_automatic_selection_target(session, now: datetime) -> str | None:
    """定时选股的目标交易日：今天须为本地日历开市日；17:00 前用前一开市日。"""
    from backend.app.market.market_sync import selection_target_ceiling

    today = now.strftime("%Y%m%d")
    local_open_date = session.scalar(
        select(MarketTradingCalendar.cal_date)
        .where(
            MarketTradingCalendar.exchange == "SSE",
            MarketTradingCalendar.cal_date == today,
            MarketTradingCalendar.is_open.is_(True),
        )
        .limit(1)
    )
    if str(local_open_date or "") != today:
        return None
    ceiling = selection_target_ceiling(now)
    if ceiling >= today:
        return today
    previous = session.scalar(
        select(func.max(MarketTradingCalendar.cal_date)).where(
            MarketTradingCalendar.exchange == "SSE",
            MarketTradingCalendar.is_open.is_(True),
            MarketTradingCalendar.cal_date <= ceiling,
        )
    )
    return str(previous) if previous else None


def _scheduled_automatic_selection_data_ready(session, now: datetime) -> bool:
    """Use only local evidence to gate the scheduled ranking run."""
    expected_trade_date = _scheduled_automatic_selection_target(session, now)
    if not expected_trade_date:
        return False
    return successful_automatic_selection_ingestion(session, expected_trade_date) is not None


def successful_automatic_selection_ingestion(session, target_trade_date: str) -> JobRun | None:
    # A newer failed ingestion may have partially changed the cache; an older
    # success must not certify that newer data.
    latest = session.scalar(
        select(JobRun)
        .where(JobRun.job_key == AUTOMATIC_SELECTION_DATA_JOB_KEY)
        .order_by(JobRun.started_at.desc(), JobRun.id.desc())
        .limit(1)
    )
    if latest is None or latest.status != "success":
        return None
    try:
        output = json.loads(latest.output_content or "{}")
    except (TypeError, ValueError):
        return None
    if (
        isinstance(output, dict)
        and output.get("ready") is True
        and output.get("target_trade_date") == target_trade_date
    ):
        return latest
    return None


def _claim_job_run(
    session,
    job_key: str,
    *,
    trigger_type: str,
    ran_at: datetime,
    actor_user_id: int | None = None,
    expected_job_kind: str | None = None,
) -> dict[str, Any]:
    locked_jobs = _lock_claim_group(session, job_key)
    job = locked_jobs.get(job_key)
    if job is None:
        session.rollback()
        return {"status": "missing", "run_id": None}
    if expected_job_kind and job.job_kind != expected_job_kind:
        session.rollback()
        return {"status": "invalid_kind", "run_id": None}
    if trigger_type == "schedule" and (
        not job.enabled or not _is_due(job, ran_at)
    ):
        session.rollback()
        return {"status": "not_due", "run_id": None}

    manual_trigger = trigger_type in _MANUAL_TRIGGER_TYPES
    checks_running = (
        manual_trigger
        or job_key in _AUTOMATIC_SELECTION_CLAIM_GROUP
        or job.job_kind == "position_price_sync"
    )
    if checks_running:
        started_after = (
            ran_at - timedelta(minutes=2)
            if trigger_type == "schedule"
            and job.job_kind == "position_price_sync"
            else None
        )
        running = _find_running_claim(
            session,
            job_key,
            started_after=started_after,
        )
        if running is not None:
            run_id = str(running.run_id) if running.run_id else None
            session.rollback()
            return {"status": "already_running", "run_id": run_id}

    if (
        trigger_type == "schedule"
        and job_key == AUTOMATIC_SELECTION_JOB_KEY
        and not _scheduled_automatic_selection_data_ready(session, ran_at)
    ):
        session.rollback()
        return {"status": "waiting_dependency", "run_id": None}
    if (
        trigger_type == "schedule"
        and job_key == AUTOMATIC_SELECTION_DATA_JOB_KEY
        and _scheduled_automatic_selection_data_ready(session, ran_at)
    ):
        # 当前目标日已入库成功（含手动触发），重试槽位不再重复同步。
        session.rollback()
        return {"status": "not_due", "run_id": None}

    run_id = uuid4().hex
    fire_key = (
        f"{job_key}@manual-{run_id[:8]}"
        if manual_trigger
        else _scheduled_fire_key(job, ran_at)
    )
    session.add(
        JobRun(
            run_id=run_id,
            job_key=job_key,
            owner_user_id=(
                actor_user_id
                if manual_trigger and actor_user_id is not None
                else job.owner_user_id
            ),
            fire_key=fire_key,
            trigger_type=trigger_type,
            status="running",
            started_at=ran_at,
        )
    )
    if trigger_type == "schedule" and job.schedule_type == "once":
        job.enabled = False
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return {"status": "duplicate", "run_id": None}
    return {"status": "claimed", "run_id": run_id}


def _claim_due_jobs(now: datetime, limit: int | None = None) -> list[tuple[str, str]]:
    if limit is not None and limit <= 0:
        return []
    claimed: list[tuple[str, str]] = []
    with SessionLocal() as session:
        job_keys = list(
            session.scalars(
                select(ScheduledJob.job_key)
                .where(ScheduledJob.enabled.is_(True))
                .order_by(ScheduledJob.sort_order, ScheduledJob.id)
            ).all()
        )
        # End the discovery transaction before taking per-group claim locks.
        session.commit()
        for job_key in job_keys:
            if limit is not None and len(claimed) >= limit:
                break
            result = _claim_job_run(
                session,
                str(job_key),
                trigger_type="schedule",
                ran_at=now,
            )
            run_id = result.get("run_id")
            if result.get("status") == "claimed" and run_id:
                claimed.append((str(job_key), str(run_id)))
    return claimed


def _claim_manual_run(
    job_key: str,
    *,
    actor_user_id: int | None = None,
    expected_job_kind: str | None = None,
    trigger_type: str = "manual",
) -> dict[str, Any]:
    if trigger_type not in _MANUAL_TRIGGER_TYPES:
        raise ValueError("手动任务触发类型无效")
    with SessionLocal() as session:
        return _claim_job_run(
            session,
            job_key,
            trigger_type=trigger_type,
            ran_at=now_sh(),
            actor_user_id=actor_user_id,
            expected_job_kind=expected_job_kind,
        )


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_max_concurrent_jobs())
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


async def dispatch_manual_run_result(
    job_key: str,
    *,
    actor_user_id: int | None = None,
    expected_job_kind: str | None = None,
) -> dict[str, Any]:
    result = await asyncio.to_thread(
        _claim_manual_run,
        job_key,
        actor_user_id=actor_user_id,
        expected_job_kind=expected_job_kind,
    )
    run_id = result.get("run_id")
    if result.get("status") == "claimed" and run_id:
        _dispatch(job_key, str(run_id))
    return result


async def dispatch_automatic_selection_pipeline_result(
    *,
    actor_user_id: int,
) -> dict[str, Any]:
    result = await asyncio.to_thread(
        _claim_manual_run,
        AUTOMATIC_SELECTION_DATA_JOB_KEY,
        actor_user_id=actor_user_id,
        expected_job_kind="automatic_selection_data",
        trigger_type="manual_pipeline",
    )
    run_id = result.get("run_id")
    if result.get("status") == "claimed" and run_id:
        _dispatch(AUTOMATIC_SELECTION_DATA_JOB_KEY, str(run_id))
    return {
        **result,
        "job_key": AUTOMATIC_SELECTION_DATA_JOB_KEY,
        "pipeline_stage": "data_ingestion",
    }


async def dispatch_manual_run(
    job_key: str,
    *,
    actor_user_id: int | None = None,
) -> str | None:
    """手动触发：无视交易日/时间窗，立即后台执行。"""
    result = await dispatch_manual_run_result(
        job_key,
        actor_user_id=actor_user_id,
    )
    run_id = result.get("run_id")
    return str(run_id) if run_id else None


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
    available_slots = max(0, _max_concurrent_jobs() - len(_inflight_runs))
    await asyncio.to_thread(
        _write_scheduler_heartbeat,
        now,
        {
            "tick_at": now.isoformat(),
            "inflight": len(_inflight_runs),
            "available_slots": available_slots,
            "max_concurrency": _max_concurrent_jobs(),
        },
    )
    if available_slots <= 0:
        return
    claimed = await asyncio.to_thread(_claim_due_jobs, now, available_slots)
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
        "job_key": POSITION_PRICE_SYNC_JOB_KEY,
        "name": "持仓价格分钟同步",
        "description": "交易时段每分钟刷新所有持仓的最新价、市值和盈亏，并重算账户资金。",
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
        "description": "收盘后把当天新增的文档和报告加进知识库，失败会自动重试。",
        "job_kind": "rag_ingest",
        "schedule_type": "daily",
        "run_time": "16:30",
        "weekday_mask": "1,2,3,4,5",
        "trading_day_only": True,
        "enabled": True,
    },
    {
        "job_key": "news-ingest-quick-15m",
        "name": "快讯入库(每15分)",
        "description": "每 15 分钟把新闻快讯存进知识库，重复的自动跳过。",
        "job_kind": "news_ingest",
        "schedule_type": "interval",
        "run_time": None,
        "interval_minutes": 15,
        "window_start": "00:00",
        "window_end": "23:59",
        "trading_day_only": False,
        "enabled": True,
    },
    {
        "job_key": "news-ingest-major-1h",
        "name": "要闻入库(每小时)",
        "description": "每小时把重要新闻存进知识库，重复的自动跳过。",
        "job_kind": "news_ingest",
        "schedule_type": "interval",
        "run_time": None,
        "interval_minutes": 60,
        "window_start": "00:00",
        "window_end": "23:59",
        "trading_day_only": False,
        "enabled": True,
    },
    {
        "job_key": "news-ingest-cctv-daily",
        "name": "联播入库(每日)",
        "description": "每天把新闻联播文字稿存进知识库。",
        "job_kind": "news_ingest",
        "schedule_type": "daily",
        "run_time": "21:30",
        "trading_day_only": False,
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
        "job_key": AUTOMATIC_SELECTION_DATA_JOB_KEY,
        "name": "自动选股数据入库",
        "description": "17:00 后同步全市场日线、基础指标、复权因子、股票目录、交易日历和策略所需因子；失败每 30 分钟重试至 22:00。",
        "job_kind": "automatic_selection_data",
        "schedule_type": "daily",
        "run_time": "17:30",
        "weekday_mask": "1,2,3,4,5",
        "trading_day_only": True,
        "enabled": True,
    },
    {
        "job_key": AUTOMATIC_SELECTION_JOB_KEY,
        "name": "全自动选股",
        "description": "数据入库成功后运行确定性多因子选股、保存不可变证据并更新前瞻评估，前 5 名推送任务群并写入站内信；只做研究，不自动交易。",
        "job_kind": "automatic_selection",
        "schedule_type": "daily",
        "run_time": "17:50",
        "weekday_mask": "1,2,3,4,5",
        "trading_day_only": True,
        "push_channel_key": "feishu-daily",
        "enabled": True,
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


def _default_jobs_owner_id(session) -> int | None:
    """Return the enabled super admin that owns built-in scheduled jobs."""
    owner_id = session.scalar(
        select(User.id)
        .where(
            User.username == "admin",
            User.role == "super_admin",
            User.is_enabled.is_(True),
        )
        .limit(1)
    )
    if owner_id is not None:
        return int(owner_id)
    owner_id = session.scalar(
        select(User.id)
        .where(
            User.role == "super_admin",
            User.is_enabled.is_(True),
        )
        .order_by(User.id)
        .limit(1)
    )
    return int(owner_id) if owner_id is not None else None


def _adopt_legacy_default_job_history(session, *, owner_user_id: int) -> None:
    """Migrate ownerless history created before per-user schedule isolation."""
    personal_default_keys = [
        item["job_key"]
        for item in DEFAULT_JOBS
        if item["job_key"] != POSITION_PRICE_SYNC_JOB_KEY
    ]
    report_ids = {
        report_id
        for report_id in session.scalars(
            select(JobRun.report_id).where(
                JobRun.job_key.in_(personal_default_keys),
                JobRun.owner_user_id.is_(None),
                JobRun.report_id.is_not(None),
            )
        ).all()
        if report_id
    }
    if report_ids:
        session.execute(
            update(AgentReport)
            .where(
                AgentReport.report_id.in_(report_ids),
                AgentReport.owner_user_id.is_(None),
            )
            .values(owner_user_id=owner_user_id)
        )
    session.execute(
        update(JobRun)
        .where(
            JobRun.job_key.in_(personal_default_keys),
            JobRun.owner_user_id.is_(None),
        )
        .values(owner_user_id=owner_user_id)
    )


def _normalize_shared_price_sync_history(session) -> None:
    """Keep the single infrastructure task and all of its runs globally owned."""
    # The price synchronizer is one global infrastructure job. It updates all
    # users' holdings in a single batch and must never be duplicated or owned
    # by one user's private task space.
    session.execute(
        update(JobRun)
        .where(JobRun.job_key == POSITION_PRICE_SYNC_JOB_KEY)
        .values(owner_user_id=None)
    )


def ensure_default_jobs(session) -> int:
    """补齐内置任务；基础设施型价格同步任务、以及仍停留在旧默认时间的自动选股任务会随版本迁移调度配置。"""
    created = 0
    owner_user_id = _default_jobs_owner_id(session)
    for item in DEFAULT_JOBS:
        item_owner_user_id = (
            None if item["job_key"] == POSITION_PRICE_SYNC_JOB_KEY else owner_user_id
        )
        existing = session.scalar(select(ScheduledJob).where(ScheduledJob.job_key == item["job_key"]).limit(1))
        if existing is not None:
            if existing.job_key == POSITION_PRICE_SYNC_JOB_KEY:
                existing.owner_user_id = None
            elif existing.owner_user_id is None and owner_user_id is not None:
                existing.owner_user_id = owner_user_id
            if (
                existing.job_key == AUTOMATIC_SELECTION_JOB_KEY
                and existing.push_channel_key is None
                and existing.description == _LEGACY_AUTOMATIC_SELECTION_DESCRIPTION
            ):
                existing.push_channel_key = item.get("push_channel_key")
                existing.description = item.get("description")
            legacy_time = _LEGACY_AUTOMATIC_SELECTION_RUN_TIMES.get(existing.job_key)
            if legacy_time and existing.run_time == legacy_time:
                existing.run_time = item.get("run_time")
                existing.description = item.get("description")
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
                owner_user_id=item_owner_user_id,
                enabled=item.get("enabled", False),
                sort_order=_JOB_SORT[item["job_key"]],
            )
        )
        created += 1
    if owner_user_id is not None:
        _adopt_legacy_default_job_history(session, owner_user_id=owner_user_id)
    _normalize_shared_price_sync_history(session)
    if created or owner_user_id is not None:
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
