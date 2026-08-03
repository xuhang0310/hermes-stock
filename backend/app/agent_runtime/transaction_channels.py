from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from backend.app.agent_runtime.channel_runs import build_channel_request, create_channel_run
from backend.app.agent_runtime.repository import agent_run_repository
from backend.app.agent_runtime.transaction_state import transaction_state_service
from backend.app.core.database import SessionLocal
from backend.app.core.llm_config import (
    resolve_position_agent_model_config_id,
    resolve_vision_model_config_id,
)
from backend.app.core.models import (
    AiChatMessage,
    AiChatSession,
    AgentToolExecution,
    DigitalHumanProfile,
)
from backend.app.data.repository import StockRepository


_DATA_URL = re.compile(r"^data:(?P<mime>[-\w.+/]+);base64,(?P<data>.+)$", re.DOTALL)


def _store_inline_image(user_id: int, image_base64: str, fallback_mime: str) -> tuple[dict[str, Any], str]:
    match = _DATA_URL.match(image_base64.strip())
    mime_type = match.group("mime") if match else fallback_mime
    encoded = match.group("data") if match else image_base64
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("Invalid base64 image payload.") from exc
    if len(raw) > 15 * 1024 * 1024:
        raise ValueError("Image exceeds the 15MB transaction Agent limit.")
    extension = mimetypes.guess_extension(mime_type) or ".png"
    if extension == ".jpe":
        extension = ".jpg"
    rel_path = f"uploads/transactions/{uuid.uuid4().hex}{extension}"
    from backend.app.skills.agent_workspace import _resolve_in_jail, workspace_user_scope

    with workspace_user_scope(user_id):
        target = _resolve_in_jail(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    return (
        {
            "path": rel_path,
            "name": Path(rel_path).name,
            "mime_type": mime_type,
            "bytes": len(raw),
            "kind": "image",
        },
        digest,
    )


def _open_transaction_session(
    *,
    user_id: int,
    code: str,
    question: str,
    model_config_id: int | None,
    client_session_id: str | None = None,
) -> tuple[int, list[dict[str, str]]]:
    with SessionLocal() as db:
        profile = db.scalar(
            select(DigitalHumanProfile).where(DigitalHumanProfile.code == code).limit(1)
        )
        if profile is None:
            raise ValueError(f"Transaction Agent profile does not exist: {code}")
        external_id = f"transaction:{user_id}:{code}"
        if client_session_id:
            external_id = f"{external_id}:{client_session_id}"
        chat_session = db.scalar(
            select(AiChatSession)
            .where(
                AiChatSession.user_id == user_id,
                AiChatSession.digital_human_id == profile.id,
                AiChatSession.source_type == "web",
                AiChatSession.external_conversation_id == external_id,
            )
            .limit(1)
        )
        if chat_session is None:
            chat_session = AiChatSession(
                user_id=user_id,
                digital_human_id=profile.id,
                model_config_id=model_config_id,
                title=("持仓录入 Agent" if code == "position-entry-agent" else "自选录入 Agent"),
                status="active",
                chat_mode="research",
                source_type="web",
                external_conversation_id=external_id,
                source_label="网页",
            )
            db.add(chat_session)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                chat_session = db.scalar(
                    select(AiChatSession)
                    .where(
                        AiChatSession.digital_human_id == profile.id,
                        AiChatSession.source_type == "web",
                        AiChatSession.external_conversation_id == external_id,
                    )
                    .limit(1)
                )
                if chat_session is None:
                    raise
        rows = db.scalars(
            select(AiChatMessage)
            .where(AiChatMessage.session_id == chat_session.id, AiChatMessage.error.is_(None))
            .order_by(desc(AiChatMessage.id))
            .limit(16)
        ).all()
        history = [
            {"role": row.role, "content": row.content}
            for row in reversed(rows)
            if row.role in {"user", "assistant"} and row.content
        ]
        if model_config_id:
            chat_session.model_config_id = model_config_id
        db.commit()
        return chat_session.id, history


def create_transaction_run(
    *,
    user_id: int,
    user_label: str,
    code: str,
    question: str,
    model_config_id: int | None,
    account_id: int | None = None,
    pending_action: dict[str, Any] | None = None,
    image_base64: str | None = None,
    mime_type: str = "image/png",
    client_session_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    existing = agent_run_repository.get_by_idempotency_key(user_id, idempotency_key)
    if existing is not None:
        return {**existing, "created": False}
    if code == "position-entry-agent":
        try:
            model_config_id = resolve_position_agent_model_config_id(model_config_id)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
    requested_model_config_id = model_config_id
    if image_base64:
        try:
            model_config_id = resolve_vision_model_config_id(model_config_id)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
    session_id, history = _open_transaction_session(
        user_id=user_id,
        code=code,
        question=question,
        model_config_id=model_config_id,
        client_session_id=client_session_id,
    )
    active_run = agent_run_repository.active_run_for_session(user_id, session_id)
    if active_run is not None:
        return {**active_run, "created": False, "duplicate_active": True}
    attachments: list[dict[str, Any]] = []
    image_sha256 = None
    if image_base64:
        attachment, image_sha256 = _store_inline_image(user_id, image_base64, mime_type)
        attachments.append(attachment)
    with SessionLocal() as db:
        message = AiChatMessage(
            session_id=session_id,
            role="user",
            content=question,
            attachments_json=json.dumps(attachments, ensure_ascii=False) if attachments else None,
            event_count=0,
        )
        db.add(message)
        db.commit()
        message_id = message.id
    request_payload = build_channel_request(
        session_id=session_id,
        question=question,
        history=history,
        digital_human_code=code,
        source_type="web",
        source_ref=f"transaction:{message_id}",
        user_id=user_id,
        user_label=user_label,
        model_config_id=model_config_id,
        attachments=attachments,
        default_account_id=account_id,
        pending_action=pending_action,
    )
    request_payload["image_mime_type"] = mime_type
    request_payload["image_sha256"] = image_sha256
    request_payload["requested_model_config_id"] = requested_model_config_id
    request_payload["vision_model_routed"] = bool(
        image_base64 and model_config_id != requested_model_config_id
    )
    run = create_channel_run(
        request_payload,
        idempotency_key=idempotency_key or f"transaction:{message_id}",
    )
    if not run["created"]:
        # A concurrent retry may win the unique idempotency race after this
        # request stored its message. Keep one canonical user turn.
        with SessionLocal() as db:
            duplicate = db.get(AiChatMessage, message_id)
            if duplicate is not None:
                db.delete(duplicate)
                db.commit()
        if attachments:
            from backend.app.skills.agent_workspace import _resolve_in_jail, workspace_user_scope

            with workspace_user_scope(user_id):
                for attachment in attachments:
                    _resolve_in_jail(str(attachment["path"])).unlink(missing_ok=True)
    return run


def _transaction_result(run_id: str, code: str) -> dict[str, Any]:
    run = agent_run_repository.get_run(run_id)
    answer = ""
    error = (run or {}).get("error")
    session_id = int((run or {}).get("session_id") or 0)
    user_id = int((run or {}).get("user_id") or 0)
    with SessionLocal() as db:
        if run and run.get("result_message_id"):
            message = db.get(AiChatMessage, int(run["result_message_id"]))
            if message:
                answer = message.content or ""
                error = message.error or error
        executions = db.scalars(
            select(AgentToolExecution)
            .where(AgentToolExecution.run_id == run_id, AgentToolExecution.result_json.is_not(None))
            .order_by(
                AgentToolExecution.created_at.desc(),
                AgentToolExecution.execution_id.desc(),
            )
        ).all()
        tool_result: dict[str, Any] = {}
        for execution in executions:
            try:
                parsed = json.loads(execution.result_json or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and (
                execution.tool_name in {"save_position_holdings", "add_watchlist_items", "remove_watchlist_items"}
            ):
                tool_result = parsed
                break
        if code == "position-entry-agent":
            rows = tool_result.get("rows") or []
            changed = int(tool_result.get("saved_count") or 0)
            if changed:
                failed = int(tool_result.get("error_count") or 0)
                batch_id = tool_result.get("batch_id")
                tool_summary = (
                    f"持仓入库完成：成功 {changed} 条，失败 {failed} 条"
                    + (f"（批次 {batch_id}）" if batch_id else "")
                    + "。"
                )
            else:
                tool_summary = str(tool_result.get("message") or tool_result.get("error") or "")
        else:
            rows = tool_result.get("watchlist") or (
                StockRepository(db, owner_user_id=user_id).watchlist() if user_id else []
            )
            changed = int(tool_result.get("changed_count") or 0)
            tool_summary = str(tool_result.get("message") or tool_result.get("error") or "")
    authoritative_answer = tool_summary or answer
    state = transaction_state_service.load(session_id, code) if session_id else None
    return {
        "reply": authoritative_answer,
        "summary": authoritative_answer,
        "rows": rows,
        "should_save": changed > 0,
        "pending_action": (state or {}).get("pending_action"),
        "status": (run or {}).get("status"),
        "error": error,
    }


async def stream_transaction_run(
    run_id: str,
    *,
    code: str,
    request: Request,
) -> AsyncIterator[dict[str, Any]]:
    cursor = 0
    latest_observation = ""
    while True:
        events = await asyncio.to_thread(agent_run_repository.events_after, run_id, cursor)
        for stored in events:
            cursor = stored.seq
            event = stored.payload
            event_type = stored.event_type
            if event_type == "model_selected":
                model = event.get("model") or {}
                yield {
                    "type": "model_selected",
                    "model": model,
                    "step": {
                        "type": "model",
                        "content": f"使用 {model.get('display_name') or model.get('model_name') or '模型'} 处理。",
                    },
                }
            elif event_type in {"reasoning_delta", "stage_start", "retrying"}:
                yield {
                    "type": "thinking",
                    "message": event.get("message") or event.get("content") or "Agent 正在处理...",
                }
            elif event_type == "content_delta":
                yield {"type": "answer_delta", "content": event.get("content") or ""}
            elif event_type == "action":
                yield {
                    "type": "tool_start",
                    "tool": event.get("tool"),
                    "display_name": event.get("display_name"),
                    "step": {
                        "type": "action",
                        "tool": event.get("tool"),
                        "display_name": event.get("display_name"),
                        "content": "正在执行结构化业务工具",
                    },
                }
            elif event_type == "observation":
                latest_observation = str(event.get("content") or "")
            elif event_type == "tool_done":
                yield {
                    "type": "tool_done",
                    "tool": event.get("tool"),
                    "display_name": event.get("display_name"),
                    "step": {
                        "type": "observation" if event.get("success") else "error",
                        "tool": event.get("tool"),
                        "display_name": event.get("display_name"),
                        "content": latest_observation or "工具执行完成",
                        "success": bool(event.get("success")),
                    },
                }
            elif event_type == "error":
                yield {"type": "error", "message": event.get("content") or "Agent 执行失败"}
            elif event_type == "done":
                result = await asyncio.to_thread(_transaction_result, run_id, code)
                if result.get("error") and not result.get("reply"):
                    yield {"type": "error", "message": result["error"]}
                yield {"type": "done", **result, "duration_ms": event.get("duration_ms")}
                return
        if await request.is_disconnected():
            return
        run = await asyncio.to_thread(agent_run_repository.get_run, run_id)
        if run is None:
            yield {"type": "error", "message": "Agent run no longer exists."}
            return
        if run["status"] in {"completed", "failed", "cancelled"} and cursor >= int(run["last_event_seq"]):
            result = await asyncio.to_thread(_transaction_result, run_id, code)
            yield {"type": "done", **result}
            return
        await asyncio.sleep(0.25)
