from __future__ import annotations

import json


def sse_event(event: dict) -> str:
    """把事件 dict 格式化为 SSE 帧（持仓/自选 Agent 流式接口共用）。"""
    event_type = str(event.get("type") or "message")
    payload = json.dumps(event, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"