"""建议成绩单接口。只看得到自己的成绩单。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.database import SessionLocal
from backend.app.core.response import fail, ok
from backend.app.scorecard import rules, service

router = APIRouter(prefix="/api/scorecard", tags=["scorecard"])
_BEIJING = timezone(timedelta(hours=8))


@router.get("")
def scorecard_summary(
    horizon: int = Query(default=rules.PRIMARY_HORIZON),
    days: int = Query(default=180, ge=30, le=730),
    account_id: int | None = Query(default=None, ge=0, description="只看这个账户；不传是自己名下全部账户"),
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    if horizon not in rules.HORIZONS:
        return JSONResponse(status_code=400, content=fail(f"期限只能是 {'、'.join(str(item) for item in rules.HORIZONS)} 个交易日", code="bad_request"))
    with SessionLocal() as db:
        return ok(service.summary(db, context.user.id, account_id=account_id, horizon=horizon, days=days))


@router.post("/refresh")
def scorecard_refresh(
    context: AuthContext = Depends(require_permission("schedule:manage")),
) -> dict:
    """手动更新一次：登记新建议、给到期的算结果。只动自己的建议，别人的等每天收盘后的任务。"""
    message = service.run_daily(datetime.now(_BEIJING).replace(tzinfo=None), owner_user_id=context.user.id)
    if "失败" in message:
        return JSONResponse(status_code=400, content=fail(message, code="refresh_failed"))
    return ok({"message": message}, message)
