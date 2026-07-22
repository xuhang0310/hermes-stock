from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select, text

from backend.app.agent_runtime.agentscope_engine import select_engine
from backend.app.agent_runtime.repository import AgentRunRepository, agent_run_repository
from backend.app.core.database import SessionLocal
from backend.app.core.models import AiChatMessage, AiChatSession, DigitalHumanProfile, User
from backend.app.core.settings import settings


def build_channel_request(
    *,
    session_id: int,
    question: str,
    history: list[dict[str, Any]],
    digital_human_code: str | None,
    source_type: str,
    source_ref: str | None,
    user_id: int | None = None,
    user_label: str | None = None,
    model_config_id: int | None = None,
    attachments: list[dict[str, Any]] | None = None,
    default_account_id: int | None = None,
    pending_action: dict[str, Any] | None = None,
    allow_code_execution: bool = False,
    approval_policy: str = "full",
) -> dict[str, Any]:
    with SessionLocal() as db:
        chat_session = db.get(AiChatSession, session_id)
        if chat_session is None:
            raise ValueError("Agent session does not exist.")
        owner = db.get(User, user_id or chat_session.user_id)
        profile = None
        if digital_human_code:
            profile = db.scalar(
                select(DigitalHumanProfile)
                .where(DigitalHumanProfile.code == digital_human_code)
                .limit(1)
            )
        if default_account_id is None and digital_human_code == "position-entry-agent":
            default_account_id = db.scalar(
                text("SELECT id FROM position_accounts WHERE is_default = 1 ORDER BY id LIMIT 1")
            )
    digital_context = None
    if profile is not None:
        digital_context = "\n".join(
            [
                f"当前数字人：{profile.name}",
                f"风格：{profile.avatar_tone}",
                f"职责：{profile.description}",
                f"系统提示：{profile.system_prompt}",
            ]
        )
    return {
        "session_id": session_id,
        "user_id": int(user_id or chat_session.user_id),
        "user_label": user_label or (owner.display_name if owner else "channel-user"),
        "question": question,
        "user_text": question,
        "attachments": attachments or [],
        "history": history,
        "digital_human_id": profile.id if profile else chat_session.digital_human_id,
        "digital_human_code": digital_human_code,
        "digital_human_context": digital_context,
        "model_config_id": model_config_id or chat_session.model_config_id,
        "chat_mode": "research",
        "skills": [],
        "memory_context": "",
        "can_write_positions": digital_human_code == "position-entry-agent",
        "default_account_id": default_account_id,
        "allow_code_execution": bool(allow_code_execution),
        "approval_policy": approval_policy if approval_policy in {"ask", "smart", "full"} else "full",
        "source_type": source_type,
        "source_ref": source_ref,
        "pending_action": pending_action,
    }


def create_channel_run(
    request_payload: dict[str, Any],
    *,
    idempotency_key: str,
    repository: AgentRunRepository = agent_run_repository,
) -> dict[str, Any]:
    source_type = str(request_payload.get("source_type") or "web")
    source_ref = str(request_payload.get("source_ref") or "") or None
    engine = select_engine(str(request_payload.get("chat_mode") or "research"), settings.agent_runtime_mode)
    priority = {"web": 300, "feishu": 200, "scheduler": 100}.get(source_type, 150)
    run, created = repository.create_run(
        session_id=int(request_payload["session_id"]),
        user_id=int(request_payload["user_id"]),
        engine=engine,
        chat_mode=str(request_payload.get("chat_mode") or "research"),
        request_payload=request_payload,
        idempotency_key=idempotency_key,
        priority=priority,
        source_type=source_type,
        source_ref=source_ref,
    )
    if created:
        repository.append_event(
            run["run_id"],
            {
                "type": "session",
                "session_id": request_payload["session_id"],
                "chat_mode": request_payload.get("chat_mode") or "research",
                "source_type": source_type,
            },
        )
        repository.append_event(
            run["run_id"],
            {"type": "queued", "engine": engine, "status": "queued"},
        )
    return {**run, "created": created}


def wait_for_channel_run(
    run_id: str,
    *,
    repository: AgentRunRepository = agent_run_repository,
    timeout_seconds: int | None = None,
) -> tuple[str, str | None, str | None]:
    deadline = time.monotonic() + max(30, timeout_seconds or settings.agent_run_timeout_seconds + 30)
    cursor = 0
    answer_parts: list[str] = []
    final_answer = ""
    model_name: str | None = None
    stream_error: str | None = None
    while time.monotonic() < deadline:
        for event in repository.events_after(run_id, cursor, limit=1000):
            cursor = event.seq
            payload = event.payload
            event_type = event.event_type
            if payload.get("reset_stream"):
                answer_parts.clear()
                final_answer = ""
            if event_type == "model_selected":
                model_name = str((payload.get("model") or {}).get("model_name") or "") or None
            elif event_type == "content_delta":
                answer_parts.append(str(payload.get("content") or ""))
            elif event_type == "content_end" and payload.get("role") == "answer":
                final_answer = "".join(answer_parts).strip()
                answer_parts.clear()
            elif event_type == "error":
                stream_error = str(payload.get("content") or payload.get("message") or "Agent failed")
        run = repository.get_run(run_id)
        if run is None:
            return "", model_name, "Agent run disappeared."
        if run["status"] in {"completed", "failed", "cancelled"}:
            if run.get("result_message_id"):
                with SessionLocal() as db:
                    message = db.get(AiChatMessage, int(run["result_message_id"]))
                    if message is not None:
                        final_answer = message.content or final_answer
                        stream_error = message.error or stream_error
            return final_answer or "".join(answer_parts).strip(), model_name, run.get("error") or stream_error
        if run["status"] == "waiting_approval":
            return "", model_name, "Channel run is waiting for an approval that must be completed in the web console."
        if run["status"] == "waiting_children":
            time.sleep(0.25)
            continue
        time.sleep(0.25)
    return "", model_name, "Agent run timed out while waiting for a terminal result."
