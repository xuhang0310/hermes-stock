"""运行时配置覆盖接口：查看/设置/删除白名单参数的运行时覆盖值。

新增端点，统一用 core.response 信封。属于系统管理能力，鉴权用 system:view
（仅 admin/super_admin 持有）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core import runtime_overrides
from backend.app.core.response import fail, ok

router = APIRouter(prefix="/api/system/runtime-overrides", tags=["system"])


class OverrideIn(BaseModel):
    value: str = Field(min_length=1, max_length=500)


@router.get("")
def overrides_list(
    _context: AuthContext = Depends(require_permission("system:view")),
) -> dict:
    return ok({"items": runtime_overrides.list_overrides()})


@router.put("/{key}")
def overrides_set(
    key: str,
    payload: OverrideIn,
    _context: AuthContext = Depends(require_permission("system:view")),
) -> dict:
    try:
        item = runtime_overrides.set_override(key, payload.value)
    except ValueError as exc:
        return JSONResponse(status_code=400, content=fail(str(exc), code="bad_request"))
    return ok(item, message="配置已更新，立即生效")


@router.delete("/{key}")
def overrides_delete(
    key: str,
    _context: AuthContext = Depends(require_permission("system:view")),
) -> dict:
    if not runtime_overrides.delete_override(key):
        return JSONResponse(status_code=404, content=fail("该配置项未设置覆盖值", code="not_found"))
    return ok(message="已恢复环境变量默认值")
