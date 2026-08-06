"""站内通知接口：查询/已读/删除当前用户自己的通知。

约定：
- 新增端点，统一用 core.response 的 ok()/fail() 信封。
- 全部操作都按 context.user.id 隔离（个人数据），读写都只需 dashboard:view，
  与 watchlist 的 data:write 不同：通知删除/已读是个人内务，不触碰共享行情/持仓数据。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.response import fail, ok
from backend.app.notify import notification_service

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
def notification_list(
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    context: AuthContext = Depends(require_permission("dashboard:view")),
) -> dict:
    items, unread_count = notification_service.list_notifications(
        context.user.id, unread_only=unread_only, limit=limit, offset=offset
    )
    return ok({"items": items, "unread_count": unread_count})


@router.post("/{notification_id}/read")
def notification_read(
    notification_id: int,
    context: AuthContext = Depends(require_permission("dashboard:view")),
) -> dict:
    if not notification_service.mark_read(context.user.id, notification_id):
        return JSONResponse(status_code=404, content=fail("通知不存在", code="not_found"))
    return ok()


@router.post("/read-all")
def notification_read_all(
    context: AuthContext = Depends(require_permission("dashboard:view")),
) -> dict:
    count = notification_service.mark_all_read(context.user.id)
    return ok({"marked": count}, message="已全部标记为已读")


@router.delete("/{notification_id}")
def notification_delete(
    notification_id: int,
    context: AuthContext = Depends(require_permission("dashboard:view")),
) -> dict:
    if not notification_service.delete_notification(context.user.id, notification_id):
        return JSONResponse(status_code=404, content=fail("通知不存在", code="not_found"))
    return ok()
