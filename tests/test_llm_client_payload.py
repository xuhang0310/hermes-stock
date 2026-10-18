from __future__ import annotations

import unittest

from backend.app.ai.llm_client import _build_payload


class LlmClientPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "base_url": "https://example.invalid/v1",
            "api_key": "test-key",
            "model": "qwen3.7-plus",
            "temperature": 0.2,
        }

    def test_thinking_setting_is_omitted_by_default(self) -> None:
        _, payload, _ = _build_payload(
            self.config,
            [{"role": "user", "content": "hello"}],
            tools=None,
            tool_choice=None,
            temperature=None,
            stream=False,
        )

        self.assertNotIn("enable_thinking", payload)
        self.assertNotIn("reasoning_effort", payload)

    def test_thinking_can_be_disabled_for_one_call(self) -> None:
        _, payload, _ = _build_payload(
            self.config,
            [{"role": "user", "content": "image"}],
            tools=[{"type": "function", "function": {"name": "extract"}}],
            tool_choice="required",
            temperature=None,
            stream=False,
            enable_thinking=False,
        )

        self.assertIs(payload["enable_thinking"], False)
        self.assertEqual(payload["tool_choice"], "required")

    def test_thinking_can_be_enabled_for_one_call(self) -> None:
        _, payload, _ = _build_payload(
            self.config,
            [{"role": "user", "content": "deep research"}],
            tools=None,
            tool_choice=None,
            temperature=None,
            stream=True,
            enable_thinking=True,
        )

        self.assertIs(payload["enable_thinking"], True)

    def test_reasoning_effort_is_passed_top_level(self) -> None:
        _, payload, _ = _build_payload(
            self.config,
            [{"role": "user", "content": "深度推理"}],
            tools=None,
            tool_choice=None,
            temperature=None,
            stream=True,
            reasoning_effort="high",
        )

        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertNotIn("enable_thinking", payload)


if __name__ == "__main__":
    unittest.main()
