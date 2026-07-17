"""验证工具参数流式进度：明文解码边界 + 节流 + 预览增量 + SSE 旁路。免 DB，本地直跑。

    python scripts/test_tool_progress.py
"""
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.ai.llm_client import parse_sse_stream  # noqa: E402
from backend.app.ai.tool_progress import (  # noqa: E402
    EMIT_EVERY_CHARS,
    PREVIEW_TOTAL_LIMIT,
    ToolArgsProgressTracker,
    _partial_json_string,
)

ok = 0


def check(label: str, cond: bool) -> None:
    global ok
    assert cond, f"✗ {label}"
    ok += 1
    print(f"  ✓ {label}")


print("_partial_json_string 明文解码：")
check("普通文本原样", _partial_json_string("hello 世界") == "hello 世界")
check("转义 \\n \\\" \\\\ 解码", _partial_json_string('a\\nb\\"c\\\\d') == 'a\nb"c\\d')
check("悬挂反斜杠停在断点", _partial_json_string("abc\\") == "abc")
check("不完整 \\uXX 停在断点", _partial_json_string("abc\\u4e") == "abc")
check("完整 \\u4e2d 解码为 中", _partial_json_string("\\u4e2d") == "中")
check("未转义引号=字符串闭合，后续截断", _partial_json_string('正文"}, "x": 1}') == "正文")

print("节流与首报：")
t = ToolArgsProgressTracker()
ev = t.feed(0, "ws_write_file", "")
check("首个带工具名片段立即发（哪怕参数为空）", ev is not None and ev["tool"] == "ws_write_file" and ev["chars"] == 0)
ev = t.feed(0, "", '{"path": "a.md", ')
check(f"小片段（<{EMIT_EVERY_CHARS}字）不发", ev is None)
ev = t.feed(0, "", '"content": "' + "x" * EMIT_EVERY_CHARS)
check("累积过阈值发进度", ev is not None and ev["chars"] > EMIT_EVERY_CHARS)

print("content 预览增量拼接：")
t = ToolArgsProgressTracker()
content = "# 报告标题\n第一段：宁德时代基本面稳健。\n第二段：风险在于\"产能\"扩张节奏。\n" + "正文填充。" * 80
raw = json.dumps({"path": "reports/x.md", "content": content}, ensure_ascii=False)
# 按 37 字符一片喂（刻意错开转义边界），拼接所有 delta 应等于 content 的前缀且最终全量
previews: list[str] = []
for i in range(0, len(raw), 37):
    ev = t.feed(0, "ws_write_file" if i == 0 else "", raw[i : i + 37])
    if ev:
        previews.append(ev["delta"])
joined = "".join(previews)
check("预览增量拼接后是 content 前缀", content.startswith(joined) and len(joined) > 0)
# 结尾补一次空喂不出事件；预览必然略滞后（差 < 阈值窗口），补齐检查覆盖率
check(f"预览已覆盖 content 大部分（差<{EMIT_EVERY_CHARS*2}字）", len(content) - len(joined) < EMIT_EVERY_CHARS * 2)
check("闭合后预览不含 JSON 尾巴", not joined.rstrip().endswith('"}'))

print("多 index 独立：")
t = ToolArgsProgressTracker()
e0 = t.feed(0, "ws_write_file", '{"content": "AAA' + "a" * EMIT_EVERY_CHARS)
e1 = t.feed(1, "ws_glob", '{"pattern": "' + "b" * EMIT_EVERY_CHARS)
check("index0 事件归 ws_write_file", e0 is not None and e0["tool"] == "ws_write_file" and e0["delta"].startswith("AAA"))
check("index1 事件归 ws_glob，无 content 则 delta 为空", e1 is not None and e1["tool"] == "ws_glob" and e1["delta"] == "")

print("预览总量上限：")
t = ToolArgsProgressTracker()
huge = '{"content": "' + "长" * (PREVIEW_TOTAL_LIMIT + 3000)
total = ""
for i in range(0, len(huge), 500):
    ev = t.feed(0, "ws_write_file" if i == 0 else "", huge[i : i + 500])
    if ev:
        total += ev["delta"]
check(f"预览累计 ≤ {PREVIEW_TOTAL_LIMIT}", len(total) <= PREVIEW_TOTAL_LIMIT)

print("parse_sse_stream 旁路（假 SSE 行）：")
def sse(obj) -> str:
    return "data:" + json.dumps(obj, ensure_ascii=False)

args_full = '{"path": "n.md", "content": "你好\\n世界"}'
lines = [
    sse({"choices": [{"delta": {"content": "先说"}}]}),
    sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "ws_write_file", "arguments": args_full[:20]}}]}}]}),
    sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args_full[20:]}}]}}]}),
    "data:[DONE]",
]
kinds = list(parse_sse_stream(lines))
tool_args_events = [p for k, p in kinds if k == "tool_args"]
message = [p for k, p in kinds if k == "message"][0]
check("旁路吐出 tool_args 片段", len(tool_args_events) == 2 and tool_args_events[0]["name"] == "ws_write_file")
check("message 组装不受旁路影响（参数完整）", message["tool_calls"][0]["function"]["arguments"] == args_full)
check("content 通道正常", message["content"] == "先说")

print(f"\n工具进度流验证通过 ✓（{ok} 项断言）")
