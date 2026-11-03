from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.app.agent_runtime.transaction_channels import (
    create_transaction_run,
    stream_transaction_run,
)
from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.response import fail, ok
from backend.app.data import position_sync, positions_service
from backend.app.data.errors import PositionNotFoundError
from backend.app.data.deps import get_repo
from backend.app.data.repository import StockRepository
from backend.app.data.sse import sse_event


class PositionAccountIn(BaseModel):
    broker_code: str = Field(default="manual", max_length=40)
    broker_name: str | None = Field(default=None, max_length=120)
    account_name: str | None = Field(default=None, max_length=120)
    account_no_masked: str | None = Field(default=None, max_length=80)
    account_type: str = Field(default="cash", max_length=40)
    currency: str = Field(default="CNY", max_length=12)
    is_default: bool = False
    remark: str | None = None


class PositionAccountPatchIn(BaseModel):
    account_name: str | None = Field(default=None, max_length=120)
    account_no_masked: str | None = Field(default=None, max_length=80)
    account_type: str | None = Field(default=None, max_length=40)
    status: str | None = Field(default=None, max_length=24)
    remark: str | None = None
    sort_order: int | None = None
    is_default: bool | None = None


class AccountFundsIn(BaseModel):
    # 用户维护"可用现金"（券商 App 现金数），系统算 total_asset = 可用现金 + 持仓市值
    available_cash: float | None = Field(default=None, ge=0)


class PositionHoldingIn(BaseModel):
    symbol: str = Field(min_length=2, max_length=24)
    name: str | None = Field(default=None, max_length=120)
    account_id: int | None = None
    broker_code: str | None = Field(default=None, max_length=40)
    broker_name: str | None = Field(default=None, max_length=120)
    account_name: str | None = Field(default=None, max_length=120)
    account_no_masked: str | None = Field(default=None, max_length=80)
    account_type: str | None = Field(default="cash", max_length=40)
    as_of_date: str | None = Field(default=None, max_length=16)
    position_quantity: float = Field(default=0, ge=0)
    available_quantity: float | None = Field(default=None, ge=0)
    frozen_quantity: float | None = Field(default=None, ge=0)
    today_buy_quantity: float | None = Field(default=None, ge=0)
    today_sell_quantity: float | None = Field(default=None, ge=0)
    cost_price: float | None = Field(default=None, ge=0)
    execution_price: float | None = Field(default=None, ge=0)
    cost_amount: float | None = Field(default=None, ge=0)
    latest_price: float | None = Field(default=None, ge=0)
    market_value: float | None = Field(default=None, ge=0)
    floating_pnl: float | None = None
    floating_pnl_pct: float | None = None
    day_pnl: float | None = None
    position_pct: float | None = Field(default=None, ge=0)
    stop_price: float | None = Field(default=None, ge=0)
    take_profit_price: float | None = Field(default=None, ge=0)
    thesis: str | None = None
    risk_note: str | None = None
    status: str | None = Field(default=None, max_length=24)
    action: str | None = Field(default=None, max_length=24, description="交易动作: buy/sell/add/reduce/close/replace/update_field")


class PositionCorrectionIn(BaseModel):
    symbol: str = Field(min_length=2, max_length=24)
    name: str = Field(min_length=1, max_length=120)
    position_quantity: float = Field(ge=0)
    cost_price: float = Field(ge=0)


class PositionAiImportIn(BaseModel):
    image_base64: str = Field(min_length=100)
    mime_type: str = Field(default="image/png", max_length=80)
    model_config_id: int | None = None
    account_id: int | None = None


class PositionOcrRecognizeIn(BaseModel):
    image_base64: str = Field(min_length=100)
    mime_type: str = Field(default="image/png", max_length=80)
    model_config_id: int | None = None


class PositionOcrConfirmIn(BaseModel):
    rows: list[dict] = Field(min_length=1, max_length=100)
    account_id: int = Field(ge=1)
    as_of_date: str | None = Field(default=None, max_length=32)
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: str = Field(default="image/png", max_length=80)


class PositionAgentAddIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=2000)
    image_base64: str | None = Field(default=None, min_length=100)
    mime_type: str = Field(default="image/png", max_length=80)
    account_id: int | None = Field(default=None, ge=1)
    model_config_id: int | None = None
    client_session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    history: list[dict[str, str]] = Field(default_factory=list, max_length=8)
    pending_action: dict | None = None


class PositionSymbolValidateIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    name: str | None = Field(default=None, max_length=120)


class PositionSyncImageIn(BaseModel):
    image_base64: str = Field(min_length=100)
    mime_type: str = Field(default="image/png", max_length=80)
    name: str | None = Field(default=None, max_length=200)


class PositionSyncRecognizeIn(BaseModel):
    images: list[PositionSyncImageIn] = Field(min_length=1, max_length=position_sync.MAX_SYNC_IMAGES)
    account_id: int | None = Field(default=None, ge=1)
    model_config_id: int | None = None


class PositionSyncPreviewIn(BaseModel):
    account_id: int = Field(ge=1)
    rows: list[dict] = Field(default_factory=list, max_length=300)
    funds: dict | None = None


class PositionSyncApplyIn(BaseModel):
    account_id: int = Field(ge=1)
    items: list[dict] = Field(default_factory=list, max_length=300)
    funds: dict | None = None
    apply_funds: bool = True
    as_of_date: str | None = Field(default=None, max_length=16)
    image_sha256: str | None = Field(default=None, max_length=80)
    account_no_masked: str | None = Field(default=None, max_length=80)


class PositionAgentConfirmIn(BaseModel):
    rows: list[dict] = Field(min_length=1, max_length=100)
    account: dict | None = None
    as_of_date: str | None = Field(default=None, max_length=32)
    account_id: int | None = Field(default=None, ge=1)


router = APIRouter()


@router.get("/api/positions")
def positions(
    account_id: int | None = Query(default=None),
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("dashboard:view")),
) -> list[dict]:
    return repository.for_user(context.user.id).positions(account_id)


@router.get("/api/positions/brokers")
def position_brokers(
    _context: AuthContext = Depends(require_permission("dashboard:view")),
) -> list[dict]:
    from backend.app.data.broker_presets import BROKER_PRESETS

    return BROKER_PRESETS


@router.get("/api/positions/accounts")
def position_accounts(
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("dashboard:view")),
) -> list[dict]:
    return repository.for_user(context.user.id).list_position_accounts()


