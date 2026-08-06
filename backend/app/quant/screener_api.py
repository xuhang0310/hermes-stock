"""技术面选股器接口：筛选 / 全市场同步 / AI 解析 / AI 解读。

统一用 core.response 信封；鉴权用 require_permission：筛选与同步是只读研究
能力（data:read），AI 解析/解读消耗 LLM（agent:use）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.response import fail, ok
from backend.app.market import market_sync
from backend.app.quant import screener_ai, screener_service

router = APIRouter(prefix="/api/screener", tags=["screener"])


class ScreenerQueryIn(BaseModel):
    market: str | None = Field(default=None, max_length=24, description="市场/交易所过滤")
    stock_type: str | None = Field(default="stock", max_length=24, description="标的类型，默认仅股票")
    price_min: float | None = Field(default=None, ge=0, description="最新价下限（含）")
    price_max: float | None = Field(default=None, ge=0, description="最新价上限（含）")
    pct_chg_min: float | None = Field(default=None, description="当日涨跌幅下限 %（含）")
    pct_chg_max: float | None = Field(default=None, description="当日涨跌幅上限 %（含）")
    keyword: str | None = Field(default=None, max_length=32, description="代码/名称模糊匹配")
    volume_ratio_min: float | None = Field(default=None, gt=0, description="量比下限（官方量比，daily_basic）")
    turnover_rate_min: float | None = Field(default=None, ge=0, description="换手率下限 %（含）")
    turnover_rate_max: float | None = Field(default=None, ge=0, description="换手率上限 %（含）")
    total_mv_min: float | None = Field(default=None, ge=0, description="总市值下限（亿元，含）")
    total_mv_max: float | None = Field(default=None, ge=0, description="总市值上限（亿元，含）")
    pe_min: float | None = Field(default=None, description="市盈率 TTM 下限（含）")
    pe_max: float | None = Field(default=None, description="市盈率 TTM 上限（含）")
    above_ma: int | None = Field(default=None, description="收盘须站上该周期均线（仅 5/10/20）")
    limit: int = Field(default=50, ge=1, le=100, description="返回条数上限")


@router.post("/run")
def screener_run(
    payload: ScreenerQueryIn,
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    try:
        result = screener_service.run_screener(
            market=payload.market,
            stock_type=payload.stock_type,
            price_min=payload.price_min,
            price_max=payload.price_max,
            pct_chg_min=payload.pct_chg_min,
            pct_chg_max=payload.pct_chg_max,
            keyword=payload.keyword,
            volume_ratio_min=payload.volume_ratio_min,
            turnover_rate_min=payload.turnover_rate_min,
            turnover_rate_max=payload.turnover_rate_max,
            total_mv_min=payload.total_mv_min,
            total_mv_max=payload.total_mv_max,
            pe_min=payload.pe_min,
            pe_max=payload.pe_max,
            above_ma=payload.above_ma,
            limit=payload.limit,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return ok(result)


class SyncIn(BaseModel):
    days: int = Field(default=20, ge=5, le=60, description="同步最近 N 个交易日")


@router.post("/sync")
def screener_sync(
    payload: SyncIn,
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    """全市场增量同步（幂等）；上游错误原文透出，便于定位 TuShare 积分/权限问题。"""
    try:
        stats = market_sync.sync_market(payload.days)
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content=fail(str(exc), code="upstream_error"))
    return ok(stats)


@router.get("/sync-status")
def screener_sync_status(
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    return ok(market_sync.sync_status())


class ParseIn(BaseModel):
    text: str = Field(max_length=500, description="一句话自然语言选股条件")


@router.post("/parse")
def screener_parse(
    payload: ParseIn,
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    try:
        result = screener_ai.parse_query(payload.text)
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content=fail(str(exc), code="llm_error"))
    return ok(result)


class InterpretIn(BaseModel):
    query: dict = Field(default_factory=dict, description="本次筛选条件")
    universe: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    items: list[dict] = Field(default_factory=list, description="命中股票明细（前端截前 15）")


@router.post("/interpret")
def screener_interpret(
    payload: InterpretIn,
    context: AuthContext = Depends(require_permission("agent:use")),
) -> dict:
    try:
        result = screener_ai.interpret_result(
            payload.query, payload.universe, payload.total, payload.items
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content=fail(str(exc), code="llm_error"))
    return ok(result)
