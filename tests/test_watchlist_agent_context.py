from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from backend.app.agents import watchlist_agent


class _DbStub:
    def commit(self) -> None:
        pass


class _RepositoryStub:
    def __init__(self) -> None:
        self.db = _DbStub()

    def find_instruments(self, query: str, limit: int = 10) -> list[dict[str, str]]:
        if query == "方正证券":
            return [
                {"symbol": "601901.SH", "name": "方正证券", "market": "SSE"},
                {"symbol": "300998.SZ", "name": "宁波方正", "market": "SZSE"},
            ][:limit]
        return []

    def watchlist(self) -> list[dict]:
        return []


def _llm_config() -> dict:
    return {
        "id": 1,
        "api_key": "test-key",
        "base_url": "https://example.invalid/v1",
        "model": "test-model",
        "display_name": "测试模型",
        "provider_name": "test",
    }


class WatchlistAgentContextTests(unittest.TestCase):
    def test_ambiguous_search_keeps_write_intent_and_candidates(self) -> None:
        calls = 0

        def fake_stream(_messages, _config, _tools):
            nonlocal calls
            calls += 1
            if calls == 1:
                yield "message", {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_search",
                            "function": {
                                "name": "search_instruments",
                                "arguments": json.dumps({"query": "方正证券"}, ensure_ascii=False),
                            },
                        }
                    ],
                }
            else:
                yield "message", {"content": "请确认是 601901.SH 还是 300998.SZ。", "tool_calls": []}

        with (
            patch.object(watchlist_agent, "active_llm_config", return_value=_llm_config()),
            patch.object(watchlist_agent, "_stream_chat", side_effect=fake_stream),
        ):
            events = list(
                watchlist_agent.run_watchlist_agent_events(
                    repository=_RepositoryStub(),
                    instruction="把方正证券加入自选，理由是观察券商板块",
                )
            )

        pending = events[-1]["pending_action"]
        self.assertEqual(pending["tool"], "add_watchlist_items")
        self.assertEqual(pending["arguments"]["rows"], [{"name": "方正证券"}])
        self.assertEqual([item["symbol"] for item in pending["candidates"]], ["601901.SH", "300998.SZ"])

    def test_clarification_reuses_pending_operation_and_history(self) -> None:
        captured_messages: list[dict] = []
        executed_args: list[dict] = []
        calls = 0
        pending = {
            "tool": "add_watchlist_items",
            "arguments": {
                "rows": [{"name": "方正证券", "group_name": "券商", "reason": "观察券商板块"}]
            },
            "original_instruction": "把方正证券加入自选，理由是观察券商板块",
            "candidates": [
                {"symbol": "601901.SH", "name": "方正证券", "market": "SSE"},
                {"symbol": "300998.SZ", "name": "宁波方正", "market": "SZSE"},
            ],
        }

        def fake_stream(messages, _config, _tools):
            nonlocal calls
            calls += 1
            if calls == 1:
                captured_messages.extend(messages)
                yield "message", {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_add",
                            "function": {
                                "name": "add_watchlist_items",
                                "arguments": json.dumps({"rows": [{"symbol": "601901.SH"}]}),
                            },
                        }
                    ],
                }
            else:
                yield "message", {"content": "已加入方正证券。", "tool_calls": []}

        def fake_execute(_repository, _name, args, *, created_by):
            del created_by
            executed_args.append(args)
            return {"changed_count": 1, "error_count": 0, "watchlist": []}

        history = [
            {"role": "user", "content": "把方正证券加入自选，理由是观察券商板块"},
            {"role": "assistant", "content": "请确认具体代码。"},
        ]
        with (
            patch.object(watchlist_agent, "active_llm_config", return_value=_llm_config()),
            patch.object(watchlist_agent, "_stream_chat", side_effect=fake_stream),
            patch.object(watchlist_agent, "_execute_watchlist_tool", side_effect=fake_execute),
        ):
            events = list(
                watchlist_agent.run_watchlist_agent_events(
                    repository=_RepositoryStub(),
                    instruction="601901.SH",
                    history=history,
                    pending_action=pending,
                )
            )

        self.assertIn("不要再问用户要添加还是删除", captured_messages[0]["content"])
        self.assertIn("601901.SH", captured_messages[0]["content"])
        self.assertEqual(
            [(item["role"], item["content"]) for item in captured_messages[1:]],
            [("user", history[0]["content"]), ("assistant", history[1]["content"]), ("user", "601901.SH")],
        )
        self.assertEqual(
            executed_args[0]["rows"],
            [
                {
                    "name": "方正证券",
                    "group_name": "券商",
                    "reason": "观察券商板块",
                    "symbol": "601901.SH",
                }
            ],
        )
        self.assertIsNone(events[-1]["pending_action"])

    def test_pending_action_is_bounded_and_cancelled_explicitly(self) -> None:
        pending = watchlist_agent._normalize_pending_action(
            {
                "tool": "remove_watchlist_items",
                "arguments": {"rows": [{"symbol": "000001.SZ", "unknown": "drop-me"}]},
                "candidates": [{"symbol": "000001.SZ", "name": "平安银行", "extra": "drop-me"}],
            }
        )

        self.assertEqual(pending["arguments"]["rows"], [{"symbol": "000001.SZ"}])
        self.assertEqual(pending["candidates"], [{"symbol": "000001.SZ", "name": "平安银行"}])
        self.assertTrue(watchlist_agent._is_cancel_reply("算了吧！"))
        self.assertEqual(
            watchlist_agent._detect_watchlist_write_tool("不要删除，帮我放到自选里"),
            "add_watchlist_items",
        )


if __name__ == "__main__":
    unittest.main()
