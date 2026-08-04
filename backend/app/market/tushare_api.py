from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from backend.app.auth.auth import AuthContext, require_permission


router = APIRouter()


@router.get("/api/tushare/catalog")
def tushare_catalog_list(
    category: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    _context: AuthContext = Depends(require_permission("dashboard:view")),
) -> dict:
    """TuShare 接口元数据目录（聚焦 A 股核心约 150 接口）。供前端管理页与 LLM 目录查询共用。"""
    from backend.app.market.tushare_catalog import (
        TUSHARE_CATALOG,
        by_category_scope as catalog_by_category_scope,
        category_index as catalog_category_index,
    )
    if not category:
        return {
            "categories": catalog_category_index(),
            "total": len(TUSHARE_CATALOG),
        }
    items = catalog_by_category_scope(category, scope)
    return {"category": category, "scope": scope, "count": len(items), "items": items}