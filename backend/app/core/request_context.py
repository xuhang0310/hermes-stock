"""请求级上下文：trace_id 的存取。

单一职责：只管理"当前请求的追踪 id"这一件事（contextvar + 生成/读/写），
不做日志、不做中间件。HA 双副本下用同一个 trace_id 把跨副本日志串起来。
"""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar, Token

_TRACE_ID: ContextVar[str] = ContextVar("hermes_trace_id", default="")


def new_trace_id() -> str:
    """生成一个短 trace_id（16 位十六进制）。"""
    return uuid.uuid4().hex[:16]


def get_trace_id() -> str:
    """读当前请求的 trace_id；不在请求上下文中时返回空串。"""
    return _TRACE_ID.get()


def set_trace_id(trace_id: str) -> Token:
    """写入 trace_id（空串则自动生成），返回 token 供 reset。"""
    return _TRACE_ID.set(trace_id or new_trace_id())


def reset_trace_id(token: Token) -> None:
    """恢复到 set 之前的状态，防止跨请求串号。"""
    _TRACE_ID.reset(token)


class TraceIdFilter(logging.Filter):
    """给每条日志记录注入 trace_id 字段（无请求上下文时为 ``-``）。

    需挂在 handler 上（而非 logger 上），这样从子 logger 传播上来的记录也能覆盖。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _TRACE_ID.get() or "-"
        return True
