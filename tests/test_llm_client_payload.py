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


if __name__ == "__main__":
    unittest.main()
