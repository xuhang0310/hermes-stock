"""模拟盘接口：精简闭环（账户概览 / 市价买卖 / 成交流水 / 重置）。

新增端点，统一用 core.response 信封。模拟盘是研究演练能力，与回测同属
quant 研究域，鉴权用 agent:use；失败统一 400 + fail()（业务校验错误）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.response import fail, ok
from backend.app.paper import paper_service

router = APIRouter(prefix="/api/paper-trading", tags=["paper-trading"])


class PaperOrderIn(BaseModel):
    symbol: str = Field(min_length=2, max_length=24, description="标的代码，支持 000001.SZ / SH600000 等写法")
    side: str = Field(description="交易方向：buy / sell")
    quantity: int = Field(gt=0, description="成交数量（股）；买入须为 100 的整数倍")


@router.get("/account")
def paper_account(
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    return ok(paper_service.account_summary(context.user.id))


@router.post("/account/reset")
def paper_account_reset(
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    account = paper_service.reset_account(context.user.id)
    return ok(account, message="模拟账户已重置")


@router.post("/orders")
def paper_place_order(
    payload: PaperOrderIn,
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    try:
        result = paper_service.place_order(
            context.user.id, payload.symbol, payload.side, payload.quantity
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return ok(result, message="模拟成交成功")


@router.get("/orders")
def paper_orders(
    limit: int = Query(default=50, ge=1, le=200),
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    return ok({"items": paper_service.list_orders(context.user.id, limit=limit)})
