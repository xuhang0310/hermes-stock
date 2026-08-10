from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.app.agents import stock_agent


class StockAgentModelFailoverTests(unittest.TestCase):
    def test_unpurchased_primary_model_falls_back_and_updates_session(self) -> None:
        configs = {
            5: {
                "id": 5,
                "display_name": "百炼 Qwen3.7 Plus",
                "provider_name": "百炼",
                "base_url": "https://bailian.invalid/v1",
                "api_key": "test-key",
                "model": "qwen3.7-plus",
                "temperature": 0.2,
                "max_steps": 2,
            },
            11: {
                "id": 11,
                "display_name": "GPT fallback",
                "provider_name": "OpenAI compatible",
                "base_url": "https://fallback.invalid/v1",
                "api_key": "fallback-key",
                "model": "gpt-fallback",
                "temperature": 0.2,
                "max_steps": 2,
            },
        }

        def fake_stream(_messages, llm_config, _tools):
            if llm_config["id"] == 5:
                raise RuntimeError(
                    "LLM 接口返回 403: AccessDenied.Unpurchased: Access to model denied"
                )
            yield "content", "已恢复"
            yield "message", {"content": "已恢复"}

        with (
            patch.object(
                stock_agent,
                "resolve_chat_model_config_ids",
                return_value=[5, 11],
                create=True,
            ),
            patch.object(
                stock_agent,
                "active_llm_config",
                side_effect=lambda model_id=None: configs[model_id or 5],
            ),
            patch.object(stock_agent, "_stream_chat", side_effect=fake_stream),
            patch.object(
                stock_agent,
                "persist_chat_session_model",
                create=True,
            ) as persist_model,
        ):
            events = list(
                stock_agent.run_agent_events(
                    "分析今天的持仓",
                    [],
                    model_config_id=5,
                    session_id=344,
                    user_id=8,
                )
            )

        self.assertFalse([event for event in events if event["type"] == "error"])
        self.assertEqual(
            [event["model"]["id"] for event in events if event["type"] == "model_selected"],
            [5, 11],
        )
        retry = next(event for event in events if event["type"] == "retrying")
        self.assertTrue(retry["reset_stream"])
        self.assertIn("自动切换", retry["message"])
        self.assertIn("已恢复", "".join(event.get("content", "") for event in events))
        persist_model.assert_called_once_with(session_id=344, user_id=8, model_config_id=11)


if __name__ == "__main__":
    unittest.main()
