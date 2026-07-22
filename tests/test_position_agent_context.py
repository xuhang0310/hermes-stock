from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from backend.app.agents import position_agent


class _RepositoryStub:
    def __init__(self, accounts: list[dict] | None = None) -> None:
        self._accounts = accounts or []

    def list_position_accounts(self) -> list[dict]:
        return self._accounts


def _llm_config() -> dict:
    return {
        "id": 1,
        "api_key": "test-key",
        "base_url": "https://example.invalid/v1",
        "model": "test-model",
        "display_name": "测试模型",
        "provider_name": "test",
    }


class PositionAgentContextTests(unittest.TestCase):
    def test_selected_account_is_visible_before_model_decides(self) -> None:
        captured_messages: list[dict] = []

        def fake_stream(messages, _config, _tools):
            captured_messages.extend(messages)
            yield "message", {"content": "开始保存", "tool_calls": []}

        repository = _RepositoryStub(
            [{"id": 7, "broker_name": "方正证券", "account_name": "主账户", "account_no_masked": "****1234"}]
        )
        history = [
            {"role": "user", "content": "请识别这张持仓截图"},
            {"role": "assistant", "content": "请确认保存到哪个账户"},
        ]

        with (
            patch.object(position_agent, "active_llm_config", return_value=_llm_config()),
            patch.object(position_agent, "_stream_chat", side_effect=fake_stream),
        ):
            events = list(
                position_agent.run_position_agent_events(
                    repository=repository,
                    instruction="继续录入",
                    account_id=7,
                    history=history,
                )
            )

        system_prompt = captured_messages[0]["content"]
        self.assertIn("方正证券 / 主账户", system_prompt)
        self.assertIn("不得再次询问用户是哪个证券账户", system_prompt)
        self.assertEqual(
            [(item["role"], item["content"]) for item in captured_messages[1:]],
            [("user", history[0]["content"]), ("assistant", history[1]["content"]), ("user", "继续录入")],
        )
        self.assertEqual(events[-1]["type"], "done")

    def test_missing_account_keeps_parsed_rows_for_next_turn(self) -> None:
        calls = 0
        tool_args = {
            "rows": [
                {
                    "symbol": "600519.SH",
                    "name": "贵州茅台",
                    "position_quantity": 100,
                    "cost_price": 1500.5,
                }
            ]
        }

        def fake_stream(_messages, _config, _tools):
            nonlocal calls
            calls += 1
            if calls == 1:
                yield "message", {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_save",
                            "function": {
                                "name": "save_position_holdings",
                                "arguments": json.dumps(tool_args, ensure_ascii=False),
                            },
                        }
                    ],
                }
            else:
                yield "message", {"content": "这批持仓保存到哪个证券账户？", "tool_calls": []}

        with (
            patch.object(position_agent, "active_llm_config", return_value=_llm_config()),
            patch.object(position_agent, "_stream_chat", side_effect=fake_stream),
            patch.object(
                position_agent,
                "_execute_position_tool",
                return_value={"need_account": True, "message": "请确认证券账户"},
            ),
        ):
            events = list(
                position_agent.run_position_agent_events(
                    repository=_RepositoryStub(),
                    instruction="识别并录入截图里的持仓",
                )
            )

        done = events[-1]
        self.assertEqual(done["type"], "done")
        self.assertFalse(done["should_save"])
        self.assertEqual(done["pending_action"]["tool"], "save_position_holdings")
        self.assertEqual(done["pending_action"]["arguments"]["rows"], tool_args["rows"])

    def test_pending_draft_is_bounded_and_injected_as_state(self) -> None:
        pending = position_agent._normalize_pending_action(
            {
                "tool": "save_position_holdings",
                "arguments": {
                    "rows": [{"symbol": "000001.SZ", "position_quantity": 200, "unknown": "drop-me"}],
                    "unexpected": "drop-me",
                },
            }
        )

        self.assertIsNotNone(pending)
        row = pending["arguments"]["rows"][0]
        self.assertEqual(row, {"symbol": "000001.SZ", "position_quantity": 200})
        hint = position_agent._pending_action_hint(pending)
        self.assertIn("000001.SZ", hint)
        self.assertIn("不要要求重传图片", hint)

        merged = position_agent._merge_pending_tool_args(
            "save_position_holdings",
            {"account": {"broker_name": "方正证券"}},
            pending,
        )
        self.assertEqual(merged["rows"], pending["arguments"]["rows"])
        self.assertEqual(merged["account"]["broker_name"], "方正证券")


if __name__ == "__main__":
    unittest.main()
