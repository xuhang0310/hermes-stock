from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select, text

from backend.app.agent_runtime.agentscope_engine import select_engine
from backend.app.agent_runtime.repository import AgentRunRepository, agent_run_repository
from backend.app.core.database import SessionLocal
from backend.app.core.models import AiChatMessage, AiChatSession, DigitalHumanProfile, User
from backend.app.core.settings import settings
from backend.app.market.digital_human_tools import is_role_profile


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
    delegation_config: dict[str, Any] | None = None,
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
                text(
                    "SELECT id FROM position_accounts "
                    "WHERE owner_user_id = :owner_user_id AND is_default = 1 "
                    "ORDER BY id LIMIT 1"
                ),
                {"owner_user_id": int(user_id or chat_session.user_id)},
            )
    digital_context = None
    if profile is not None:
        digital_context = "\n".join(
            [
                f"当前数字人：{profile.name}",
                f"风格：{profile.avatar_tone}",
                f"职责：{profile.description}",
                # 研究角色的提示词由角色段落统一注入（digital_human_skill_prompt），这里只给其他数字人带上
                *([] if is_role_profile(profile.code) else [f"系统提示：{profile.system_prompt}"]),
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
        # The session records the last model used, including temporary fallbacks.
        "model_config_id": model_config_id,
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
        "delegation_config": delegation_config,
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
    hard_timeout_seconds: int | None = None,
    queue_timeout_seconds: int | None = None,
) -> tuple[str, str | None, str | None]:
    idle_timeout = max(30, timeout_seconds or settings.agent_run_timeout_seconds + 30)
    hard_timeout = max(idle_timeout, hard_timeout_seconds or idle_timeout)
    wait_started_at = time.monotonic()
    queue_deadline = wait_started_at + queue_timeout_seconds if queue_timeout_seconds is not None else None
    idle_deadline = wait_started_at + idle_timeout
    hard_deadline = wait_started_at + hard_timeout
    cursor = 0
    answer_parts: list[str] = []
    final_answer = ""
    model_name: str | None = None
    stream_error: str | None = None
    last_status: str | None = None
    last_heartbeat: str | None = None
    while time.monotonic() < (queue_deadline if queue_deadline is not None else idle_deadline):
        made_progress = False
        for event in repository.events_after(run_id, cursor, limit=1000):
            cursor = event.seq
            made_progress = True
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
        run_status = str(run.get("status") or "")
        # Only the initial queue wait gets a separate budget. A resumed run
        # retains its original execution deadline when it is requeued.
        if queue_deadline is not None and (run_status != "queued" or run.get("started_at")):
            queue_deadline = None
            execution_started_at = time.monotonic()
            idle_deadline = execution_started_at + idle_timeout
            hard_deadline = execution_started_at + hard_timeout
        heartbeat = str(run.get("heartbeat_at") or "") or None
        if run_status != last_status or (heartbeat is not None and heartbeat != last_heartbeat):
            made_progress = True
        last_status = run_status
        last_heartbeat = heartbeat
        if queue_deadline is None and made_progress:
            idle_deadline = min(hard_deadline, time.monotonic() + idle_timeout)
        if run_status in {"completed", "failed", "cancelled"}:
            if run.get("result_message_id"):
                with SessionLocal() as db:
                    message = db.get(AiChatMessage, int(run["result_message_id"]))
                    if message is not None:
                        final_answer = message.content or final_answer
                        stream_error = message.error or stream_error
            return final_answer or "".join(answer_parts).strip(), model_name, run.get("error") or stream_error
        if run_status == "waiting_approval":
            return "", model_name, "Channel run is waiting for an approval that must be completed in the web console."
        if run_status == "waiting_children":
            time.sleep(0.25)
            continue
        time.sleep(0.25)
    if queue_deadline is not None:
        return "", model_name, "Agent run timed out while waiting in the worker queue."
    return "", model_name, "Agent run timed out while waiting for a terminal result."
