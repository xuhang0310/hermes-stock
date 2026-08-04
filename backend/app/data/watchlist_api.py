from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.app.agent_runtime.transaction_channels import (
    create_transaction_run,
    stream_transaction_run,
)
from backend.app.auth.auth import AuthContext, require_permission
from backend.app.data.deps import get_repo
from backend.app.data.repository import StockRepository
from backend.app.data.sse import sse_event


class WatchlistCreate(BaseModel):
    symbol: str = Field(min_length=2, max_length=16)
    group_name: str = "default"
    reason: str | None = None


class WatchlistAgentIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    model_config_id: int | None = None
    history: list[dict[str, str]] = Field(default_factory=list, max_length=8)
    pending_action: dict | None = None


router = APIRouter()


@router.get("/api/watchlist")
def watchlist(
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("dashboard:view")),
) -> list[dict]:
    return repository.for_user(context.user.id).watchlist()


@router.post("/api/watchlist")
def add_watchlist_item(
    payload: WatchlistCreate,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    try:
        return repository.for_user(context.user.id).add_watchlist_item(
            payload.symbol,
            payload.group_name,
            payload.reason,
            added_by=context.user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/watchlist/{item_id}")
def delete_watchlist_item(
    item_id: int,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    try:
        return repository.for_user(context.user.id).remove_watchlist_item(
            item_id,
            removed_by=context.user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/watchlist/agent/stream")
async def stream_watchlist_agent(
    payload: WatchlistAgentIn,
    request: Request,
    context: AuthContext = Depends(require_permission("data:write")),
) -> StreamingResponse:
    try:
        run = await asyncio.to_thread(
            create_transaction_run,
            user_id=context.user.id,
            user_label=context.user.username,
            code="watchlist-entry-agent",
            question=payload.instruction,
            model_config_id=payload.model_config_id,
            pending_action=payload.pending_action,
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def event_stream() -> AsyncIterator[str]:
        yield ": connected\n\n"
        try:
            async for event in stream_transaction_run(
                run["run_id"],
                code="watchlist-entry-agent",
                request=request,
            ):
                yield sse_event(event)
        except Exception as exc:  # noqa: BLE001
            yield sse_event({"type": "error", "message": str(exc)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")