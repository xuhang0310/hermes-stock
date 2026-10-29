"""Agent 链路纯函数回归测试集。

抄 Pydantic AI v2 评估框架的思路（给定输入→断言期望行为），但不引 pydantic-evals
依赖、不调真实 LLM——只覆盖 agent 链路里最易回归、最适合单测的纯函数层：

1. position_intent.detect_position_intent —— 交易意图识别（BUY/SELL/CLOSE/...）
2. position_import._extract_json / _normalize_row —— LLM 返回解析与字段规整
3. ai.llm_client.parse_sse_stream —— OpenAI SSE 流解析（content delta + tool_call 累积）

这些是 agent 改重构（如 skill_registry / llm_client 抽取）后最该挡住的回归点。
不连 MySQL，独立可跑：``python -m pytest tests/test_agent_regression.py -q``
"""
from __future__ import annotations

import json
import unittest

from backend.app.agents.position_import import (
    _extract_json,
    _extract_message_content,
    _normalize_row,
)
from backend.app.agents.position_intent import (
    PositionAction,
    _normalize_to_ts_code,
    build_intent_hint,
    detect_position_intent,
)
from backend.app.ai.llm_client import parse_sse_stream


class IntentDetectionTests(unittest.TestCase):
    """交易意图识别：给定自然语言→断言 action/quantity。改 position_intent 后跑这组。"""

    def assertAction(self, text: str, action: PositionAction, *, quantity: float | None = None) -> None:
        intent = detect_position_intent(text)
        self.assertEqual(intent.action, action, f"{text!r} -> {intent.action} != {action}")
        if quantity is not None:
            self.assertEqual(intent.quantity, quantity, f"{text!r} qty {intent.quantity} != {quantity}")

    def test_buy_with_code_and_quantity(self) -> None:
        self.assertAction("买入100股平安银行000001", PositionAction.BUY, quantity=100)

    def test_add_position(self) -> None:
        self.assertAction("加仓500股600519", PositionAction.ADD, quantity=500)

    def test_sell_all(self) -> None:
        self.assertAction("卖出全部茅台600519", PositionAction.SELL)

    def test_reduce(self) -> None:
        self.assertAction("减仓200股000002", PositionAction.REDUCE, quantity=200)

    def test_close(self) -> None:
        self.assertAction("清仓000002", PositionAction.CLOSE)

    def test_trade_hints_pass_the_traded_quantity_not_the_new_total(self) -> None:
        # 后端按"本次变动数量"扣减/累加并结算资金；提示若让模型传剩余/总数会扣错数量和回款
        for text in ("减仓200股000002", "卖出100股000002", "加仓500股600519"):
            hint = build_intent_hint(detect_position_intent(text))
            self.assertIn("position_quantity 传本次", hint, text)
            self.assertNotIn("新 quantity =", hint, text)
            self.assertNotIn("剩余 quantity", hint, text)

    def test_chitchat_is_unknown(self) -> None:
        self.assertAction("你好", PositionAction.UNKNOWN)
        self.assertAction("在吗", PositionAction.UNKNOWN)

    def test_normalize_ts_code(self) -> None:
        self.assertEqual(_normalize_to_ts_code("600519"), "600519.SH")
        self.assertEqual(_normalize_to_ts_code("000001"), "000001.SZ")
        self.assertEqual(_normalize_to_ts_code("430047"), "430047.BJ")
        self.assertEqual(_normalize_to_ts_code("600519.SH"), "600519.SH")


class LlmResponseParsingTests(unittest.TestCase):
    """LLM 返回解析：fenced/bare JSON 提取 + 字段规整（KEY_MAP 映射）。"""

    def test_extract_json_from_fenced_block(self) -> None:
        raw = 'some text\n```json\n{"intent": "add_position", "rows": []}\n```\nmore'
        parsed = _extract_json(raw)
        self.assertEqual(parsed["intent"], "add_position")

    def test_extract_json_bare_braces(self) -> None:
        parsed = _extract_json('噪音 {"should_save": true, "reply": "hi"} 尾巴')
        self.assertTrue(parsed["should_save"])
        self.assertEqual(parsed["reply"], "hi")

    def test_extract_json_array(self) -> None:
        parsed = _extract_json('[{"a":1},{"b":2}]')
        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 2)

    def test_normalize_row_maps_chinese_keys(self) -> None:
        # KEY_MAP: 代码->symbol, 名称->name, 持仓数量->position_quantity, 成本价->cost_price
        raw = {"代码": "000001.SZ", "名称": "平安银行", "持仓数量": 1000, "成本价": 10.2}
        normalized = _normalize_row(raw, {}, {"as_of_date": "2026-07-16"})
        self.assertEqual(normalized["symbol"], "000001.SZ")
        self.assertEqual(normalized["name"], "平安银行")
        self.assertEqual(normalized["position_quantity"], 1000)
        self.assertEqual(normalized["cost_price"], 10.2)
        self.assertEqual(normalized["as_of_date"], "2026-07-16")

    def test_extract_message_content(self) -> None:
        body = {"choices": [{"message": {"content": "hello world"}}]}
        self.assertEqual(_extract_message_content(body), "hello world")

    def test_extract_message_content_missing(self) -> None:
        body = {"choices": [{"message": {}}]}
        self.assertEqual(_extract_message_content(body), "")


class SseStreamParsingTests(unittest.TestCase):
    """OpenAI SSE 流解析：content delta 累积 + tool_call 累积 + [DONE] 结束。"""

    @staticmethod
    def _sse_line(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}"

    def test_content_delta_accumulates(self) -> None:
        lines = [
            self._sse_line({"choices": [{"delta": {"content": "hello"}}]}),
            self._sse_line({"choices": [{"delta": {"content": " world"}}]}),
            "data: [DONE]",
        ]
        events = list(parse_sse_stream(lines))
        contents = [p for kind, p in events if kind == "content"]
        self.assertEqual("".join(contents), "hello world")
        # 最后一个事件是 message，含完整 content
        msg_events = [p for kind, p in events if kind == "message"]
        self.assertEqual(msg_events[-1]["content"], "hello world")

    def test_tool_call_accumulates(self) -> None:
        lines = [
            self._sse_line({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "function": {"name": "resolve_stock", "arguments": '{"q":'}}
            ]}}]}),
            self._sse_line({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '"平安银行"}'}}
            ]}}]}),
            "data: [DONE]",
        ]
        events = list(parse_sse_stream(lines))
        msg = [p for kind, p in events if kind == "message"][-1]
        self.assertEqual(len(msg["tool_calls"]), 1)
        tc = msg["tool_calls"][0]
        self.assertEqual(tc["id"], "call_1")
        self.assertEqual(tc["function"]["name"], "resolve_stock")
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"q": "平安银行"})

    def test_done_terminates(self) -> None:
        lines = [
            self._sse_line({"choices": [{"delta": {"content": "a"}}]}),
            "data: [DONE]",
            self._sse_line({"choices": [{"delta": {"content": "b"}}]}),  # 应被忽略
        ]
        events = list(parse_sse_stream(lines))
        contents = "".join(p for kind, p in events if kind == "content")
        self.assertEqual(contents, "a")

    def test_reasoning_delta(self) -> None:
        lines = [
            self._sse_line({"choices": [{"delta": {"reasoning_content": "thinking..."}}]}),
            "data: [DONE]",
        ]
        events = list(parse_sse_stream(lines))
        reasoning = [p for kind, p in events if kind == "reasoning"]
        self.assertEqual(reasoning, ["thinking..."])


if __name__ == "__main__":
    unittest.main()
