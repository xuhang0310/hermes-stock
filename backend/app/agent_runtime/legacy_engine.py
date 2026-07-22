from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from backend.app.agent_runtime.contracts import EngineOutcome
from backend.app.agent_runtime.payloads import materialize_openai_content
from backend.app.agents.stock_agent import run_agent_events


Emit = Callable[[dict[str, Any]], Awaitable[None]]
Cancelled = Callable[[], Awaitable[bool]]


async def run_legacy(
    *,
    request_payload: dict[str, Any],
    emit: Emit,
    is_cancelled: Cancelled,
) -> EngineOutcome:
    """Run the existing generator outside the event loop.

    This remains the rollback engine and preserves every current tool and
    prompt behavior as an explicit configuration rollback path.
    """

    queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue(maxsize=1000)
    done = object()
    loop = asyncio.get_running_loop()
    cancel_event = threading.Event()

    def produce() -> None:
        try:
            for event in run_agent_events(
                str(request_payload.get("question") or ""),
                list(request_payload.get("history") or []),
                request_payload.get("digital_human_context"),
                request_payload.get("model_config_id"),
                str(request_payload.get("chat_mode") or "research"),
                list(request_payload.get("skills") or []),
                request_payload.get("digital_human_code"),
                str(request_payload.get("memory_context") or ""),
                user_id=int(request_payload["user_id"]),
                user_label=str(request_payload.get("user_label") or "unknown"),
                can_write_positions=bool(request_payload.get("can_write_positions")),
                default_account_id=request_payload.get("default_account_id"),
                allow_code_execution=bool(request_payload.get("allow_code_execution")),
                approval_policy=request_payload.get("approval_policy") or "ask",
                session_id=int(request_payload["session_id"]),
                user_content=materialize_openai_content(request_payload),
                cancel_event=cancel_event,
            ):
                if cancel_event.is_set():
                    break
                asyncio.run_coroutine_threadsafe(queue.put(event), loop).result()
        except Exception as exc:  # noqa: BLE001
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "error", "content": str(exc)}), loop
            ).result()
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(done), loop).result()

    producer = asyncio.create_task(asyncio.to_thread(produce))
    outcome = EngineOutcome()
    content_buffer: list[str] = []
    while True:
        if await is_cancelled():
            cancel_event.set()
            outcome.status = "cancelled"
            break
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1)
        except TimeoutError:
            continue
        if event is done:
            break
        assert isinstance(event, dict)
        if event.get("type") == "content_delta":
            content_buffer.append(str(event.get("content") or ""))
        elif event.get("type") == "content_end":
            if event.get("role") == "answer":
                outcome.final_answer = "".join(content_buffer).strip()
            content_buffer.clear()
        elif event.get("type") == "error":
            outcome.status = "failed"
            outcome.error = str(event.get("content") or "Legacy Agent failed")
        await emit(event)
    if outcome.status == "completed" and not outcome.final_answer and content_buffer:
        outcome.final_answer = "".join(content_buffer).strip()
    if not producer.done():
        cancel_event.set()
    await producer
    return outcome
