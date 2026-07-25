from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.database import SessionLocal
from backend.app.core.models import AgentTransactionState


TRANSACTION_AGENT_CODES = {"position-entry-agent", "watchlist-entry-agent"}


def _load_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class TransactionStateService:
    def __init__(self, session_factory: Callable[[], Session] | sessionmaker[Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    def load(self, session_id: int, agent_code: str) -> dict[str, Any] | None:
        if agent_code not in TRANSACTION_AGENT_CODES:
            return None
        with self.session_factory() as db:
            row = db.scalar(
                select(AgentTransactionState).where(
                    AgentTransactionState.session_id == session_id,
                    AgentTransactionState.agent_code == agent_code,
                )
            )
            if row is None:
                return None
            return self._dict(row)

    def clear_pending_for_user(self, user_id: int, agent_code: str) -> int:
        if agent_code not in TRANSACTION_AGENT_CODES:
            return 0
        with self.session_factory() as db:
            rows = db.scalars(
                select(AgentTransactionState).where(
                    AgentTransactionState.user_id == user_id,
                    AgentTransactionState.agent_code == agent_code,
                    AgentTransactionState.pending_action_json.is_not(None),
                )
            ).all()
            for row in rows:
                slots = _load_json(row.slots_json, {})
                slots.pop("rows", None)
                row.slots_json = json.dumps(slots, ensure_ascii=False, default=str)
                row.pending_action_json = None
                row.missing_fields_json = "[]"
                row.status = "completed"
            if rows:
                db.commit()
            return len(rows)

    @staticmethod
    def _dict(row: AgentTransactionState) -> dict[str, Any]:
        return {
            "session_id": row.session_id,
            "user_id": row.user_id,
            "agent_code": row.agent_code,
            "intent": row.intent,
            "slots": _load_json(row.slots_json, {}),
            "evidence": _load_json(row.evidence_json, []),
            "missing_fields": _load_json(row.missing_fields_json, []),
            "pending_action": _load_json(row.pending_action_json, None),
            "status": row.status,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        }

    def initialize(self, request_payload: dict[str, Any]) -> dict[str, Any] | None:
        code = str(request_payload.get("digital_human_code") or "")
        if code not in TRANSACTION_AGENT_CODES:
            return None
        session_id = int(request_payload["session_id"])
        user_id = int(request_payload["user_id"])
        with self.session_factory() as db:
            row = db.scalar(
                select(AgentTransactionState)
                .where(
                    AgentTransactionState.session_id == session_id,
                    AgentTransactionState.agent_code == code,
                )
                .with_for_update()
            )
            if row is None:
                row = AgentTransactionState(
                    session_id=session_id,
                    user_id=user_id,
                    agent_code=code,
                    slots_json="{}",
                    evidence_json="[]",
                    missing_fields_json="[]",
                    status="collecting",
                )
                db.add(row)
                db.flush()
            slots = _load_json(row.slots_json, {})
            explicit_account_id = request_payload.get("account_id")
            default_account_id = request_payload.get("default_account_id")
            account_id = explicit_account_id or default_account_id
            if account_id:
                slots["account_id"] = int(account_id)
                slots["account_source"] = "explicit" if explicit_account_id else "default"
            elif code == "position-entry-agent" and slots.get("account_id") and not slots.get("account_source"):
                # Older versions silently cached the user's default account in this slot.
                # Its origin cannot be proven, so do not keep forcing it onto future OCR imports.
                slots.pop("account_id", None)
            row.slots_json = json.dumps(slots, ensure_ascii=False, default=str)
            supplied_pending = request_payload.get("pending_action")
            if supplied_pending and not row.pending_action_json:
                row.pending_action_json = json.dumps(supplied_pending, ensure_ascii=False, default=str)
            row.expires_at = datetime.utcnow() + timedelta(days=1)
            db.commit()
            db.refresh(row)
            return self._dict(row)

    def prompt_context(self, request_payload: dict[str, Any]) -> str:
        state = self.initialize(request_payload)
        if state is None:
            return ""
        code = state["agent_code"]
        pending = state.get("pending_action")
        if code == "position-entry-agent":
            from backend.app.agents.position_agent import _pending_action_hint, _selected_account_hint
            from backend.app.data.repository import StockRepository

            account_id = state.get("slots", {}).get("account_id")
            account_hint = ""
            if account_id:
                with self.session_factory() as db:
                    account_hint = _selected_account_hint(
                        StockRepository(
                            db,
                            owner_user_id=int(request_payload["user_id"]),
                        ),
                        int(account_id),
                    )
            return account_hint + _pending_action_hint(pending)
        from backend.app.agents.watchlist_agent import _pending_action_hint

        return _pending_action_hint(pending)

    def before_tool(
        self,
        request_payload: dict[str, Any],
        tool_name: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        state = self.initialize(request_payload)
        if state is None:
            return arguments, None
        pending = state.get("pending_action")
        if state["agent_code"] == "position-entry-agent":
            from backend.app.agents.position_agent import _merge_pending_tool_args

            arguments = _merge_pending_tool_args(tool_name, arguments, pending)
        else:
            from backend.app.agents.watchlist_agent import _merge_pending_tool_args

            arguments = _merge_pending_tool_args(tool_name, arguments, pending)
        return arguments, state

    def after_tool(
        self,
        request_payload: dict[str, Any],
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        code = str(request_payload.get("digital_human_code") or "")
        if code not in TRANSACTION_AGENT_CODES:
            return
        session_id = int(request_payload["session_id"])
        with self.session_factory() as db:
            row = db.scalar(
                select(AgentTransactionState)
                .where(
                    AgentTransactionState.session_id == session_id,
                    AgentTransactionState.agent_code == code,
                )
                .with_for_update()
            )
            if row is None:
                return
            slots = _load_json(row.slots_json, {})
            evidence = _load_json(row.evidence_json, [])
            pending = _load_json(row.pending_action_json, None)
            missing: list[str] = []

            if code == "position-entry-agent":
                from backend.app.agents.position_agent import _pending_from_tool_result

                argument_account = (
                    arguments.get("account")
                    if isinstance(arguments.get("account"), dict)
                    else {}
                )
                result_rows = result.get("rows") if isinstance(result.get("rows"), list) else []
                written_account_id = next(
                    (
                        item.get("account_id")
                        for item in result_rows
                        if isinstance(item, dict) and item.get("account_id")
                    ),
                    None,
                )
                account_id = (
                    request_payload.get("account_id")
                    or request_payload.get("default_account_id")
                    or argument_account.get("account_id")
                    or written_account_id
                    or slots.get("account_id")
                )
                if account_id:
                    slots["account_id"] = int(account_id)
                    slots["account_source"] = (
                        "explicit"
                        if request_payload.get("account_id") or argument_account.get("account_id")
                        else "resolved"
                    )
                next_pending = _pending_from_tool_result(tool_name, arguments, result)
                if next_pending is not None:
                    pending = next_pending
                    slots["rows"] = next_pending.get("arguments", {}).get("rows", [])
                    missing = (
                        ["symbol_review"]
                        if next_pending.get("review_required")
                        else ["account_id"]
                    )
                    row.status = "waiting_input"
                elif tool_name == "save_position_holdings" and int(result.get("saved_count") or 0):
                    pending = None
                    slots.pop("rows", None)
                    row.status = "completed"
                elif tool_name == "save_position_holdings" and result.get("error"):
                    row.status = "failed"
            else:
                from backend.app.agents.watchlist_agent import (
                    _pending_from_search,
                    _pending_from_write_failure,
                )

                question = str(request_payload.get("question") or "")
                next_pending = None
                if tool_name == "search_instruments":
                    next_pending = _pending_from_search(question, arguments, result)
                elif tool_name in {"add_watchlist_items", "remove_watchlist_items"}:
                    next_pending = _pending_from_write_failure(tool_name, arguments, result, question)
                    if next_pending is None and int(result.get("changed_count") or 0):
                        pending = None
                        row.status = "completed"
                if next_pending is not None:
                    pending = next_pending
                    missing = ["symbol"]
                    row.status = "waiting_input"

            evidence.append(
                {
                    "tool": tool_name,
                    "success": "error" not in result,
                    "at": datetime.utcnow().isoformat(),
                }
            )
            row.slots_json = json.dumps(slots, ensure_ascii=False, default=str)
            row.evidence_json = json.dumps(evidence[-20:], ensure_ascii=False, default=str)
            row.missing_fields_json = json.dumps(missing, ensure_ascii=False)
            row.pending_action_json = (
                json.dumps(pending, ensure_ascii=False, default=str) if pending else None
            )
            row.expires_at = datetime.utcnow() + timedelta(days=1)
            db.commit()


transaction_state_service = TransactionStateService()
