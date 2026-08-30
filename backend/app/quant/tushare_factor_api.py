"""TuShare 官方因子库管理端点：目录同步 / 因子值摄入 / 缓存查询。

新增端点约定：``ok()/fail()`` 信封 + ``JSONResponse`` + ``require_permission``。
评估计算不在此处——``tushare:`` 前缀因子直接走既有 ``/api/factors/benchmark``。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.response import fail, ok
from backend.app.quant import tushare_factor_service
from backend.app.quant.tushare_factor_service import MAX_SYNC_FACTORS

router = APIRouter(prefix="/api/factors/tushare", tags=["factors"])


class TushareFactorSyncRequest(BaseModel):
    factor_names: list[str] = Field(min_length=1, max_length=MAX_SYNC_FACTORS)
    start_date: str | None = Field(default=None, max_length=10)
    end_date: str | None = Field(default=None, max_length=10)
    symbols: list[str] | None = Field(default=None, max_length=50, description="可选股票池过滤；缺省缓存全市场截面")


@router.get("/catalog")
def get_tushare_catalog(
    factor_type: str | None = Query(default=None, max_length=32),
    query: str | None = Query(default=None, max_length=120),
    _context: AuthContext = Depends(require_permission("agent:use")),
):
    try:
        items = tushare_factor_service.list_catalog(factor_type=factor_type, query=query)
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc)))
    return ok({
        "total": len(items),
        "by_type": tushare_factor_service.catalog_factor_types(),
        "items": items,
    })


@router.post("/catalog/sync")
def post_tushare_catalog_sync(
    _context: AuthContext = Depends(require_permission("agent:use")),
):
    try:
        result = tushare_factor_service.sync_catalog()
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc)))
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content=fail(str(exc)))
    return ok(result, message="因子目录同步完成")


@router.post("/sync")
def post_tushare_factor_sync(
    payload: TushareFactorSyncRequest,
    _context: AuthContext = Depends(require_permission("agent:use")),
):
    try:
        result = tushare_factor_service.sync_factor_values(
            payload.factor_names,
            start_date=payload.start_date,
            end_date=payload.end_date,
            symbols=payload.symbols,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc)))
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content=fail(str(exc)))
    return ok(result, message="因子值同步完成")


@router.get("/values")
def get_tushare_factor_values(
    factor_name: str = Query(min_length=1, max_length=64),
    symbol: str | None = Query(default=None, max_length=16),
    start_date: str | None = Query(default=None, max_length=10),
    end_date: str | None = Query(default=None, max_length=10),
    limit: int = Query(default=500, ge=1, le=2000),
    _context: AuthContext = Depends(require_permission("agent:use")),
):
    try:
        items = tushare_factor_service.query_values(
            factor_name,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc)))
    return ok({"factor_name": factor_name, "total": len(items), "items": items})
