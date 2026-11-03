"""数字人三处优化的回归测试：长期记忆不存持仓快照、模型出错自动换备用模型、角色提示词只有一个来源。

都是纯函数/桩对象测试，不连 MySQL。
"""
from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from backend.app.agent_runtime.executor import AgentRunExecutor, exception_text
from backend.app.ai import memory
from backend.app.market.digital_human_tools import (
    ROLE_SKILLS,
    default_role_prompt,
    digital_human_skill_prompt,
    is_role_profile,
)


class VolatileMemoryTests(unittest.TestCase):
    def test_holding_snapshots_from_production_are_volatile(self) -> None:
        for content in (
            "用户当前持仓仅剩平安银行（000001.SZ）1000股，成本价10.20元，仓位占比100%。",
            "用户当前持仓平安银行(000001.SZ)，自选池包含贵州茅台(600519.SH)。",
            "用户拥有3个券商账户（方正、广发、中信），总资产约339万，持有9只标的，可用现金仅约2.05万。",
            "用户持仓包括平安银行、盈康生命、海象新材、友阿股份。",
            "当前用户自选池包含建设机械（600984.SH），不包含比亚迪（002594.SZ）。",
            "用户关注平安银行，系统/对话中显示其止损价为9.89",
        ):
            with self.subTest(content=content[:20]):
                self.assertTrue(memory.is_volatile_memory("fact", content))

    def test_stable_facts_and_user_rules_are_kept(self) -> None:
        kept = (
            ("fact", "用户使用的股票交易券商是方正证券。"),
            ("fact", "用户有三个证券账户，要求逐账户管理持仓、现金与策略，不跨账户混用持仓、资金或买入预算。"),
            ("fact", "通常预计在晚上22:00前完成系统内持仓信息的更新。"),
            ("decision", "未另给预算时，每只股票首次买入不超过该账户总资产的5%；不知道账户现金时只给买入价，不给股数。"),
            ("preference", "用户希望持仓建议给出具体股数和价格。"),
        )
        for memory_type, content in kept:
            with self.subTest(content=content[:20]):
                self.assertFalse(memory.is_volatile_memory(memory_type, content))

    def test_archive_stale_memories_marks_snapshots_and_old_gaps(self) -> None:
        now = datetime(2026, 11, 3, 12, 0)

        def row(memory_id: int, memory_type: str, content: str, updated: datetime, accessed: datetime | None = None):
            return SimpleNamespace(
                id=memory_id,
                memory_type=memory_type,
                content=content,
                updated_at=updated,
                created_at=updated,
                last_accessed_at=accessed,
                status="active",
            )

        rows = [
            row(1, "fact", "用户当前持有平安银行1000股，成本价10.20元。", datetime(2026, 10, 30)),
            row(2, "gap", "用户想知道同力天启的解禁时间。", datetime(2026, 8, 1)),
            row(3, "fact", "用户是一名量化分析师。", datetime(2026, 3, 1), datetime(2026, 6, 1)),
            row(4, "fact", "用户使用的股票交易券商是方正证券。", datetime(2026, 3, 1), datetime(2026, 11, 1)),
            row(5, "preference", "用户希望回答先给结论。", datetime(2026, 1, 1)),
        ]
        commits: list[bool] = []
        db = SimpleNamespace(
            scalars=lambda _stmt: SimpleNamespace(all=lambda: rows),
            commit=lambda: commits.append(True),
        )
        with patch.object(memory, "_delete_memory_vector") as delete_vector:
            archived = memory.archive_stale_memories(db, user_id=1, now=now)
        self.assertEqual(archived, 3)
        self.assertEqual([r.status for r in rows], ["archived", "archived", "archived", "active", "active"])
        self.assertEqual(commits, [True])
        self.assertEqual(delete_vector.call_count, 3)


