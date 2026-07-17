"""工具调用参数的流式进度跟踪 —— 把 LLM 生成长参数（写文件内容、脚本正文）时的
"黑箱静默期"变成可见进度：字数计数 + content 字段明文实时预览。

背景：模型决定调工具后，参数 JSON 在 tool_calls delta 里逐片生成；写 10KB 报告时这段
可长达几十秒，而正文通道零输出，用户完全看不到过程。llm_client.parse_sse_stream 已把
参数片段旁路成 ``("tool_args", {...})``；本模块消费这些片段，节流后产出可直接下发前端的
``tool_progress`` 事件。单一职责：只做累积/节流/提取，不碰 SSE、不 import 业务模块。

预览提取：参数是流式的 **不完整 JSON**（如 ``{"path": "a.md", "content": "# 标\\n``），
不能 json.loads。_partial_json_string 逐字符解码 JSON 字符串值的已到片段（跨片转义、
不完整 \\uXXXX 自然留待下次全量重扫补齐），拿到 content 字段已生成部分的明文。
"""
from __future__ import annotations

import re
from typing import Any

# 每累积这么多字符参数才发一次进度事件（首个带工具名的片段立即发，让 UI 秒知在调什么）
EMIT_EVERY_CHARS = 200
# 预览明文最多下发这么多字符，之后只发字数计数（防超长文件把 SSE 流灌爆）
PREVIEW_TOTAL_LIMIT = 12000

_CONTENT_KEY_RE = re.compile(r'"content"\s*:\s*"')

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}


def _partial_json_string(fragment: str) -> str:
    """把 JSON 字符串值的流式片段尽力解码为明文。

    片段可能未闭合、可能在转义序列中间截断：悬挂 ``\\`` 或不完整 ``\\uXXXX`` 直接停
    （调用方每次全量重扫，下一片到达自然补齐）；遇到未转义的 ``"`` 说明字符串已闭合。
    """
    out: list[str] = []
    i, n = 0, len(fragment)
    while i < n:
        ch = fragment[i]
        if ch == '"':
            break  # 字符串值已闭合，后面是 JSON 其余部分
        if ch == "\\":
            if i + 1 >= n:
                break  # 悬挂转义，等下一片
            nxt = fragment[i + 1]
            if nxt == "u":
                if i + 6 > n:
                    break  # \uXXXX 不完整
                try:
                    out.append(chr(int(fragment[i + 2 : i + 6], 16)))
                except ValueError:
                    pass
                i += 6
                continue
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class ToolArgsProgressTracker:
    """按 tool_call index 累积参数片段，节流产出 tool_progress 事件负载。

    每轮 LLM 流式调用新建一个实例（状态不跨轮）。``feed`` 返回 None（未到发射点）
    或事件 dict：``{"type": "tool_progress", "index", "tool", "chars", "delta"}``；
    ``delta`` 是 content 字段明文预览的增量（无 content 字段或超预览上限时为空串）。
    display_name 由调用方补（本模块不 import 业务展示表）。
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, Any]] = {}

    def feed(self, index: int, name: str, delta: str) -> dict[str, Any] | None:
        state = self._calls.setdefault(
            index,
            {"name": "", "raw": "", "announced": False, "last_emit_len": 0, "preview_sent": 0},
        )
        if name:
            state["name"] = name
        state["raw"] += delta

        first_announce = bool(state["name"]) and not state["announced"]
        grown_enough = len(state["raw"]) - state["last_emit_len"] >= EMIT_EVERY_CHARS
        if not first_announce and not grown_enough:
            return None

        state["announced"] = True
        state["last_emit_len"] = len(state["raw"])
        return {
            "type": "tool_progress",
            "index": index,
            "tool": state["name"],
            "chars": len(state["raw"]),
            "delta": self._preview_delta(state),
        }

    def _preview_delta(self, state: dict[str, Any]) -> str:
        """从累积参数里提取 content 明文的新增部分（相对上次已下发）。"""
        if state["preview_sent"] >= PREVIEW_TOTAL_LIMIT:
            return ""
        match = _CONTENT_KEY_RE.search(state["raw"])
        if not match:
            return ""
        full = _partial_json_string(state["raw"][match.end():])
        new_text = full[state["preview_sent"]:]
        if not new_text:
            return ""
        budget = PREVIEW_TOTAL_LIMIT - state["preview_sent"]
        new_text = new_text[:budget]
        state["preview_sent"] += len(new_text)
        return new_text
