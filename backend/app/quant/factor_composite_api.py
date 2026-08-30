"""多因子综合打分端点：各因子最新横截面 z-score 加权排名。

新增端点约定：``ok()/fail()`` 信封 + ``JSONResponse`` + ``require_permission``。
研究工具，不构成投资建议——免责说明随结果 interpretation 返回。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.response import fail, ok
from backend.app.quant import factor_service
from backend.app.quant.factor_service import MAX_COMPOSITE_FACTORS, MAX_COMPOSITE_ROWS, MAX_FACTOR_SYMBOLS

router = APIRouter(prefix="/api/factors", tags=["factors"])


class CompositeScoreRequest(BaseModel):
    factor_ids: list[str] = Field(min_length=1, max_length=MAX_COMPOSITE_FACTORS, description="因子 id，可混合 tushare: 前缀与本地预置因子")
    weights: dict[str, float] | None = Field(default=None, description="因子权重（缺省等权，可负；按绝对值之和归一化）")
    symbols: list[str] | None = Field(default=None, max_length=MAX_FACTOR_SYMBOLS, description="股票池；含本地因子时必填，仅 TuShare 因子可留空（用缓存全市场截面）")
    start_date: str | None = Field(default=None, max_length=10)
    end_date: str | None = Field(default=None, max_length=10)
    limit: int = Field(default=50, ge=1, le=MAX_COMPOSITE_ROWS, description="返回排名前 N 名")


@router.post("/composite-score")
def post_factor_composite_score(
    payload: CompositeScoreRequest,
    _context: AuthContext = Depends(require_permission("agent:use")),
):
    try:
        result = factor_service.composite_score_factors(
            payload.factor_ids,
            weights=payload.weights,
            symbols=payload.symbols,
            start_date=payload.start_date,
            end_date=payload.end_date,
            limit=payload.limit,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc)))
    return ok(result)
