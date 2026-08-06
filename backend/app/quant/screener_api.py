"""技术面选股器接口：POST /api/screener/run。

新增端点，统一用 core.response 信封；鉴权用 require_permission（选股是只读研究
能力，data:read 即可）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.response import fail, ok
from backend.app.quant import screener_service

router = APIRouter(prefix="/api/screener", tags=["screener"])


class ScreenerQueryIn(BaseModel):
    market: str | None = Field(default=None, max_length=24, description="市场/交易所过滤")
    stock_type: str | None = Field(default="stock", max_length=24, description="标的类型，默认仅股票")
    price_min: float | None = Field(default=None, ge=0, description="最新价下限（含）")
    price_max: float | None = Field(default=None, ge=0, description="最新价上限（含）")
    pct_chg_min: float | None = Field(default=None, description="当日涨跌幅下限 %（含）")
    pct_chg_max: float | None = Field(default=None, description="当日涨跌幅上限 %（含）")
    keyword: str | None = Field(default=None, max_length=32, description="代码/名称模糊匹配")
    volume_ratio_min: float | None = Field(default=None, gt=0, description="量比下限（当日量/前5日均量）")
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
            above_ma=payload.above_ma,
            limit=payload.limit,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return ok(result)
