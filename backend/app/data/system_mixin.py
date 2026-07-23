"""系统域 mixin：health / dashboard / events / event_detail / system_logs。"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, desc, func, select

from backend.app.core.models import (
    AgentReport,
    AgentToolApproval,
    AiChatSession,
    DailyBar,
    Event,
    EventEvidence,
    Instrument,
    JobRun,
    PositionAccount,
    RiskCheck,
    ScheduledJob,
    SystemLog,
    WatchlistItem,
)
from backend.app.core.settings import settings
from backend.app.data._helpers import dt, parse_json, system_log_out


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
INDEX_SPECS = (
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
)


def _now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ).replace(tzinfo=None)


def _local_iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=SHANGHAI_TZ).isoformat() if value else None


def _utc_iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _configured_trading_day(day: date) -> bool:
    return day.isoweekday() < 6 and day.isoformat() not in set(settings.stock_holidays)


def _market_state(now: datetime) -> dict[str, str]:
    if not _configured_trading_day(now.date()):
        return {"code": "closed", "label": "休市", "tone": "muted"}
    current = now.time()
    if time(9, 30) <= current < time(11, 30) or time(13, 0) <= current < time(15, 0):
        return {"code": "open", "label": "交易中", "tone": "ok"}
    if time(11, 30) <= current < time(13, 0):
        return {"code": "break", "label": "午间休市", "tone": "warning"}
    if current < time(9, 30):
        return {"code": "preopen", "label": "盘前", "tone": "warning"}
    return {"code": "closed", "label": "已收盘", "tone": "muted"}


def _risk_level(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"high", "critical", "l0", "高", "严重"}:
        return "high"
    if normalized in {"medium", "warning", "l1", "中", "警告"}:
        return "medium"
    return "low"


def _last_month_day_allowed(day: date, *, trading_day_only: bool) -> bool:
    cursor = day + timedelta(days=1)
    while cursor.month == day.month:
        if not trading_day_only or _configured_trading_day(cursor):
            return False
        cursor += timedelta(days=1)
    return not trading_day_only or _configured_trading_day(day)


def _job_day_allowed(job: ScheduledJob, day: date) -> bool:
    if job.trading_day_only and not _configured_trading_day(day):
        return False
    if job.schedule_type in {"daily", "weekly"} and job.weekday_mask:
        allowed = {item.strip() for item in job.weekday_mask.split(",") if item.strip()}
        if str(day.isoweekday()) not in allowed:
            return False
    if job.schedule_type == "monthly_last" and not _last_month_day_allowed(
        day, trading_day_only=job.trading_day_only
    ):
        return False
    return True


def _parse_hhmm(value: str | None) -> tuple[int, int] | None:
    try:
        hour_text, minute_text = (value or "").split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (TypeError, ValueError):
        pass
    return None


def _next_job_run(job: ScheduledJob, now: datetime) -> datetime | None:
    if not job.enabled:
        return None
    if job.next_run_at and job.next_run_at > now:
        return job.next_run_at

    for offset in range(0, 40):
        day = now.date() + timedelta(days=offset)
        if not _job_day_allowed(job, day):
            continue

        if job.schedule_type == "interval":
            interval = max(int(job.interval_minutes or 0), 1)
            start_parts = _parse_hhmm(job.window_start) or (0, 0)
            end_parts = _parse_hhmm(job.window_end) or (23, 59)
            candidate = datetime.combine(day, time(*start_parts))
            window_end = datetime.combine(day, time(*end_parts))
            if candidate <= now:
                elapsed_minutes = max(0, int((now - candidate).total_seconds() // 60) + 1)
                candidate += timedelta(minutes=((elapsed_minutes + interval - 1) // interval) * interval)
            if candidate <= window_end:
                return candidate
            continue

        run_parts = _parse_hhmm(job.run_time)
        if run_parts is None:
            continue
        candidate = datetime.combine(day, time(*run_parts))
        if candidate > now:
            return candidate
    return None


def _approval_preview(row: AgentToolApproval) -> str:
    data = parse_json(row.arguments_json)
    if not isinstance(data, dict):
        return str(data or "")[:240]
    if row.tool_name == "ws_run_python":
        return f"执行脚本：{data.get('script_path') or '未指定'}"
    if row.tool_name in {"ws_write_file", "ws_edit_file"}:
        return f"修改文件：{data.get('path') or data.get('file_path') or '未指定'}"
    return json.dumps(data, ensure_ascii=False, default=str)[:240]


class SystemMixin:
    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "database": self.db.bind.url.render_as_string(hide_password=True) if self.db.bind else "",
            "user_version": 1,
            "journal_mode": "mysql",
            "integrity": "ok",
            "missing_tables": [],
            "event_count": self.db.scalar(select(func.count(Event.id))) or 0,
            "report_count": self.db.scalar(select(func.count(AgentReport.id))) or 0,
            "log_count": self.db.scalar(select(func.count(SystemLog.id))) or 0,
        }

    def dashboard(self, user_id: int | None = None) -> dict[str, Any]:
        now = _now_shanghai()
        utc_now = datetime.utcnow()
        day_start = datetime.combine(now.date(), time.min) - timedelta(hours=8)
        last_24h = utc_now - timedelta(hours=24)

        positions = self.positions()
        summary = self._dashboard_portfolio(positions)
        reports = self.reports(limit=20)
        risk_checks = self.risk_checks(limit=30)
        enriched_positions = self._dashboard_positions(positions, reports, risk_checks)
        market = self._dashboard_market()
        runtime = self._dashboard_runtime()
        approvals = self._dashboard_approvals(user_id, utc_now)
        risk_events = self._dashboard_risk_events()

        risk_counts = {"high": 0, "medium": 0, "low": 0}
        for level, total in self.db.execute(
            select(RiskCheck.risk_level, func.count())
            .where(RiskCheck.created_at >= last_24h)
            .group_by(RiskCheck.risk_level)
        ).all():
            risk_counts[_risk_level(level)] += int(total)

        scheduled_jobs = list(
            self.db.scalars(
                select(ScheduledJob)
                .where(ScheduledJob.enabled.is_(True))
                .order_by(ScheduledJob.sort_order, ScheduledJob.id)
            ).all()
        )
        next_candidates = [
            (candidate, job)
            for job in scheduled_jobs
            if (candidate := _next_job_run(job, now)) is not None
        ]
        next_job = None
        if next_candidates:
            run_at, job = min(next_candidates, key=lambda item: item[0])
            next_job = {
                "job_key": job.job_key,
                "name": job.name,
                "run_at": _local_iso(run_at),
                "trading_day_only": job.trading_day_only,
                "digital_human_code": job.digital_human_code,
            }

        day_pnl = float(summary.get("day_pnl") or 0)
        holding_value = float(summary.get("holding_market_value") or 0)
        day_base = holding_value - day_pnl
        day_pnl_pct = round(day_pnl / day_base * 100, 2) if day_base > 0 else None
        market_updated_at = max(
            (item.get("updated_at") for item in market["items"] if item.get("updated_at")),
            default=None,
        )

        return {
            "title": "SixRabbits A股研究台",
            "market_status": _market_state(now)["label"],
            "market_state": _market_state(now),
            "server_time": _local_iso(now),
            "generated_at": _local_iso(now),
            "pipeline": ["数据源", "数据网关", "MySQL", "规则引擎", "Agent路由", "通知/日报"],
            "disclaimer": "仅供研究与复盘，不自动下单，不构成投资建议",
            "counts": {
                "open_positions": summary.get("holding_count") or 0,
                "watchlist": self.db.scalar(
                    select(func.count(WatchlistItem.id)).where(
                        WatchlistItem.status == "active",
                        self._owner_clause(WatchlistItem),
                    )
                )
                or 0,
                "l0_events": self.db.scalar(select(func.count(Event.id)).where(Event.level == "L0")) or 0,
                "reports": self.db.scalar(select(func.count(AgentReport.id))) or 0,
                "high_risk_24h": risk_counts["high"],
                "pending_approvals": len(approvals),
                "running_jobs": len(runtime["running"]),
            },
            "portfolio": {
                **summary,
                "day_pnl_pct": day_pnl_pct,
            },
            "market": market,
            "risk_summary": {**risk_counts, "since": dt(last_24h)},
            "risk_events": risk_events,
            "recent_events": self.events(limit=8),
            "positions": enriched_positions,
            "reports": reports[:5],
            "agents": self.agents(),
            "runtime": runtime,
            "approvals": approvals,
            "next_job": next_job,
            "data_freshness": {
                "positions": max((item.get("updated_at") for item in positions if item.get("updated_at")), default=None),
                "market": market_updated_at,
                "risk": max((item.get("created_at") for item in risk_checks if item.get("created_at")), default=None),
                "reports": max((item.get("created_at") for item in reports if item.get("created_at")), default=None),
                "runs": max(
                    (
                        item.get("started_at")
                        for item in [*runtime["running"], *runtime["recent"]]
                        if item.get("started_at")
                    ),
                    default=None,
                ),
            },
            "today": {
                "reports": self.db.scalar(
                    select(func.count(AgentReport.id)).where(AgentReport.created_at >= day_start)
                )
                or 0,
            },
            "logs": self.system_logs(limit=8),
        }

    def _dashboard_portfolio(self, positions: list[dict[str, Any]]) -> dict[str, Any]:
        accounts = list(
            self.db.scalars(
                select(PositionAccount)
                .where(
                    PositionAccount.status != "archived",
                    self._owner_clause(PositionAccount),
                )
                .order_by(PositionAccount.sort_order, PositionAccount.id)
            ).all()
        )

        def account_sum(field: str) -> float | None:
            values = [getattr(account, field) for account in accounts]
            present = [float(value) for value in values if value is not None]
            return round(sum(present), 2) if present else None

        def position_sum(field: str) -> float | None:
            present = [float(row[field]) for row in positions if row.get(field) is not None]
            return round(sum(present), 2) if present else None

        holding_market_value = account_sum("holding_market_value")
        total_pnl = account_sum("total_pnl")
        day_pnl = account_sum("day_pnl")
        if holding_market_value is None:
            holding_market_value = position_sum("market_value")
        if total_pnl is None:
            total_pnl = position_sum("floating_pnl")
        if day_pnl is None:
            day_pnl = position_sum("day_pnl")

        open_symbols = {
            str(row.get("symbol"))
            for row in positions
            if row.get("status") == "open" and row.get("symbol")
        }
        return {
            "account_count": len(accounts),
            "total_asset": account_sum("total_asset"),
            "available_cash": account_sum("available_cash"),
            "holding_market_value": holding_market_value,
            "total_pnl": total_pnl,
            "day_pnl": day_pnl,
            "holding_count": len(open_symbols),
            "fund_updated_at": max(
                (_utc_iso(account.fund_updated_at) for account in accounts if account.fund_updated_at),
                default=None,
            ),
        }

    def _dashboard_positions(
        self,
        positions: list[dict[str, Any]],
        reports: list[dict[str, Any]],
        risk_checks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        report_by_symbol: dict[str, dict[str, Any]] = {}
        for report in reports:
            symbol = report.get("symbol")
            if symbol and symbol not in report_by_symbol:
                report_by_symbol[symbol] = report

        risk_by_symbol: dict[str, dict[str, Any]] = {}
        for risk in risk_checks:
            symbol = risk.get("symbol")
            if symbol and symbol not in risk_by_symbol:
                risk_by_symbol[symbol] = risk

        result: list[dict[str, Any]] = []
        for position in positions:
            row = dict(position)
            latest_price = row.get("latest_price")
            stop_price = row.get("stop_price")
            stop_distance = None
            if latest_price not in (None, 0) and stop_price is not None:
                stop_distance = round((float(latest_price) - float(stop_price)) / float(latest_price) * 100, 2)

            latest_risk = risk_by_symbol.get(str(row.get("symbol") or ""))
            level = _risk_level(latest_risk.get("risk_level") if latest_risk else None)
            if stop_distance is not None and stop_distance <= 2:
                risk_state = {"level": "high", "label": "接近止损"}
            elif level == "high":
                risk_state = {"level": "high", "label": "高风险"}
            elif (stop_distance is not None and stop_distance <= 5) or level == "medium":
                risk_state = {"level": "medium", "label": "观察"}
            else:
                risk_state = {"level": "low", "label": "安全"}

            day_pnl = row.get("day_pnl")
            market_value = row.get("market_value")
            day_base = float(market_value or 0) - float(day_pnl or 0)
            row.update(
                {
                    "stop_distance_pct": stop_distance,
                    "day_pnl_pct": round(float(day_pnl) / day_base * 100, 2)
                    if day_pnl is not None and day_base > 0
                    else None,
                    "latest_research": report_by_symbol.get(str(row.get("symbol") or "")),
                    "latest_risk": latest_risk,
                    "risk_state": risk_state,
                }
            )
            result.append(row)
        return result

    def _dashboard_market(self) -> dict[str, Any]:
        symbols = [item[0] for item in INDEX_SPECS]
        ranked = (
            select(
                DailyBar.symbol.label("symbol"),
                DailyBar.trade_date.label("trade_date"),
                DailyBar.close.label("close"),
                DailyBar.change_pct.label("change_pct"),
                DailyBar.source.label("source"),
                DailyBar.created_at.label("created_at"),
                func.row_number()
                .over(partition_by=DailyBar.symbol, order_by=(desc(DailyBar.trade_date), desc(DailyBar.id)))
                .label("rank"),
            )
            .where(DailyBar.symbol.in_(symbols))
            .subquery()
        )
        rows = self.db.execute(
            select(ranked).where(ranked.c.rank <= 14).order_by(ranked.c.symbol, ranked.c.trade_date)
        ).mappings().all()
        by_symbol: dict[str, list[Any]] = {symbol: [] for symbol in symbols}
        for row in rows:
            by_symbol[str(row["symbol"])].append(row)

        items: list[dict[str, Any]] = []
        for symbol, name in INDEX_SPECS:
            bars = by_symbol[symbol]
            latest = bars[-1] if bars else None
            items.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "price": float(latest["close"]) if latest and latest["close"] is not None else None,
                    "change_pct": float(latest["change_pct"])
                    if latest and latest["change_pct"] is not None
                    else None,
                    "trade_date": latest["trade_date"] if latest else None,
                    "source": latest["source"] if latest else "tushare-cache",
                    "updated_at": dt(latest["created_at"]) if latest else None,
                    "sparkline": [float(bar["close"]) for bar in bars if bar["close"] is not None],
                }
            )
        return {"source": "TuShare 本地缓存", "items": items}

    def _dashboard_runtime(self) -> dict[str, list[dict[str, Any]]]:
        base = select(JobRun, ScheduledJob.name, ScheduledJob.digital_human_code).outerjoin(
            ScheduledJob, ScheduledJob.job_key == JobRun.job_key
        )
        running_rows = self.db.execute(
            base.where(JobRun.status == "running").order_by(desc(JobRun.started_at)).limit(4)
        ).all()
        recent_rows = self.db.execute(
            base.where(JobRun.status != "running")
            .order_by(desc(JobRun.started_at), desc(JobRun.id))
            .limit(4)
        ).all()

        def out(row: Any) -> dict[str, Any]:
            run, name, digital_human_code = row
            return {
                "run_id": run.run_id,
                "job_key": run.job_key,
                "name": name or run.job_key,
                "digital_human_code": digital_human_code,
                "trigger_type": run.trigger_type,
                "status": run.status,
                "started_at": dt(run.started_at),
                "finished_at": dt(run.finished_at),
                "duration_ms": run.duration_ms,
                "report_id": run.report_id,
                "output_preview": run.output_preview,
                "error": run.error,
            }

        return {
            "running": [out(row) for row in running_rows],
            "recent": [out(row) for row in recent_rows],
        }

    def _dashboard_approvals(self, user_id: int | None, now: datetime) -> list[dict[str, Any]]:
        if user_id is None:
            return []
        rows = self.db.execute(
            select(AgentToolApproval, AiChatSession.title)
            .outerjoin(AiChatSession, AiChatSession.id == AgentToolApproval.session_id)
            .where(
                AgentToolApproval.user_id == user_id,
                AgentToolApproval.status == "pending",
                AgentToolApproval.expires_at > now,
            )
            .order_by(desc(AgentToolApproval.created_at))
            .limit(10)
        ).all()
        return [
            {
                "approval_id": row.approval_id,
                "session_id": row.session_id,
                "session_title": session_title,
                "tool": row.tool_name,
                "display_name": row.display_name,
                "risk_level": row.risk_level,
                "arguments_preview": _approval_preview(row),
                "status": row.status,
                "expires_at": _utc_iso(row.expires_at),
                "created_at": _utc_iso(row.created_at),
            }
            for row, session_title in rows
        ]

    def _dashboard_risk_events(self) -> list[dict[str, Any]]:
        risk_rows = self.db.execute(
            select(RiskCheck, Instrument.name.label("symbol_name"))
            .outerjoin(Instrument, Instrument.symbol == RiskCheck.symbol)
            .order_by(desc(RiskCheck.created_at))
            .limit(6)
        ).all()
        event_rows = self.db.execute(
            select(Event, Instrument.name.label("symbol_name"))
            .outerjoin(Instrument, Instrument.symbol == Event.symbol)
            .order_by(desc(Event.created_at))
            .limit(6)
        ).all()
        items = [
            {
                "kind": "risk",
                "id": str(row.id),
                "level": _risk_level(row.risk_level),
                "symbol": row.symbol,
                "symbol_name": symbol_name,
                "title": row.finding,
                "summary": row.required_action,
                "created_at": dt(row.created_at),
            }
            for row, symbol_name in risk_rows
        ]
        items.extend(
            {
                "kind": "event",
                "id": row.event_id,
                "level": _risk_level(row.level),
                "symbol": row.symbol,
                "symbol_name": symbol_name,
                "title": row.title,
                "summary": row.summary,
                "created_at": dt(row.created_at),
            }
            for row, symbol_name in event_rows
        )
        return sorted(items, key=lambda item: item.get("created_at") or "", reverse=True)[:6]

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(Event, Instrument.name.label("symbol_name"))
            .outerjoin(Instrument, Instrument.symbol == Event.symbol)
            .order_by(case((Event.level == "L0", 0), (Event.level == "L1", 1), else_=2), desc(Event.created_at))
            .limit(limit)
        ).all()
        return [self._event_out(event, symbol_name) for event, symbol_name in rows]

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            select(Event, Instrument.name.label("symbol_name"))
            .outerjoin(Instrument, Instrument.symbol == Event.symbol)
            .where(Event.event_id == event_id)
        ).first()
        if row is None:
            return None

        event, symbol_name = row
        result = self._event_out(event, symbol_name)
        result["evidence"] = [
            {
                "evidence_type": item.evidence_type,
                "source_table": item.source_table,
                "source_id": item.source_id,
                "content": item.content,
                "created_at": dt(item.created_at),
            }
            for item in self.db.scalars(
                select(EventEvidence).where(EventEvidence.event_id == event.id).order_by(desc(EventEvidence.created_at))
            )
        ]
        result["reports"] = [
            {
                "report_id": item.report_id,
                "agent_name": item.agent_name,
                "title": item.title,
                "conclusion": item.conclusion,
                "action_suggestion": item.action_suggestion,
                "created_at": dt(item.created_at),
            }
            for item in self.db.scalars(
                select(AgentReport).where(AgentReport.event_id == event.event_id).order_by(desc(AgentReport.created_at))
            )
        ]
        return result

    def system_logs(
        self, limit: int = 100, module: str | None = None, level: str | None = None
    ) -> list[dict[str, Any]]:
        query = select(SystemLog)
        if module:
            query = query.where(SystemLog.module == module)
        if level:
            query = query.where(SystemLog.level == level.upper())
        rows = self.db.scalars(
            query.order_by(desc(SystemLog.created_at), desc(SystemLog.id)).limit(limit)
        ).all()
        return [system_log_out(row) for row in rows]

    def system_log_modules(self) -> list[str]:
        rows = self.db.scalars(select(SystemLog.module).distinct()).all()
        return sorted({str(r) for r in rows if r})

    def _event_out(self, event: Event, symbol_name: str | None) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "level": event.level,
            "event_type": event.event_type,
            "symbol": event.symbol,
            "symbol_name": symbol_name,
            "title": event.title,
            "summary": event.summary,
            "facts": parse_json(event.facts_json),
            "source_type": event.source_type,
            "status": event.status,
            "created_at": dt(event.created_at),
            "updated_at": dt(event.updated_at),
        }
