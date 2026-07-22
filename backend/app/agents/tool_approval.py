"""Server-side Agent tool approval policy and cross-replica wait state."""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from backend.app.core.applog import log_event
from backend.app.core.database import SessionLocal
from sqlalchemy import distinct, select, update

from backend.app.core.models import AgentToolApproval


ApprovalPolicy = Literal["ask", "smart", "full"]
RiskLevel = Literal["low", "medium", "high"]

APPROVAL_TTL_SECONDS = 300
_WORKSPACE_MUTATIONS = {"ws_write_file", "ws_edit_file"}
_CODE_EXECUTION = {"ws_run_python"}
_PERSISTENT_AUTOMATION = {
    "create_agent_schedule",
    "update_agent_schedule",
    "pause_agent_schedule",
    "delete_agent_schedule",
}


def normalize_approval_policy(value: str | None, *, legacy_allow_code_execution: bool = False) -> ApprovalPolicy:
    if value in {"ask", "smart", "full"}:
        return value  # type: ignore[return-value]
    return "full" if legacy_allow_code_execution else "ask"


def classify_tool_risk(tool_name: str, args: dict[str, Any], write_tool_names: set[str] | None = None) -> RiskLevel:
    if tool_name in _CODE_EXECUTION or tool_name in _PERSISTENT_AUTOMATION:
        return "high"
    if tool_name in (write_tool_names or set()):
        return "high" if bool(args.get("confirmed")) else "low"
    if tool_name in _WORKSPACE_MUTATIONS:
        return "medium"
    return "low"


def requires_approval(policy: ApprovalPolicy, risk_level: RiskLevel) -> bool:
    if policy == "full":
        return False
    if policy == "smart":
        return risk_level == "high"
    return risk_level in {"medium", "high"}


def _argument_summary(tool_name: str, args: dict[str, Any]) -> tuple[str, str]:
    sanitized: dict[str, Any] = {}
    for key, value in args.items():
        lowered = key.lower()
        if any(token in lowered for token in ("base64", "secret", "token", "password", "api_key")):
            sanitized[key] = "[已隐藏]"
        elif key in {"content", "patch"} and isinstance(value, str):
            sanitized[key] = {"characters": len(value), "preview": value[:240]}
        else:
            sanitized[key] = value
    encoded = json.dumps(sanitized, ensure_ascii=False, default=str)
    if len(encoded) > 4000:
        encoded = encoded[:3997] + "..."

    if tool_name == "ws_run_python":
        preview = f"执行脚本：{args.get('script_path') or '未指定'}"
    elif tool_name in _WORKSPACE_MUTATIONS:
        preview = f"修改工作区文件：{args.get('path') or args.get('file_path') or '未指定'}"
    else:
        preview = encoded[:360]
    return encoded, preview


def create_tool_approval(
    *,
    session_id: int,
    user_id: int,
    tool_name: str,
    display_name: str,
    risk_level: RiskLevel,
    args: dict[str, Any],
    run_id: str | None = None,
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    approval_id = uuid.uuid4().hex
    arguments_json, arguments_preview = _argument_summary(tool_name, args)
    expires_at = datetime.utcnow() + timedelta(seconds=APPROVAL_TTL_SECONDS)
    with SessionLocal() as db:
        db.add(
            AgentToolApproval(
                approval_id=approval_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                session_id=session_id,
                user_id=user_id,
                tool_name=tool_name,
                display_name=display_name,
                risk_level=risk_level,
                arguments_json=arguments_json,
                status="pending",
                expires_at=expires_at,
            )
        )
        db.commit()
    log_event(
        "agent", "INFO", "等待工具审批", approval_id=approval_id, user_id=user_id,
        session_id=session_id, tool=tool_name, risk_level=risk_level,
    )
    return {
        "approval_id": approval_id,
        "tool": tool_name,
        "display_name": display_name,
        "risk_level": risk_level,
        "arguments_preview": arguments_preview,
        "expires_at": expires_at.isoformat(),
    }


def wait_for_tool_approval(
    approval_id: str,
    *,
    cancel_event: threading.Event | None = None,
    poll_interval: float = 0.25,
) -> Literal["approved", "denied", "expired", "cancelled"]:
    deadline = time.monotonic() + APPROVAL_TTL_SECONDS
    while time.monotonic() < deadline:
        if cancel_event and cancel_event.is_set():
            _finish_pending(approval_id, "cancelled")
            return "cancelled"
        with SessionLocal() as db:
            row = db.get(AgentToolApproval, approval_id)
            if row is None:
                return "denied"
            if row.status in {"approved", "denied", "expired", "cancelled"}:
                return row.status  # type: ignore[return-value]
            if row.expires_at <= datetime.utcnow():
                row.status = "expired"
                row.resolved_at = datetime.utcnow()
                db.commit()
                return "expired"
        time.sleep(poll_interval)
    _finish_pending(approval_id, "expired")
    return "expired"


def _finish_pending(approval_id: str, status: Literal["expired", "cancelled"]) -> None:
    with SessionLocal() as db:
        row = db.get(AgentToolApproval, approval_id)
        if row and row.status == "pending":
            row.status = status
            row.resolved_at = datetime.utcnow()
            db.commit()


def expire_pending_run_approvals() -> list[str]:
    """Expire parked approvals and return runs that are ready to resume."""

    now = datetime.utcnow()
    with SessionLocal() as db:
        run_ids = list(
            db.scalars(
                select(distinct(AgentToolApproval.run_id)).where(
                    AgentToolApproval.run_id.is_not(None),
                    AgentToolApproval.status == "pending",
                    AgentToolApproval.expires_at <= now,
                )
            ).all()
        )
        if not run_ids:
            return []
        db.execute(
            update(AgentToolApproval)
            .where(
                AgentToolApproval.run_id.in_(run_ids),
                AgentToolApproval.status == "pending",
                AgentToolApproval.expires_at <= now,
            )
            .values(status="expired", resolved_at=now)
        )
        db.commit()
        ready: list[str] = []
        for run_id in run_ids:
            remaining = db.scalar(
                select(AgentToolApproval.approval_id).where(
                    AgentToolApproval.run_id == run_id,
                    AgentToolApproval.status == "pending",
                ).limit(1)
            )
            if not remaining and run_id:
                ready.append(run_id)
        return ready


def cancel_pending_run_approvals(run_id: str) -> int:
    """Cancel every unresolved approval belonging to a cancelled run."""

    now = datetime.utcnow()
    with SessionLocal() as db:
        result = db.execute(
            update(AgentToolApproval)
            .where(
                AgentToolApproval.run_id == run_id,
                AgentToolApproval.status == "pending",
            )
            .values(status="cancelled", resolved_at=now)
        )
        db.commit()
        return int(result.rowcount or 0)
