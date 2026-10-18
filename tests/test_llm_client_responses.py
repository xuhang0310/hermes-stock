from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.app.ai.llm_client import (
    _messages_to_input,
    _normalize_response,
    _responses_url,
    _tools_to_responses,
    parse_sse_stream,
)


class ResponsesAdapterTests(unittest.TestCase):
    @patch("backend.app.ai.ai_api.chat_completion")
    def test_model_connectivity_check_uses_normalized_response_shape(self, completion) -> None:
        from backend.app.ai.ai_api import _test_openai_chat

        completion.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "output_text": "ok",
        }
        result = _test_openai_chat("https://example.invalid/v1", "key", "test-model")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["response_preview"], "ok")
        self.assertEqual(completion.call_args.args[1]["model"], "test-model")

    def test_url_normalization_removes_legacy_suffix(self) -> None:
        self.assertEqual(_responses_url("https://example.invalid/v1"), "https://example.invalid/v1/responses")
        self.assertEqual(
            _responses_url("https://example.invalid/v1/chat/completions"),
            "https://example.invalid/v1/responses",
        )

    def test_history_tool_and_multimodal_conversion(self) -> None:
        converted = _messages_to_input(
            [
                {"role": "user", "content": [{"type": "text", "text": "看图"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{\"symbol\":\"600519.SH\"}"}}],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "{\"price\": 100}"},
            ]
        )
        self.assertEqual(converted[0]["content"][0]["type"], "input_text")
        self.assertEqual(converted[0]["content"][1]["type"], "input_image")
        self.assertEqual(converted[1]["type"], "function_call")
        self.assertEqual(converted[2]["type"], "function_call_output")

    def test_tool_schema_conversion(self) -> None:
        tools = _tools_to_responses(
            [{"type": "function", "function": {"name": "lookup", "description": "查行情", "parameters": {"type": "object"}}}]
        )
        self.assertEqual(tools, [{"type": "function", "name": "lookup", "description": "查行情", "parameters": {"type": "object"}}])

    def test_response_output_is_adapted_to_choices(self) -> None:
        data = _normalize_response(
            {
                "id": "resp_1",
                "output_text": "完成",
                "output": [
                    {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{\"x\":1}"}
                ],
            }
        )
        message = data["choices"][0]["message"]
        self.assertEqual(message["content"], "完成")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "lookup")

    def test_responses_sse_text_and_function_call(self) -> None:
        lines = [
            'event: response.output_text.delta',
            'data: {"type":"response.output_text.delta","delta":"你好"}',
            'event: response.output_item.added',
            'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"lookup"}}',
            'event: response.function_call_arguments.delta',
            'data: {"type":"response.function_call_arguments.delta","item_id":"fc_1","delta":"{\\"x\\":1}"}',
            'event: response.completed',
            'data: {"type":"response.completed","response":{"output":[{"type":"message","content":[{"type":"output_text","text":"你好"}]},{"type":"function_call","call_id":"call_1","name":"lookup","arguments":"{\\"x\\":1}"}]}}',
        ]
        events = list(parse_sse_stream(lines))
        self.assertEqual([kind for kind, _ in events], ["content", "tool_args", "message"])
        message = events[-1][1]
        self.assertEqual(message["content"], "你好")
        self.assertEqual(message["tool_calls"][0]["id"], "call_1")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "lookup")


if __name__ == "__main__":
    unittest.main()
