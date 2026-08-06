"""只读域 mixin：agents / reports / risk_checks 查询。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import desc, func, select

from backend.app.core.models import AgentProfile, AgentReport, AgentTask, Instrument, RiskCheck
from backend.app.data._helpers import dt


class AgentMixin:
    def agents(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(select(AgentProfile)).all()
        counts = dict(self.db.execute(select(AgentReport.agent_name, func.count(AgentReport.id)).group_by(AgentReport.agent_name)).all())
        pending = dict(
            self.db.execute(
                select(AgentTask.agent_name, func.count(AgentTask.id)).where(AgentTask.status == "pending").group_by(AgentTask.agent_name)
            ).all()
        )
        running = dict(
            self.db.execute(
                select(AgentTask.agent_name, func.count(AgentTask.id)).where(AgentTask.status == "running").group_by(AgentTask.agent_name)
            ).all()
        )
        order = {"pm": 0, "sentinel": 1, "intelligence": 2, "risk": 3, "researcher": 4}
        rows = sorted(rows, key=lambda item: (not item.enabled, order.get(item.agent_name, 9), item.agent_name))
        return [
            {
                "agent_name": item.agent_name,
                "display_name": item.display_name,
                "enabled": item.enabled,
                "role_description": item.role_description,
                "model_primary": item.model_primary,
                "model_backup": item.model_backup,
                "pending_count": pending.get(item.agent_name, 0),
                "running_count": running.get(item.agent_name, 0),
                "report_count": counts.get(item.agent_name, 0),
                "last_update": dt(item.updated_at),
                "status": "running" if running.get(item.agent_name, 0) else ("ready" if item.enabled else "disabled"),
            }
            for item in rows
        ]

    def reports(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(AgentReport, AgentProfile.display_name, Instrument.name.label("symbol_name"))
            .outerjoin(AgentProfile, AgentProfile.agent_name == AgentReport.agent_name)
            .outerjoin(Instrument, Instrument.symbol == AgentReport.symbol)
            .order_by(desc(AgentReport.created_at))
            .limit(limit)
        ).all()
        return [
            {
                "report_id": item.report_id,
                "agent_name": item.agent_name,
                "display_name": display_name,
                "symbol": item.symbol,
                "symbol_name": symbol_name,
                "title": item.title,
                "conclusion": item.conclusion,
                "risk_points": item.risk_points,
                "action_suggestion": item.action_suggestion,
                "model_name": item.model_name,
                "created_at": dt(item.created_at),
            }
            for item, display_name, symbol_name in rows
        ]

    def report_detail(self, report_id: str) -> dict[str, Any] | None:
        """按 report_id 取单篇报告全量结构化字段；不存在返回 None。"""
        row = self.db.execute(
            select(AgentReport, AgentProfile.display_name, Instrument.name.label("symbol_name"))
            .outerjoin(AgentProfile, AgentProfile.agent_name == AgentReport.agent_name)
            .outerjoin(Instrument, Instrument.symbol == AgentReport.symbol)
            .where(AgentReport.report_id == report_id)
            .limit(1)
        ).first()
        if row is None:
            return None
        item, display_name, symbol_name = row
        return {
            "report_id": item.report_id,
            "task_id": item.task_id,
            "event_id": item.event_id,
            "agent_name": item.agent_name,
            "display_name": display_name,
            "symbol": item.symbol,
            "symbol_name": symbol_name,
            "title": item.title,
            "conclusion": item.conclusion,
            "reasoning": item.reasoning,
            "counter_arguments": item.counter_arguments,
            "risk_points": item.risk_points,
            "position_limit": item.position_limit,
            "stop_condition": item.stop_condition,
            "action_suggestion": item.action_suggestion,
            "content": item.content,
            "model_name": item.model_name,
            "created_at": dt(item.created_at),
            "updated_at": dt(item.updated_at),
        }

    def risk_checks(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(RiskCheck, Instrument.name.label("symbol_name"))
            .outerjoin(Instrument, Instrument.symbol == RiskCheck.symbol)
            .order_by(desc(RiskCheck.created_at))
            .limit(limit)
        ).all()
        return [
            {
                "id": item.id,
                "symbol": item.symbol,
                "symbol_name": symbol_name,
                "risk_level": item.risk_level,
                "risk_type": item.risk_type,
                "finding": item.finding,
                "required_action": item.required_action,
                "position_limit": item.position_limit,
                "stop_condition": item.stop_condition,
                "cool_down_required": int(item.cool_down_required),
                "created_at": dt(item.created_at),
            }
            for item, symbol_name in rows
        ]
