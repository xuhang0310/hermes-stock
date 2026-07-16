"""系统域 mixin：health / dashboard / events / event_detail / system_logs / system_log_modules。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import case, desc, func, select

from backend.app.core.models import AgentReport, Event, EventEvidence, Instrument, Position, SystemLog, WatchlistItem
from backend.app.data._helpers import dt, parse_json, system_log_out


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

    def dashboard(self) -> dict[str, Any]:
        return {
            "title": "SixRabbits A股研究台",
            "market_status": "A股研究模式",
            "pipeline": ["数据源", "数据网关", "MySQL", "规则引擎", "Agent路由", "通知/日报"],
            "disclaimer": "仅供研究与复盘，不自动下单，不构成投资建议",
            "counts": {
                "open_positions": self.db.scalar(select(func.count(Position.id)).where(Position.status == "open")) or 0,
                "watchlist": self.db.scalar(select(func.count(WatchlistItem.id)).where(WatchlistItem.status == "active")) or 0,
                "l0_events": self.db.scalar(select(func.count(Event.id)).where(Event.level == "L0")) or 0,
                "reports": self.db.scalar(select(func.count(AgentReport.id))) or 0,
            },
            "recent_events": self.events(limit=8),
            "positions": self.positions(),
            "agents": self.agents(),
            "logs": self.system_logs(limit=8),
        }

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