"""行业分析：本地市场共享数据，沿用 data:read 权限。"""
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.response import fail, ok
from backend.app.market import industry_service

router = APIRouter(prefix="/api/industries", tags=["industries"])


@router.get("/overview")
def overview(context: AuthContext = Depends(require_permission("data:read"))):
    return ok(industry_service.load_snapshot()["overview"])


@router.get("/members")
def members(
    industry: str = Query(min_length=1, max_length=120),
    snapshot_id: str = Query(min_length=24, max_length=24, pattern=r"^[0-9a-f]{24}$"),
    sort_by: Literal["pct_chg", "amount_yi", "turnover_rate", "volume_ratio", "total_mv_yi"] = "pct_chg",
    order: Literal["asc", "desc"] = "desc",
    offset: int = Query(default=0, ge=0, le=20000),
    limit: int = Query(default=50, ge=1, le=100),
    context: AuthContext = Depends(require_permission("data:read")),
):
    snapshot = industry_service.load_snapshot()
    if snapshot_id != snapshot["overview"]["snapshot_id"]:
        return JSONResponse(status_code=409, content=fail("市场数据已更新，请刷新行业概览后重试。", code="snapshot_changed"))
    return ok(industry_service.industry_members(snapshot, industry, sort_by=sort_by, order=order, offset=offset, limit=limit))
