"""全站统一量化选股 API（L1 市场环境 + L2–L6 分层选股 + 执行限价 + L7 风控股数 + 前瞻净值对账）。"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.database import SessionLocal
from backend.app.core.response import fail, ok
from backend.app.market import event_signal_sync, financial_pit_sync, index_sync
from backend.app.quant import expectation_gap, layered_selection, market_regime

router = APIRouter(prefix="/api/quant/selection", tags=["quant-selection"])


class LayeredSelectionQueryIn(BaseModel):
    plan_type: Literal["swing", "long", "event"] = Field(default="swing", description="策略周期档位")
    trade_date: str | None = Field(default=None, max_length=10, description="指定交易日 YYYYMMDD，缺省取最新截面")
    verdict_filter: Literal["action", "watch", "pass"] | None = Field(default=None, description="结论档位过滤")
    industry: str | None = Field(default=None, max_length=120, description="行业精确过滤")
    market: str | None = Field(default=None, max_length=24, description="交易所过滤 SH/SZ/BSE")
    boards: list[Literal["MAIN", "CHINEXT", "STAR", "BSE"]] | None = Field(
        default=None, description="账户可用板块权限过滤",
    )
    keyword: str | None = Field(default=None, max_length=40, description="代码或名称搜索")
    price_max: float | None = Field(default=None, gt=0, description="股价上限（元）")
    pe_max: float | None = Field(default=None, gt=0, description="正市盈率上限")
    total_mv_min: float | None = Field(default=None, ge=0, description="总市值下限（亿元）")
    total_mv_max: float | None = Field(default=None, ge=0, description="总市值上限（亿元）")
    capital: float | None = Field(default=200_000.0, gt=0, description="L7 计算使用的账户总预算（元）")
    risk_per_trade: float = Field(default=0.01, ge=0.001, le=0.05, description="单笔最大可承受亏损比例（默认 1%）")
    account_id: int | None = Field(default=None, description="关联真实持仓账户 ID（可选）")
    limit: int = Field(default=50, ge=1, le=200, description="返回数量上限")


class RebuildSnapshotsIn(BaseModel):
    trade_date: str | None = Field(default=None, max_length=10)
    sync_indices: bool = Field(default=False, description="是否同步宽基指数日线")
    sync_events: bool = Field(default=False, description="是否同步最近7日全市场重大公告")
    sync_financial_pit: bool = Field(default=False, description="是否增量同步最近季度 PIT 财报指标")


class ExpectationGapIn(BaseModel):
    symbol: str = Field(..., min_length=6, max_length=16, description="A 股代码")
    trade_date: str | None = Field(default=None, max_length=10)


@router.get("/regime")
def api_get_market_regime(
    trade_date: str | None = Query(default=None, max_length=10),
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    try:
        data = market_regime.get_latest_market_regime(trade_date=trade_date, compute_if_missing=True)
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return ok(data)


@router.post("/query")
def api_query_layered_selection(
    payload: LayeredSelectionQueryIn,
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    try:
        result = layered_selection.query_layered_selection(
            plan_type=payload.plan_type,
            trade_date=payload.trade_date,
            verdict_filter=payload.verdict_filter,
            industry=payload.industry,
            market=payload.market,
            boards=payload.boards,
            keyword=payload.keyword,
            price_max=payload.price_max,
            pe_max=payload.pe_max,
            total_mv_min=payload.total_mv_min,
            total_mv_max=payload.total_mv_max,
            capital=payload.capital,
            risk_per_trade=payload.risk_per_trade,
            account_id=payload.account_id,
            owner_user_id=context.user.id,
            limit=payload.limit,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return ok(result)


@router.get("/stock/{symbol}")
def api_inspect_stock_layered(
    symbol: str,
    trade_date: str | None = Query(default=None, max_length=10),
    capital: float | None = Query(default=200_000.0, gt=0),
    risk_per_trade: float = Query(default=0.01, ge=0.001, le=0.05),
    account_id: int | None = Query(default=None),
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    try:
        result = layered_selection.inspect_single_stock_layered(
            symbol,
            trade_date=trade_date,
            capital=capital,
            risk_per_trade=risk_per_trade,
            account_id=account_id,
            owner_user_id=context.user.id,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return ok(result)


@router.post("/expectation-gap")
def api_generate_expectation_gap(
    payload: ExpectationGapIn,
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    try:
        result = expectation_gap.generate_expectation_gap_thesis(
            payload.symbol,
            trade_date=payload.trade_date,
            persist=True,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return ok(result)


@router.post("/rebuild")
def api_rebuild_layered_snapshots(
    payload: RebuildSnapshotsIn,
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    aux: dict[str, dict] = {}
    if payload.sync_indices:
        aux["indices"] = index_sync.sync_benchmark_indices(end_date=payload.trade_date)
    if payload.sync_events:
        aux["events"] = event_signal_sync.sync_market_event_signals(end_date=payload.trade_date, days=7)
    if payload.sync_financial_pit:
        aux["financial_pit"] = financial_pit_sync.sync_financial_pit()

    with SessionLocal() as db:
        target = layered_selection._resolve_latest_trade_date(db, payload.trade_date)
        if not target:
            return JSONResponse(status_code=400, content=fail("本地尚无可用交易日行情，请先同步行情", code="no_data"))
        built = layered_selection.build_daily_selection_snapshots(db, trade_date=target)
        nav = layered_selection.update_shadow_nav_comparison(db, target)
        db.commit()

    return ok({**built, "shadow_nav": nav, "aux_sync": aux})


@router.get("/shadow-nav")
def api_list_shadow_nav(
    limit: int = Query(default=120, ge=5, le=500),
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    return ok({"series": layered_selection.list_shadow_nav_series(limit=limit)})
