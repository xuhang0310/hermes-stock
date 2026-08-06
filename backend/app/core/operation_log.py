"""操作日志中间件：把 /api/* 下的写操作（POST/PUT/PATCH/DELETE）审计到 operation_logs。

单一职责：只产出写操作审计行——不修改请求/响应、不做鉴权拦截、不做业务日志。

设计要点：
- 只记写操作（变更面），GET 读取不落审计：审计关注"谁改了什么"，读操作量大且无审计价值。
- SSE 流式响应（content-type 为 text/event-stream，如智能体对话）不记：
  长连接无法用一次请求-响应刻画，且对话内容已有 ai_chat_messages 持久化。
- 用户身份复用 ``auth.decode_access_token`` 解析 Authorization bearer（DRY，不重复实现
  令牌校验）；解析失败时 user_id 记 None（匿名/凭证无效也留下操作痕迹）。
- 入库走 ``asyncio.to_thread`` 线程池，写失败只告警——审计绝不能拖慢或阻断主请求。
- 受 ``settings.operation_log_enabled`` 开关控制（高频压测场景可关）。
"""
from __future__ import annotations

import asyncio
import time

from backend.app.core.applog import get_logger
from backend.app.core.request_context import get_trace_id
from backend.app.core.settings import settings

_logger = get_logger("api")

_MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_METHOD_ACTION = {"POST": "新增", "PUT": "更新", "PATCH": "更新", "DELETE": "删除"}

# 路径前缀 -> 业务域中文标签（仅收录有独立命名空间的前缀；未命中时只记动作）
_ACTION_PREFIXES = (
    ("/api/auth/", "认证"),
    ("/api/agent-board/", "智能体看板"),
    ("/api/backtests/", "回测"),
    ("/api/factors/", "因子"),
    ("/api/stocks/", "行情"),
    ("/api/notifications/", "通知"),
    ("/api/research-reports/", "研究报告"),
    ("/api/paper-trading/", "模拟盘"),
)


def _action_label(method: str, path: str) -> str:
    verb = _METHOD_ACTION.get(method, method)
    for prefix, label in _ACTION_PREFIXES:
        if path.startswith(prefix):
            return f"{label}·{verb}"
    return verb


def _header_value(scope: dict, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key == name:
            return value.decode("latin-1", "ignore")
    return ""


def _user_id(scope: dict) -> int | None:
    """从 Authorization bearer 解出用户 id；复用令牌校验逻辑，失败返回 None。"""
    from backend.app.auth.auth import decode_access_token

    authorization = _header_value(scope, b"authorization")
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        return int(payload.get("sub"))
    except Exception:  # noqa: BLE001 —— 解析失败只影响审计归属，不影响请求
        return None


def _client_ip(scope: dict) -> str | None:
    forwarded = _header_value(scope, b"x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64] or None
    client = scope.get("client")
    return client[0] if client else None


def _write_log(row: dict) -> None:
    from backend.app.core.database import SessionLocal
    from backend.app.core.models import OperationLog

    try:
        with SessionLocal() as session:
            session.add(OperationLog(**row))
            session.commit()
    except Exception:  # noqa: BLE001 —— 审计失败绝不能影响主流程
        _logger.warning("写入 operation_logs 失败（已忽略）", exc_info=True)


class OperationLogMiddleware:
    """纯 ASGI 中间件：写操作完成后异步落审计行。"""

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")
        if (
            not settings.operation_log_enabled
            or method not in _MUTATING_METHODS
            or not path.startswith("/api/")
        ):
            await self._app(scope, receive, send)
            return

        started = time.monotonic()
        status_code: int | None = None
        is_stream = False

        async def send_wrapper(message):
            nonlocal status_code, is_stream
            if message["type"] == "http.response.start":
                status_code = message.get("status")
                for key, value in message.get("headers") or []:
                    if key == b"content-type" and b"text/event-stream" in value.lower():
                        is_stream = True
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        finally:
            if not is_stream:
                row = {
                    "user_id": _user_id(scope),
                    "method": method,
                    "path": path[:255],
                    "action": _action_label(method, path),
                    "status_code": status_code,
                    "success": bool(status_code is not None and status_code < 400),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "trace_id": get_trace_id() or None,
                    "ip": _client_ip(scope),
                }
                await asyncio.to_thread(_write_log, row)
