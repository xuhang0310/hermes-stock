from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.app.agent_runtime.agentscope_engine import (
    _resilient_agent_class,
    _trim_context_without_model,
    _unfinished_answer_reason,
    run_agentscope,
)
from backend.app.agent_runtime.executor import AgentRunExecutor
from backend.app.market.digital_human_tools import digital_human_observation_preview
from backend.app.skills.registry import SkillRegistry

LLM = {
    "id": 1,
    "display_name": "test",
    "provider_name": "test",
    "base_url": "https://example.invalid/v1",
    "api_key": "test-key",
    "model": "test-model",
    "temperature": 0.2,
    "max_steps": 3,
}


class UnfinishedAnswerReasonTests(unittest.TestCase):
    def test_production_progress_notes_are_detected(self) -> None:
        notes = [
            "最新可用收盘价为 **56.53 元**，交易日 **2026-10-16**，跌 **0.93%**；当前非交易时段，因此以下先按最近收盘数据分析。"
            "接下来读取标准公司研究底稿和你的当前持仓/自选范围。",
            "最新行情已核对：浪潮信息最近可用收盘价为 56.53元。研究底稿显示股价处于弱势区间；"
            "我再补核对公告标题、技术指标和它是否属于你的持仓/自选范围。",
            "我已经把核心底稿补齐了。当前最关键的结论是业绩预期很强但股价下行。下面一次性给你完整报告，并把数据缺口写清楚。",
        ]
        for note in notes:
            with self.subTest(note=note[:20]):
                self.assertEqual(_unfinished_answer_reason(note, used_tools=True), "progress_note")

    def test_real_short_answers_are_left_alone(self) -> None:
        self.assertIsNone(_unfinished_answer_reason("最新收盘价 56.53 元（10 月 16 日），跌 0.93%。", used_tools=True))
        self.assertIsNone(_unfinished_answer_reason("你好呀，我在呢。接下来想聊哪只股票？", used_tools=False))
        long_answer = "从数据上看，" + "均线空头排列，" * 80 + "接下来盯 55 元。"
        self.assertIsNone(_unfinished_answer_reason(long_answer, used_tools=True))

    def test_empty_answer_always_needs_completion(self) -> None:
        self.assertEqual(_unfinished_answer_reason("  ", used_tools=False), "empty_answer")


class ContinuationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, replies: list[list]):
        calls: list = []

        class FakeModel:
            class Parameters:
                def __init__(self, **kwargs) -> None:
                    self.values = kwargs

            def __init__(self, **kwargs) -> None:
                self.values = kwargs

        class FakeAgent:
            def __init__(self, *, state, **kwargs) -> None:
                self.state = state
                self.kwargs = kwargs

            async def reply_stream(self, inputs):
                calls.append(inputs)
                for event in replies[min(len(calls), len(replies)) - 1]:
                    yield event

        emitted: list[dict] = []

        async def emit(event):
            emitted.append(event)

        async def save_checkpoint(_state_json, _waiting):
            return None

        async def is_cancelled():
            return False

        with (
            patch("backend.app.agent_runtime.agentscope_engine.active_llm_config", return_value=LLM),
            patch("backend.app.agent_runtime.agentscope_engine._build_definition", return_value=("system", [], set())),
            patch("backend.app.agent_runtime.agentscope_engine._build_tools", return_value=[]),
            patch("agentscope.model.OpenAIChatModel", FakeModel),
            patch("agentscope.agent.Agent", FakeAgent),
        ):
            outcome = await run_agentscope(
                run_id="run-continuation",
                request_payload={
                    "session_id": 1,
                    "user_id": 1,
                    "question": "分析一下浪潮信息",
                    "user_text": "分析一下浪潮信息",
                    "history": [],
                    "chat_mode": "research",
                    "digital_human_code": "stock-pm",
                },
                checkpoint_json=None,
                emit=emit,
                save_checkpoint=save_checkpoint,
                is_cancelled=is_cancelled,
            )
        return outcome, emitted, calls

    @staticmethod
    def _tool_then_text(text: str) -> list:
        from agentscope.event import (
            ReplyEndEvent,
            ReplyEndReason,
            TextBlockDeltaEvent,
            TextBlockEndEvent,
            TextBlockStartEvent,
            ToolCallDeltaEvent,
            ToolCallEndEvent,
            ToolCallStartEvent,
            ToolResultEndEvent,
        )
        from agentscope.message import ToolResultState

        events = [
            ToolCallStartEvent(reply_id="r", tool_call_id="call-price", tool_call_name="get_latest_price"),
            ToolCallDeltaEvent(reply_id="r", tool_call_id="call-price", delta='{"symbol": "000977.SZ"}'),
            ToolCallEndEvent(reply_id="r", tool_call_id="call-price"),
            ToolResultEndEvent(
                reply_id="r",
                tool_call_id="call-price",
                state=ToolResultState.SUCCESS,
                metadata={"result": {"price": 56.53}, "execution_id": None},
            ),
        ]
        if text:
            events += [
                TextBlockStartEvent(reply_id="r", block_id="answer"),
                TextBlockDeltaEvent(reply_id="r", block_id="answer", delta=text),
                TextBlockEndEvent(reply_id="r", block_id="answer"),
            ]
        events.append(ReplyEndEvent(session_id="1", reply_id="r", finished_reason=ReplyEndReason.COMPLETED))
        return events

    @staticmethod
    def _text(text: str) -> list:
        from agentscope.event import (
            ReplyEndEvent,
            ReplyEndReason,
            TextBlockDeltaEvent,
            TextBlockEndEvent,
            TextBlockStartEvent,
        )

        return [
            TextBlockStartEvent(reply_id="r2", block_id="final"),
            TextBlockDeltaEvent(reply_id="r2", block_id="final", delta=text),
            TextBlockEndEvent(reply_id="r2", block_id="final"),
            ReplyEndEvent(session_id="1", reply_id="r2", finished_reason=ReplyEndReason.COMPLETED),
        ]

    async def test_progress_note_is_continued_until_a_real_answer(self) -> None:
        final = "浪潮信息现在在一年来的底部区间，55 元是关键支撑。"
        outcome, emitted, calls = await self._run(
            [self._tool_then_text("最新价已核对。接下来读取标准公司研究底稿。"), self._text(final)]
        )

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.final_answer, final)
        self.assertEqual(len(calls), 2)
        self.assertIn("系统提示", calls[1].get_text_content())
        continuations = [event for event in emitted if event["type"] == "continuation_started"]
        self.assertEqual([event["reason"] for event in continuations], ["progress_note"])

    async def test_repeated_empty_answers_fail_visibly_instead_of_blank(self) -> None:
        outcome, emitted, calls = await self._run([self._tool_then_text("")])

        self.assertEqual(len(calls), 3)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("没有给出回答", outcome.error)
        self.assertEqual(
            [event["reason"] for event in emitted if event["type"] == "continuation_started"],
            ["empty_answer", "empty_answer"],
        )

    async def test_greeting_answer_is_not_continued(self) -> None:
        outcome, emitted, calls = await self._run([self._text("你好呀，我在呢。")])

        self.assertEqual(outcome.final_answer, "你好呀，我在呢。")
        self.assertEqual(len(calls), 1)
        self.assertFalse([event for event in emitted if event["type"] == "continuation_started"])


class CompressionFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_structured_summary_falls_back_to_trimming(self) -> None:
        from agentscope.agent import ContextConfig
        from agentscope.message import AssistantMsg, ToolCallBlock, UserMsg
        from agentscope.state import AgentState

        older = [
            UserMsg("user", "分析一下浪潮信息，我的成本是 63"),
            AssistantMsg("assistant", [ToolCallBlock(id="c1", name="get_company_research_bundle", input="{}")]),
        ]
        recent = [AssistantMsg("assistant", "最新价 56.53 元")]

        class StubAgent:
            def __init__(self) -> None:
                self.state = AgentState(session_id="s", context=[*older, *recent])
                self.model = SimpleNamespace(context_size=1000)
                self.kept = None

            async def _prepare_model_input(self):
                return {"tools": []}

            async def _split_context_for_compression(self, _reserved, _tools):
                return older, recent

            async def _clear_unreserved_read_cache(self, keep):
                self.kept = keep

        agent = StubAgent()
        await _trim_context_without_model(agent, ContextConfig(), ValueError("'task_overview' is a required property"))

        self.assertEqual(agent.state.context, recent)
        self.assertEqual(agent.kept, recent)
        self.assertIn("分析一下浪潮信息，我的成本是 63", agent.state.summary)
        self.assertIn("get_company_research_bundle", agent.state.summary)

    async def test_resilient_agent_catches_compression_errors_but_not_cancellation(self) -> None:
        class Base:
            context_config = "default-config"

            def __init__(self, error: BaseException) -> None:
                self.error = error

            async def _compress_context_impl(self, context_config=None, instructions=None):
                raise self.error

        agent_cls = _resilient_agent_class(Base)
        trim = AsyncMock()
        with patch("backend.app.agent_runtime.agentscope_engine._trim_context_without_model", trim):
            failing = agent_cls(ValueError("'context_to_preserve' is a required property"))
            await failing._compress_context_impl()
            trim.assert_awaited_once()
            self.assertIs(trim.await_args.args[0], failing)
            self.assertEqual(trim.await_args.args[1], "default-config")
            with self.assertRaises(asyncio.CancelledError):
                await agent_cls(asyncio.CancelledError())._compress_context_impl()


