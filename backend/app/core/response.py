"""统一响应信封：新增端点用 ``ok()``/``fail()`` 返回一致的结构。

单一职责：只负责响应整形，不做鉴权/校验/日志。

约定：
- 新增端点统一返回 ``{"success", "data", "message", "trace_id"}``，便于前端统一解包与排障。
- 既有端点不改（避免破坏前端），信封只用于本批新增端点。
- ``trace_id`` 自动从请求上下文注入，便于和日志串联；无请求上下文时为 ``None``。
"""
from __future__ import annotations

from typing import Any

from backend.app.core.request_context import get_trace_id


def _trace() -> str | None:
    return get_trace_id() or None


def ok(data: Any = None, message: str = "ok") -> dict:
    """成功响应。"""
    return {"success": True, "data": data, "message": message, "trace_id": _trace()}


def fail(message: str, *, code: str | None = None, data: Any = None) -> dict:
    """失败响应（业务错误）。HTTP 状态码由调用方决定（可配合 JSONResponse）。"""
    return {
        "success": False,
        "data": data,
        "message": message,
        "code": code,
        "trace_id": _trace(),
    }
