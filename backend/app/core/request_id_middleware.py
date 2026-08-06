"""请求追踪中间件：为每个 HTTP 请求注入 trace_id。

单一职责：只负责 trace_id 的"透传或生成 + 写回 contextvar + 写回响应头"，
不做鉴权、不做日志、不做限流。放在中间件链最外层，保证后续日志/端点都能读到。

- 请求带 ``X-Request-ID`` 则沿用（便于前端/上游串联），否则新生成。
- 响应头回写 ``X-Trace-ID``，前端/排查可直接引用。

用纯 ASGI 实现（而非 BaseHTTPMiddleware），contextvar 在同一任务内设置，
能可靠传播到下游中间件与路由。
"""
from __future__ import annotations

from backend.app.core.request_context import new_trace_id, reset_trace_id, set_trace_id

HEADER_IN = b"x-request-id"
HEADER_OUT = b"x-trace-id"
_MAX_HEADER_LEN = 64


def _incoming_trace_id(scope: dict) -> str:
    for key, value in scope.get("headers") or []:
        if key == HEADER_IN:
            try:
                candidate = value.decode("latin-1").strip()
            except UnicodeDecodeError:
                return ""
            # 只接受安全字符且长度受限，避免把任意头内容带进日志/响应
            if 0 < len(candidate) <= _MAX_HEADER_LEN and all(
                ch.isalnum() or ch in "-_" for ch in candidate
            ):
                return candidate
            return ""
    return ""


class RequestIDMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trace_id = _incoming_trace_id(scope) or new_trace_id()
        token = set_trace_id(trace_id)

        async def send_with_trace(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((HEADER_OUT, trace_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            reset_trace_id(token)