class ModelFallbackTests(unittest.TestCase):
    def test_relay_400_switches_immediately(self) -> None:
        error = (
            "Error code: 400 - {'error': {'message': '生成失败: 任务处理失败', "
            "'type': 'toapis_error', 'code': 'unknown_error'}}"
        )
        self.assertTrue(AgentRunExecutor.should_switch_model(error, attempts=1))

    def test_transient_errors_retry_same_model_once_first(self) -> None:
        for error in (
            "Error code: 503 - {'error': {'message': 'Service temporarily unavailable'}}",
            "Upstream response stream was interrupted",
            "ReadTimeout",
        ):
            with self.subTest(error=error[:20]):
                self.assertFalse(AgentRunExecutor.should_switch_model(error, attempts=1))
                self.assertTrue(AgentRunExecutor.should_switch_model(error, attempts=2))

    def test_non_model_failures_never_switch(self) -> None:
        for error in (
            "Agent 执行超时，任务已停止。",
            "(pymysql.err.OperationalError) (1213, 'Deadlock found when trying to get lock')",
            "'task_overview' is a required property",
        ):
            with self.subTest(error=error[:20]):
                self.assertFalse(AgentRunExecutor.should_switch_model(error, attempts=2))

    def test_next_model_skips_failed_models_and_stops_after_two_switches(self) -> None:
        labels = {14: "gpt-6.1-sol", 16: "claude-opus-5-5", 5: "百炼 Qwen3.7 Plus"}

        def resolve(model_config_id):
            order = [14, 16, 5, 15]
            if model_config_id in order:
                order.remove(model_config_id)
                order.insert(0, model_config_id)
            return order

        with patch("backend.app.core.llm_config.resolve_chat_model_config_ids", side_effect=resolve), patch(
            "backend.app.core.llm_config.active_llm_config",
            side_effect=lambda model_id: {"display_name": labels.get(model_id, str(model_id))},
        ):
            first = AgentRunExecutor.next_model_choice({"model_config_id": None})
            self.assertEqual(first, (16, [14], "gpt-6.1-sol", "claude-opus-5-5"))
            second = AgentRunExecutor.next_model_choice({"model_config_id": 16, "failed_model_config_ids": [14]})
            self.assertEqual(second, (5, [14, 16], "claude-opus-5-5", "百炼 Qwen3.7 Plus"))
            self.assertIsNone(
                AgentRunExecutor.next_model_choice({"model_config_id": 5, "failed_model_config_ids": [14, 16]})
            )

    def test_empty_exception_message_keeps_the_type(self) -> None:
        class ReadTimeout(Exception):
            pass

        self.assertEqual(exception_text(ReadTimeout()), "ReadTimeout")
        self.assertEqual(exception_text(RuntimeError("boom")), "boom")


class RolePromptSourceTests(unittest.TestCase):
    def test_default_prompt_comes_from_role_skills(self) -> None:
        for code, role in ROLE_SKILLS.items():
            with self.subTest(role=code):
                prompt = default_role_prompt(code)
                self.assertIn(role["name"], prompt)
                self.assertIn("回答要讲到", prompt)
                self.assertTrue(is_role_profile(code))
        self.assertFalse(is_role_profile("ask_stock"))
        self.assertFalse(is_role_profile("position-entry-agent"))

    def test_saved_prompt_replaces_default_but_fixed_rules_stay(self) -> None:
        prompt = digital_human_skill_prompt("stock-pm", role_prompt="你是总调度。只回答用户问的事。")
        self.assertIn("你是总调度。只回答用户问的事。", prompt)
        self.assertNotIn(ROLE_SKILLS["stock-pm"]["skills"][0], prompt)
        # 持仓写入的硬规则跟工具绑定，不随角色提示词编辑丢失
        self.assertIn("confirmed=true", prompt)
        self.assertIn("不是固定标题", prompt)

    def test_empty_saved_prompt_falls_back_to_default(self) -> None:
        prompt = digital_human_skill_prompt("risk-officer", role_prompt="   ")
        self.assertIn(default_role_prompt("risk-officer"), prompt)


if __name__ == "__main__":
    unittest.main()