@router.post("/api/positions/accounts")
def create_position_account(
    payload: PositionAccountIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    return repository.for_user(context.user.id).create_position_account(payload.model_dump())


@router.patch("/api/positions/accounts/{account_id}")
def update_position_account(
    account_id: int,
    payload: PositionAccountPatchIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    try:
        return repository.for_user(context.user.id).update_position_account(
            account_id,
            payload.model_dump(exclude_none=True),
        )
    except positions_service.PositionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/positions/accounts/{account_id}/funds")
def update_account_funds(
    account_id: int,
    payload: AccountFundsIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    try:
        return repository.for_user(context.user.id).update_account_funds(
            account_id,
            payload.available_cash,
        )
    except positions_service.PositionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/positions/summary")
def positions_summary(
    account_id: int | None = Query(default=None),
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("dashboard:view")),
) -> dict:
    return repository.for_user(context.user.id).positions_summary(account_id)


@router.get("/api/positions/trades")
def position_trades(
    symbol: str | None = Query(default=None),
    account_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    include_imports: bool = Query(default=False, description="是否连截图导入时生成的记录一起列出"),
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("dashboard:view")),
) -> list[dict]:
    """买卖交易流水查询，按时间倒序。默认只列真实成交。"""
    return repository.for_user(context.user.id).position_trades(
        symbol=symbol,
        account_id=account_id,
        limit=limit,
        include_imports=include_imports,
    )


@router.delete("/api/positions/trades/{trade_id}")
def delete_position_trade(
    trade_id: int,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    """删单条交易流水（不回滚持仓，仅删流水行）。用于删错单。"""
    result = repository.for_user(context.user.id).delete_position_trade(trade_id)
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail="交易流水不存在")
    return result


@router.get("/api/chart/positions/{symbol}")
def position_chart(
    symbol: str,
    limit: int = Query(default=240, ge=30, le=1000),
    refresh: bool = Query(default=False),
    resolution: str = Query(default="1D", max_length=8),
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("dashboard:view")),
) -> dict:
    try:
        return repository.for_user(context.user.id).position_chart(
            symbol,
            limit=limit,
            refresh=refresh,
            resolution=resolution,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/positions")
def add_position(
    payload: PositionHoldingIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    try:
        data = payload.model_dump(exclude_none=True)
        action = data.pop("action", None)
        return repository.for_user(context.user.id).merge_position_holding(
            data,
            action=action,
            source_type="manual",
            commit=True,
        )
    except positions_service.PositionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/positions/by-id/{position_id}")
def correct_position(
    position_id: int,
    payload: PositionCorrectionIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    try:
        return positions_service.correct_position(
            repository=repository.for_user(context.user.id),
            position_id=position_id,
            symbol=payload.symbol,
            name=payload.name,
            position_quantity=payload.position_quantity,
            cost_price=payload.cost_price,
        )
    except positions_service.PositionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/api/positions/{symbol}")
def delete_position(
    symbol: str,
    account_id: int | None = Query(default=None),
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    """物理删除一条持仓记录及其历史快照（不可逆）。用于录错重来的场景。"""
    try:
        result = repository.for_user(context.user.id).delete_position(
            symbol,
            account_id=account_id,
        )
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/positions/price-sync")
def sync_positions_price(
    _context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    """一键刷新全部 open 持仓的最新价/市值/浮盈并重算账户资金。

    复用定时任务「持仓价格分钟同步」的同步实现；已有同步在执行时原样返回
    skipped 提示（200），行情源失败返回 502。
    """
    from backend.app.scheduler.rhythm_scheduler import sync_position_prices

    result = sync_position_prices()
    if result["status"] == "failed":
        raise HTTPException(status_code=502, detail=result["message"])
    return result


@router.post("/api/positions/ai-import")
def import_positions_from_image(
    payload: PositionAiImportIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    try:
        return positions_service.import_position_rows_from_image(
            repository=repository.for_user(context.user.id),
            image_base64=payload.image_base64,
            mime_type=payload.mime_type,
            model_config_id=payload.model_config_id,
            account_id=payload.account_id,
            created_by=context.user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _sync_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, PositionNotFoundError):
        return JSONResponse(status_code=404, content=fail(str(exc), code="not_found"))
    if isinstance(exc, ValueError):
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return JSONResponse(status_code=502, content=fail(str(exc), code="upstream_error"))


@router.post("/api/positions/sync/recognize")
def recognize_position_sync(
    payload: PositionSyncRecognizeIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
):
    """持仓同步第 1 步：识别多张截图并合并；能确定账户时同时给出变化清单。不写库。"""
    try:
        data = position_sync.recognize_sync(
            repository=repository.for_user(context.user.id),
            images=[image.model_dump() for image in payload.images],
            account_id=payload.account_id,
            model_config_id=payload.model_config_id,
        )
    except (PositionNotFoundError, ValueError, RuntimeError) as exc:
        return _sync_error(exc)
    return ok(data)


@router.post("/api/positions/sync/preview")
def preview_position_sync(
    payload: PositionSyncPreviewIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
):
    """换账户或改正代码后重新生成变化清单，不再调用视觉模型。不写库。"""
    try:
        data = repository.for_user(context.user.id).position_sync_preview(
            account_id=payload.account_id,
            rows=payload.rows,
            funds=payload.funds,
        )
    except (PositionNotFoundError, ValueError) as exc:
        return _sync_error(exc)
    return ok(data)


@router.post("/api/positions/sync/apply")
def apply_position_sync(
    payload: PositionSyncApplyIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
):
    """持仓同步第 2 步：按用户勾选一次写入；同步是校正，不写交易流水（勾选了买卖的除外）。"""
    try:
        data = position_sync.apply_sync(
            repository=repository.for_user(context.user.id),
            account_id=payload.account_id,
            items=payload.items,
            funds=payload.funds,
            apply_funds=payload.apply_funds,
            as_of_date=payload.as_of_date,
            image_sha256=payload.image_sha256,
            account_no_masked=payload.account_no_masked,
            created_by=context.user.username,
        )
    except (PositionNotFoundError, ValueError) as exc:
        return _sync_error(exc)
    return ok(data, message=data.get("message") or "ok")


@router.post("/api/positions/sync/{batch_id}/undo")
def undo_position_sync(
    batch_id: int,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
):
    """撤销一次持仓同步：持仓和可用资金回到同步前。之后账户又有改动时拒绝。"""
    try:
        data = repository.for_user(context.user.id).undo_position_sync(batch_id)
    except (PositionNotFoundError, ValueError) as exc:
        return _sync_error(exc)
    return ok(data, message=data.get("message") or "ok")


@router.post("/api/positions/ocr/recognize")
def recognize_position_ocr(
    payload: PositionOcrRecognizeIn,
    _context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    """Recognize one current screenshot and return a fixed JSON draft without writing data."""
    try:
        return positions_service.recognize_position_ocr(
            image_base64=payload.image_base64,
            mime_type=payload.mime_type,
            model_config_id=payload.model_config_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/positions/ocr/confirm")
def confirm_position_ocr(
    payload: PositionOcrConfirmIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    """Persist a user-reviewed OCR draft; all symbols are validated again server-side."""
    try:
        return positions_service.confirm_position_ocr(
            repository=repository.for_user(context.user.id),
            rows=payload.rows,
            account_id=payload.account_id,
            as_of_date=payload.as_of_date,
            image_sha256=payload.image_sha256,
            mime_type=payload.mime_type,
            created_by=context.user.username,
        )
    except positions_service.PositionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/positions/agent-add")
def agent_add_positions(
    payload: PositionAgentAddIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    try:
        return positions_service.agent_add_positions(
            repository=repository.for_user(context.user.id),
            instruction=payload.instruction,
            image_base64=payload.image_base64,
            mime_type=payload.mime_type,
            model_config_id=payload.model_config_id,
            created_by=context.user.username,
            account_id=payload.account_id,
            history=payload.history,
            pending_action=payload.pending_action,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/positions/agent-validate-symbol")
def validate_agent_position_symbol(
    payload: PositionSymbolValidateIn,
    _context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    return positions_service.validate_position_symbol(payload.symbol, payload.name)


@router.post("/api/positions/agent-confirm")
def confirm_agent_positions(
    payload: PositionAgentConfirmIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    try:
        return positions_service.confirm_agent_positions(
            repository=repository.for_user(context.user.id),
            rows=payload.rows,
            account=payload.account,
            as_of_date=payload.as_of_date,
            account_id=payload.account_id,
            user_id=context.user.id,
            created_by=context.user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/api/positions/agent-draft")
def discard_agent_position_draft(
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    cleared = positions_service.discard_agent_position_draft(context.user.id)
    return {"cleared": cleared}


@router.post("/api/positions/agent-add/stream")
async def stream_agent_add_positions(
    payload: PositionAgentAddIn,
    request: Request,
    context: AuthContext = Depends(require_permission("data:write")),
) -> StreamingResponse:
    try:
        run = await asyncio.to_thread(
            create_transaction_run,
            user_id=context.user.id,
            user_label=context.user.username,
            code="position-entry-agent",
            question=payload.instruction,
            model_config_id=payload.model_config_id,
            account_id=payload.account_id,
            pending_action=payload.pending_action,
            image_base64=payload.image_base64,
            mime_type=payload.mime_type,
            client_session_id=payload.client_session_id,
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def event_stream() -> AsyncIterator[str]:
        yield ": connected\n\n"
        try:
            async for event in stream_transaction_run(
                run["run_id"],
                code="position-entry-agent",
                request=request,
            ):
                yield sse_event(event)
        except Exception as exc:  # noqa: BLE001
            yield sse_event({"type": "error", "message": str(exc)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")