class RetryPolicyTests(unittest.TestCase):
    def test_production_transient_errors_are_retryable(self) -> None:
        for error in (
            "Upstream response stream was interrupted",
            "Upstream HTTP/2 stream failed",
            "Our servers are currently overloaded. Please try again later.",
            "Error code: 503 - {'error': {'message': 'Service temporarily unavailable'}}",
            "(pymysql.err.OperationalError) (1213, 'Deadlock found when trying to get lock; try restarting transaction')",
        ):
            with self.subTest(error=error[:30]):
                self.assertTrue(AgentRunExecutor._is_retryable(error))
        self.assertFalse(AgentRunExecutor._is_retryable("Error code: 401 - invalid api key"))
        self.assertFalse(AgentRunExecutor._is_retryable("'task_overview' is a required property"))

    def test_background_runs_wait_longer_than_chats(self) -> None:
        self.assertLess(AgentRunExecutor._retry_delay_seconds({"attempts": 1, "source_type": "web"}), 2)
        self.assertGreaterEqual(AgentRunExecutor._retry_delay_seconds({"attempts": 1, "source_type": "scheduler"}), 20)
        self.assertGreaterEqual(AgentRunExecutor._retry_delay_seconds({"attempts": 2, "source_type": "workflow"}), 60)
        self.assertLessEqual(AgentRunExecutor._retry_delay_seconds({"attempts": 5, "source_type": "scheduler"}), 61)


class ToolAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = SkillRegistry()
        self.registry.discover()

    def names(self, chat_mode: str, role_code: str | None = None) -> set[str]:
        return {item["function"]["name"] for item in self.registry.get_tools_for(chat_mode, role_code=role_code)}

    def test_stock_mode_gets_the_tools_its_prompt_promises(self) -> None:
        stock = self.names("stock")
        for tool in ("search_stock_news", "search_comprehensive_intel", "analyze_trend", "calculate_ma",
                     "get_volume_analysis", "analyze_pattern", "get_chip_distribution", "get_capital_flow"):
            self.assertIn(tool, stock)

    def test_research_roles_still_scope_specialist_tools(self) -> None:
        self.assertIn("calculate_ma", self.names("research", "stock-pm"))
        self.assertIn("search_stock_news", self.names("research", "research-analyst"))
        self.assertNotIn("search_stock_news", self.names("research", "risk-officer"))
        self.assertNotIn("list_factor_alphas", self.names("stock"))


class ResearchBundlePreviewTests(unittest.TestCase):
    def test_preview_counts_available_dimensions(self) -> None:
        preview = digital_human_observation_preview(
            "get_company_research_bundle",
            {
                "symbol": "000977.SZ",
                "data_as_of": "20261016",
                "data_availability": {
                    "market": {"available": True},
                    "financial": {"available": True},
                    "valuation": {"available": True},
                    "events": {"available": False},
                    "technical": {"available": True},
                    "capital": {"available": False},
                    "counter_view": {"available": True},
                },
            },
        )
        self.assertIn("七维覆盖 5/7", preview)


class InteractiveOrchestrationTests(unittest.TestCase):
    def _prompt(self, source_type: str) -> str:
        from backend.app.agent_runtime.agentscope_engine import _build_definition

        payload = {
            "chat_mode": "research",
            "digital_human_code": "stock-pm",
            "user_id": 1,
            "session_id": 1,
            "user_text": "浪潮信息今年还能上90吗？",
            "source_type": source_type,
        }
        with patch(
            "backend.app.agent_runtime.agentscope_engine.artifact_service.active_context",
            return_value="",
        ):
            return _build_definition(payload)[0]

    def test_chat_questions_are_answered_directly_but_schedules_keep_workflows(self) -> None:
        self.assertIn("实时对话规则", self._prompt("web"))
        self.assertNotIn("实时对话规则", self._prompt("scheduler"))


if __name__ == "__main__":
    unittest.main()
