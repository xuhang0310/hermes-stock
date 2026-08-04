from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.app.agents.position_agent import (
    _execute_position_tool,
    extract_position_ocr_arguments,
    run_position_agent,
    validate_position_rows,
    validate_position_symbol,
)
from backend.app.agents.position_import import decode_image_payload, parse_position_screenshot
from backend.app.agent_runtime.transaction_channels import (
    create_transaction_run,
    stream_transaction_run,
)
from backend.app.agent_runtime.transaction_state import transaction_state_service
from backend.app.auth.auth import AuthContext, require_permission
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
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("dashboard:view")),
) -> list[dict]:
    """买卖交易流水查询，按时间倒序。"""
    return repository.for_user(context.user.id).position_trades(
        symbol=symbol,
        account_id=account_id,
        limit=limit,
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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/positions/by-id/{position_id}")
def correct_position(
    position_id: int,
    payload: PositionCorrectionIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    user_repository = repository.for_user(context.user.id)
    current = next(
        (item for item in user_repository.positions() if int(item.get("id") or 0) == position_id),
        None,
    )
    if current is None:
        raise HTTPException(status_code=404, detail="持仓不存在或不属于当前用户")
    validation = validate_position_symbol(payload.symbol, payload.name)
    same_existing_symbol = (
        validation.get("normalized_symbol") == current.get("symbol")
        and validation.get("status") == "catalog_unavailable"
    )
    if not validation.get("valid") and not same_existing_symbol:
        raise HTTPException(status_code=400, detail=validation.get("message") or "股票代码校验失败")
    data = payload.model_dump()
    data["symbol"] = validation.get("normalized_symbol") or current["symbol"]
    data["name"] = payload.name.strip()
    try:
        return user_repository.correct_position_holding(position_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@router.post("/api/positions/ai-import")
def import_positions_from_image(
    payload: PositionAiImportIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    try:
        parsed = parse_position_screenshot(
            image_base64=payload.image_base64,
            mime_type=payload.mime_type,
            model_config_id=payload.model_config_id,
        )
        repository = repository.for_user(context.user.id)
        batch = repository.create_position_import_batch(
            source_name=parsed["model"].get("display_name") or parsed["model"].get("model_name") or "vision_model",
            image_mime_type=payload.mime_type,
            image_sha256=parsed.get("image_sha256"),
            raw_text=parsed.get("raw_text"),
            parsed_json=json.dumps(parsed.get("parsed"), ensure_ascii=False),
            created_by=context.user.username,
        )
        account_ctx = dict(parsed.get("account") or {})
        if payload.account_id:
            account_ctx["account_id"] = payload.account_id
        saved_rows = repository.import_position_rows(parsed.get("rows") or [], batch, account_ctx)
        repository.finish_position_import_batch(batch, saved_rows)
        return {
            "batch": {
                "id": batch.id,
                "status": batch.status,
                "image_sha256": batch.image_sha256,
                "created_at": batch.created_at.isoformat() if batch.created_at else None,
            },
            "model": parsed.get("model"),
            "rows": saved_rows,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/positions/ocr/recognize")
def recognize_position_ocr(
    payload: PositionOcrRecognizeIn,
    _context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    """Recognize one current screenshot and return a fixed JSON draft without writing data."""

    if not payload.mime_type.lower().startswith("image/"):
        raise HTTPException(status_code=400, detail="仅支持图片文件")
    try:
        _, clean_base64, image_sha256 = decode_image_payload(payload.image_base64)
        extracted = extract_position_ocr_arguments(
            user_content=[
                {
                    "type": "text",
                    "text": (
                        "识别这张 A 股券商持仓截图。只提取持仓明细行，不要提取资产汇总、"
                        "按钮或广告；返回完整持仓快照，每个标的只出现一次。截图没有显示股票"
                        "代码时，symbol 和 name 都填写截图中的证券名称，严禁凭记忆猜代码。"
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{payload.mime_type};base64,{clean_base64}",
                    },
                },
            ],
            model_config_id=payload.model_config_id,
        )
        arguments = extracted.get("arguments") or {}
        source_rows = arguments.get("rows")
        if not isinstance(source_rows, list) or not source_rows:
            raise ValueError("图片中没有识别到持仓明细")
        rows, validations = validate_position_rows(
            [dict(row) for row in source_rows if isinstance(row, dict)],
            prefer_name_on_conflict=True,
        )
        for row in rows:
            row["action"] = "replace"
        invalid_count = sum(1 for item in validations if not item.get("valid"))
        return {
            "schema_version": "position_ocr.v1",
            "image_sha256": image_sha256,
            "model": extracted.get("model") or {},
            "account": (
                arguments.get("account")
                if isinstance(arguments.get("account"), dict)
                else {}
            ),
            "as_of_date": arguments.get("as_of_date"),
            "rows": rows,
            "validations": validations,
            "valid_count": len(rows) - invalid_count,
            "invalid_count": invalid_count,
        }
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

    user_repository = repository.for_user(context.user.id)
    account_ids = {
        int(account["id"])
        for account in user_repository.list_position_accounts()
        if account.get("id") is not None
    }
    if payload.account_id not in account_ids:
        raise HTTPException(status_code=404, detail="持仓账户不存在或无权访问")
    arguments: dict = {
        "rows": [
            {**row, "action": "replace"}
            for row in payload.rows
            if isinstance(row, dict)
        ]
    }
    if payload.as_of_date:
        arguments["as_of_date"] = payload.as_of_date
    result = _execute_position_tool(
        repository=user_repository,
        name="save_position_holdings",
        args=arguments,
        created_by=context.user.username,
        source_name="持仓 OCR 人工确认",
        image_mime_type=payload.mime_type,
        image_sha256=payload.image_sha256,
        account_id=payload.account_id,
        instruction=None,
        trusted_pending_action=True,
    )
    if result.get("need_account"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    if result.get("need_review"):
        raise HTTPException(status_code=400, detail="OCR 结果尚未完成人工确认")
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    saved_count = int(result.get("saved_count") or 0)
    return {
        "schema_version": "position_ocr.v1",
        "batch_id": result.get("batch_id"),
        "status": result.get("status"),
        "saved_count": saved_count,
        "error_count": int(result.get("error_count") or 0),
        "rows": result.get("rows") or [],
        "summary": f"OCR 复核完成，已保存 {saved_count} 条持仓。",
    }


@router.post("/api/positions/agent-add")
def agent_add_positions(
    payload: PositionAgentAddIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    try:
        result = run_position_agent(
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
        return {
            "batch": None,
            "model": result.get("model"),
            "intent": result.get("intent"),
            "should_save": result.get("should_save"),
            "reply": result.get("reply"),
            "rows": result.get("rows") or [],
            "steps": result.get("steps") or [],
            "summary": result.get("summary") or result.get("reply"),
            "pending_action": result.get("pending_action"),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/positions/agent-validate-symbol")
def validate_agent_position_symbol(
    payload: PositionSymbolValidateIn,
    _context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    return validate_position_symbol(payload.symbol, payload.name)


@router.post("/api/positions/agent-confirm")
def confirm_agent_positions(
    payload: PositionAgentConfirmIn,
    repository: StockRepository = Depends(get_repo),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    arguments: dict = {"rows": payload.rows}
    if payload.account:
        arguments["account"] = payload.account
    if payload.as_of_date:
        arguments["as_of_date"] = payload.as_of_date
    result = _execute_position_tool(
        repository=repository.for_user(context.user.id),
        name="save_position_holdings",
        args=arguments,
        created_by=context.user.username,
        source_name="AgentScope OCR 人工复核",
        image_mime_type="image/ocr-reviewed",
        image_sha256=None,
        account_id=payload.account_id,
    )
    if result.get("need_account"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    saved_count = int(result.get("saved_count") or 0)
    if saved_count:
        transaction_state_service.clear_pending_for_user(
            context.user.id,
            "position-entry-agent",
        )
    return {
        "rows": result.get("rows") or [],
        "should_save": saved_count > 0,
        "summary": f"复核完成，已保存 {saved_count} 条持仓。",
    }


@router.delete("/api/positions/agent-draft")
def discard_agent_position_draft(
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    cleared = transaction_state_service.clear_pending_for_user(
        context.user.id,
        "position-entry-agent",
    )
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