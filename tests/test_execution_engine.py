"""Agent 任务执行引擎测试

测试三层：
1. ScheduledTaskExtractor — 校验逻辑、转换、Agent 代码查询
2. Worker 串行约束 — next_queued_run 的 assignee_profile_id 串行保证
3. Worker 孤儿恢复 — _recover_orphan_board_tasks 扫描 queued 超 5 分钟的任务
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.core.models import (
    AiChatMessage,
    AiChatSession,
    AgentRun,
    DigitalHumanProfile,
    JobRun,
    ScheduledJob,
    User,
)


def _make_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


class TaskExtractorValidateTests(unittest.TestCase):
    """测试 ScheduledTaskExtractor._validate 逻辑（纯函数，无 DB）"""

    def setUp(self) -> None:
        from backend.app.quant.task_extractor import ScheduledTaskExtractor
        self.extractor = ScheduledTaskExtractor()
        self.agent_codes = {
            "stock-pm": "总调度",
            "intelligence-officer": "情报官",
            "risk-officer": "风控官",
        }

    def test_validate_empty_tasks(self) -> None:
        result = self.extractor._validate(
            {"has_tasks": False, "confidence": 1.0, "tasks": [], "warnings": []},
            self.agent_codes,
            max_tasks=20,
        )
        self.assertFalse(result["has_tasks"])
        self.assertEqual(result["tasks"], [])

    def test_validate_has_tasks_normal(self) -> None:
        raw = {
            "has_tasks": True,
            "confidence": 0.95,
            "tasks": [
                {
                    "action_key": "verify-600984-20260801",
                    "title": "新闻核验",
                    "agent_code": "intelligence-officer",
                    "instruction": "搜索今日新闻",
                    "expected_output": "JSON 列表",
                    "required": True,
                    "priority": 200,
                }
            ],
            "warnings": [],
        }
        result = self.extractor._validate(raw, self.agent_codes, max_tasks=20)
        self.assertTrue(result["has_tasks"])
        self.assertEqual(len(result["tasks"]), 1)
        task = result["tasks"][0]
        self.assertEqual(task["action_key"], "verify-600984-20260801")
        self.assertEqual(task["agent_code"], "intelligence-officer")
        self.assertEqual(task["priority"], 200)

    def test_validate_rejects_unknown_agent_code(self) -> None:
        raw = {
            "has_tasks": True,
            "confidence": 0.9,
            "tasks": [
                {
                    "action_key": "test-key",
                    "title": "测试",
                    "agent_code": "nonexistent-agent",
                    "instruction": "不存在",
                }
            ],
            "warnings": [],
        }
        result = self.extractor._validate(raw, self.agent_codes, max_tasks=20)
        self.assertFalse(result["has_tasks"])
        self.assertEqual(result["tasks"], [])
        self.assertTrue(any("不存在" in w for w in result["warnings"]))

    def test_validate_rejects_duplicate_action_key(self) -> None:
        raw = {
            "has_tasks": True,
            "confidence": 0.9,
            "tasks": [
                {
                    "action_key": "dup-key",
                    "title": "任务 A",
                    "agent_code": "intelligence-officer",
                    "instruction": "指令 A",
                },
                {
                    "action_key": "dup-key",
                    "title": "任务 B",
                    "agent_code": "risk-officer",
                    "instruction": "指令 B",
                },
            ],
            "warnings": [],
        }
        result = self.extractor._validate(raw, self.agent_codes, max_tasks=20)
        self.assertEqual(len(result["tasks"]), 1)
        self.assertTrue(any("重复" in w for w in result["warnings"]))

    def test_validate_rejects_missing_required_fields(self) -> None:
        raw = {
            "has_tasks": True,
            "confidence": 0.9,
            "tasks": [
                {
                    "action_key": "",
                    "title": "缺 action_key",
                    "agent_code": "intelligence-officer",
                    "instruction": "指令",
                },
                {
                    "action_key": "valid-key",
                    "title": "",
                    "agent_code": "intelligence-officer",
                    "instruction": "缺 title",
                },
            ],
            "warnings": [],
        }
        result = self.extractor._validate(raw, self.agent_codes, max_tasks=20)
        self.assertFalse(result["has_tasks"])
        self.assertEqual(result["tasks"], [])

    def test_validate_truncates_at_max_tasks(self) -> None:
        tasks = [
            {
                "action_key": f"key-{i}",
                "title": f"任务 {i}",
                "agent_code": "intelligence-officer",
                "instruction": f"指令 {i}",
            }
            for i in range(30)
        ]
        raw = {"has_tasks": True, "confidence": 0.9, "tasks": tasks, "warnings": []}
        result = self.extractor._validate(raw, self.agent_codes, max_tasks=5)
        self.assertEqual(len(result["tasks"]), 5)
        self.assertTrue(any("上限" in w for w in result["warnings"]))

    def test_validate_non_boolean_has_tasks(self) -> None:
        raw = {"has_tasks": "yes", "confidence": 0.9, "tasks": [], "warnings": []}
        result = self.extractor._validate(raw, self.agent_codes, max_tasks=20)
        self.assertFalse(result["has_tasks"])

    def test_validate_tasks_not_list(self) -> None:
        raw = {"has_tasks": True, "confidence": 0.9, "tasks": "not a list", "warnings": []}
        result = self.extractor._validate(raw, self.agent_codes, max_tasks=20)
        self.assertFalse(result["has_tasks"])
        self.assertTrue(any("不是数组" in w for w in result["warnings"]))


class TaskExtractorToBatchTests(unittest.TestCase):
    """测试 ScheduledTaskExtractor.to_task_batch 转换"""

    def setUp(self) -> None:
        from backend.app.quant.task_extractor import ScheduledTaskExtractor
        from backend.app.quant.agent_board_service import TaskBatchSpec
        self.extractor = ScheduledTaskExtractor()
        self.TaskBatchSpec = TaskBatchSpec

    def test_to_task_batch_converts_plan(self) -> None:
        plan = {
            "has_tasks": True,
            "confidence": 0.95,
            "tasks": [
                {
                    "action_key": "verify-600984",
                    "title": "新闻核验",
                    "agent_code": "intelligence-officer",
                    "instruction": "搜索今日新闻",
                    "expected_output": "JSON 列表",
                    "required": True,
                    "priority": 200,
                },
                {
                    "action_key": "risk-check",
                    "title": "风险复核",
                    "agent_code": "risk-officer",
                    "instruction": "评估风险",
                    "required": False,
                    "priority": 50,
                },
            ],
            "warnings": [],
        }
        batch = self.extractor.to_task_batch(
            plan,
            objective="定时任务协同",
            completion_policy="best_effort",
            child_timeout_seconds=300,
        )
        self.assertIsInstance(batch, self.TaskBatchSpec)
        self.assertEqual(batch.objective, "定时任务协同")
        self.assertEqual(batch.completion_policy, "best_effort")
        self.assertEqual(batch.child_timeout_seconds, 300)
        self.assertEqual(len(batch.tasks), 2)
        self.assertEqual(batch.tasks[0].action_key, "verify-600984")
        self.assertEqual(batch.tasks[0].priority, 200)
        self.assertFalse(batch.tasks[1].required)

    def test_to_task_batch_empty_tasks(self) -> None:
        plan = {"has_tasks": False, "confidence": 1.0, "tasks": [], "warnings": []}
        batch = self.extractor.to_task_batch(plan)
        self.assertEqual(len(batch.tasks), 0)


class TaskExtractorExtractTests(unittest.TestCase):
    """测试 ScheduledTaskExtractor.extract 完整流程（mock LLM）"""

    def setUp(self) -> None:
        self.engine = _make_engine()
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)

        with self.SessionFactory() as db:
            user = User(
                username="extractor-test",
                display_name="Tester",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            db.add(user)
            db.flush()
            self.user_id = user.id

            agent = DigitalHumanProfile(
                code="intelligence-officer",
                name="情报官",
                avatar_tone="evidence",
                target_roles="*",
                description="新闻检索",
                system_prompt="核验新闻",
                quick_prompts_json="[]",
                status="enabled",
                sort_order=1,
                delegation_enabled=True,
            )
            db.add(agent)
            db.flush()
            self.agent_id = agent.id

            # 创建 ScheduledJob（使用正确字段名）
            job = ScheduledJob(
                job_key="test-job",
                name="测试任务",
                job_kind="agent_report",
                schedule_type="once",
                timezone="Asia/Shanghai",
                owner_user_id=self.user_id,
                digital_human_code="intelligence-officer",
                prompt_template="测试指令",
                enabled=True,
            )
            db.add(job)
            db.flush()
            self.job_key = job.job_key

            # 创建 JobRun（使用正确字段名）
            job_run = JobRun(
                run_id="test-job-run-001",
                job_key=self.job_key,
                fire_key="fire-001",
                trigger_type="schedule",
                status="running",
                started_at=datetime.now(),
            )
            db.add(job_run)
            db.commit()
            self.job_run_id = job_run.run_id

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_extract_empty_content(self) -> None:
        from backend.app.quant.task_extractor import ScheduledTaskExtractor
        extractor = ScheduledTaskExtractor()
        result = extractor.extract("", job_run_id=self.job_run_id)
        self.assertFalse(result["has_tasks"])
        self.assertIn("报告内容为空", result["warnings"])

    def test_report_excerpt_preserves_trailing_task_list(self) -> None:
        from backend.app.quant.task_extractor import _report_excerpt

        report = "context-" + ("x" * 20_000) + "-负责 Agent-待完成"
        excerpt = _report_excerpt(report)

        self.assertLess(len(excerpt), len(report))
        self.assertTrue(excerpt.startswith("context-"))
        self.assertIn("负责 Agent", excerpt)
        self.assertTrue(excerpt.endswith("待完成"))

    def test_explicit_task_table_is_parsed_without_llm(self) -> None:
        from backend.app.quant.task_extractor import ScheduledTaskExtractor

        report = """
| # | 动作 | 负责 Agent | 状态 |
|---|------|-----------|------|
| 1 | 读建设机械整改报告全文 | 研究员 | ❌ 待完成 |
| 2 | 为持仓设置止损价 | 风控官 | ❌ 待完成 |
"""
        plan = ScheduledTaskExtractor()._extract_explicit_task_table(
            report,
            agent_codes={
                "research-analyst": "个股研究员",
                "risk-officer": "风控复盘官",
            },
            job_run_id="abcdef1234567890-job",
            max_tasks=20,
        )

        self.assertTrue(plan["has_tasks"])
        self.assertEqual(plan["confidence"], 1.0)
        self.assertEqual(len(plan["tasks"]), 2)
        self.assertEqual(plan["tasks"][0]["agent_code"], "research-analyst")
        self.assertEqual(plan["tasks"][1]["agent_code"], "risk-officer")
        self.assertEqual(plan["tasks"][0]["action_key"], "scheduled-abcdef1234567890-1")

    def test_extract_no_agents_available(self) -> None:
        """Mock _get_agent_codes 返回空，测试无可用 Agent 分支"""
        from backend.app.quant.task_extractor import ScheduledTaskExtractor
        extractor = ScheduledTaskExtractor()

        with patch.object(extractor, "_get_agent_codes", return_value={}):
            result = extractor.extract("一些报告内容", job_run_id=self.job_run_id)
        self.assertFalse(result["has_tasks"])
        self.assertIn("没有可用的数字人", result["warnings"])

    def test_extract_with_mocked_llm(self) -> None:
        """Mock LLM 返回，测试完整提取流程"""
        from backend.app.quant.task_extractor import ScheduledTaskExtractor
        extractor = ScheduledTaskExtractor()

        llm_response = {
            "has_tasks": True,
            "confidence": 0.95,
            "tasks": [
                {
                    "action_key": "verify-news-20260801",
                    "title": "新闻核验",
                    "agent_code": "intelligence-officer",
                    "instruction": "搜索今日重要新闻并核验来源",
                    "expected_output": "JSON 格式新闻列表",
                    "required": True,
                    "priority": 200,
                }
            ],
            "warnings": [],
        }

        with patch.object(extractor, "_get_agent_codes", return_value={
            "intelligence-officer": "情报官",
        }):
            with patch.object(extractor, "_call_llm", return_value=llm_response):
                with patch.object(
                    extractor, "_save_extraction_plan"
                ) as mock_save:
                    result = extractor.extract(
                        "今日市场概况：三大指数上涨...建议情报官核验以下新闻来源...",
                        job_run_id=self.job_run_id,
                    )

        self.assertTrue(result["has_tasks"])
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["agent_code"], "intelligence-officer")
        mock_save.assert_called_once()

    def test_extract_llm_failure(self) -> None:
        """LLM 调用失败时返回空结果"""
        from backend.app.quant.task_extractor import ScheduledTaskExtractor
        extractor = ScheduledTaskExtractor()

        with patch.object(extractor, "_get_agent_codes", return_value={
            "intelligence-officer": "情报官",
        }):
            with patch.object(extractor, "_call_llm", side_effect=RuntimeError("API 超时")):
                result = extractor.extract("报告内容", job_run_id=self.job_run_id)

        self.assertFalse(result["has_tasks"])
        self.assertTrue(any("LLM 调用失败" in w for w in result["warnings"]))

    @patch("backend.app.quant.task_extractor.active_llm_config")
    @patch("backend.app.ai.llm_client.chat_completion")
    def test_call_llm_uses_mapping_model_config(self, completion, active_config) -> None:
        """active_llm_config returning a dict remains compatible with extraction."""
        from backend.app.quant.task_extractor import ScheduledTaskExtractor

        active_config.return_value = {
            "base_url": "https://example.invalid/v1",
            "api_key": "secret",
            "model": "test-model",
        }
        completion.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"has_tasks": false, "confidence": 1.0, "tasks": [], "warnings": []}'
                    }
                }
            ]
        }

        result = ScheduledTaskExtractor()._call_llm("extract tasks")

        self.assertFalse(result["has_tasks"])
        completion.assert_called_once()
        self.assertEqual(completion.call_args.args[1]["model"], "test-model")
        self.assertEqual(completion.call_args.kwargs["timeout"], 60.0)

    def test_save_extraction_plan_finds_job_run_by_public_run_id(self) -> None:
        from backend.app.quant.task_extractor import ScheduledTaskExtractor

        extractor = ScheduledTaskExtractor()
        plan = {
            "has_tasks": True,
            "confidence": 0.95,
            "tasks": [],
            "warnings": [],
        }

        with self.SessionFactory() as db:
            extractor._save_extraction_plan(db, self.job_run_id, plan)
            db.commit()

        with self.SessionFactory() as db:
            job_run = db.scalar(
                select(JobRun).where(JobRun.run_id == self.job_run_id)
            )
            self.assertIsNotNone(job_run)
            self.assertEqual(json.loads(job_run.handoff_plan_json), plan)

    def test_scheduled_agent_links_leader_by_public_job_run_id(self) -> None:
        from backend.app.scheduler.rhythm_scheduler import _run_scheduled_agent

        snapshot = {
            "job_key": self.job_key,
            "name": "测试任务",
            "digital_human_code": "intelligence-officer",
            "owner_user_id": self.user_id,
            "allow_code_execution": False,
            "approval_policy": "full",
            "delegation_mode": "execute_and_wait",
            "task_extraction_mode": "structured_tool",
            "completion_policy": "all_required",
            "child_timeout_seconds": 600,
            "max_delegated_tasks": 20,
        }
        leader_run_id = "leader-agent-run-001"

        # Scheduled reports are independent executions.  Historical messages
        # remain in the scheduler session for audit, but must not be sent back
        # to the model or failed daily prompts accumulate as fake backlog.
        with self.SessionFactory() as db:
            scheduler_session = AiChatSession(
                user_id=self.user_id,
                digital_human_id=self.agent_id,
                title="定时任务 · 测试任务",
                status="active",
                chat_mode="research",
                source_type="scheduler",
                external_conversation_id=self.job_key,
                source_label="定时任务",
            )
            db.add(scheduler_session)
            db.flush()
            db.add_all(
                [
                    AiChatMessage(
                        session_id=scheduler_session.id,
                        role="user",
                        content="昨天失败后遗留的任务",
                        event_count=0,
                    ),
                    AiChatMessage(
                        session_id=scheduler_session.id,
                        role="assistant",
                        content="昨天的报告",
                        event_count=0,
                    ),
                ]
            )
            db.commit()

        build_request = MagicMock(return_value={"request": "payload"})

        with (
            patch("backend.app.scheduler.rhythm_scheduler.SessionLocal", self.SessionFactory),
            patch(
                "backend.app.scheduler.rhythm_scheduler.build_channel_request",
                build_request,
            ),
            patch(
                "backend.app.scheduler.rhythm_scheduler.create_channel_run",
                return_value={"run_id": leader_run_id, "created": True},
            ),
            patch(
                "backend.app.scheduler.rhythm_scheduler.wait_for_channel_run",
                return_value=("report", "model", None),
            ) as wait_for_channel_run_mock,
        ):
            answer, model_name = _run_scheduled_agent(
                snapshot,
                "生成盘前简报",
                self.job_run_id,
            )

        self.assertEqual((answer, model_name), ("report", "model"))
        self.assertEqual(build_request.call_args.kwargs["history"], [])
        wait_call = wait_for_channel_run_mock.call_args
        self.assertEqual(wait_call.kwargs["timeout_seconds"], 930)
        self.assertEqual(wait_call.kwargs["hard_timeout_seconds"], 2460)
        with self.SessionFactory() as db:
            job_run = db.scalar(
                select(JobRun).where(JobRun.run_id == self.job_run_id)
            )
            self.assertIsNotNone(job_run)
            self.assertEqual(job_run.leader_agent_run_id, leader_run_id)
            scheduler_session = db.scalar(
                select(AiChatSession).where(
                    AiChatSession.source_type == "scheduler",
                    AiChatSession.external_conversation_id == self.job_key,
                )
            )
            messages = db.scalars(
                select(AiChatMessage)
                .where(AiChatMessage.session_id == scheduler_session.id)
                .order_by(AiChatMessage.id)
            ).all()
            self.assertEqual(
                [message.content for message in messages],
                ["昨天失败后遗留的任务", "昨天的报告", "生成盘前简报"],
            )


class SerialConstraintTests(unittest.TestCase):
    """测试 next_queued_run 的串行约束（同一 assignee_profile_id 不能有多个 running）"""

    def setUp(self) -> None:
        self.engine = _make_engine()
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)

        with self.SessionFactory() as db:
            user = User(
                username="serial-test",
                display_name="Tester",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            db.add(user)
            db.flush()
            self.user_id = user.id

            # 两个独立 session，避免 session 级串行约束干扰 agent 级约束测试
            session_a = AiChatSession(
                user_id=self.user_id,
                digital_human_id=0,
                title="session A",
                chat_mode="research",
                source_type="web",
            )
            session_b = AiChatSession(
                user_id=self.user_id,
                digital_human_id=0,
                title="session B",
                chat_mode="research",
                source_type="web",
            )
            db.add_all([session_a, session_b])
            db.flush()
            self.session_a_id = session_a.id
            self.session_b_id = session_b.id

            self.agent_a = DigitalHumanProfile(
                code="agent-a",
                name="Agent A",
                avatar_tone="calm",
                target_roles="*",
                description="A",
                system_prompt="A",
                quick_prompts_json="[]",
                status="enabled",
                sort_order=1,
                delegation_enabled=True,
            )
            self.agent_b = DigitalHumanProfile(
                code="agent-b",
                name="Agent B",
                avatar_tone="calm",
                target_roles="*",
                description="B",
                system_prompt="B",
                quick_prompts_json="[]",
                status="enabled",
                sort_order=2,
                delegation_enabled=True,
            )
            db.add_all([self.agent_a, self.agent_b])
            db.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    def _make_repo(self):
        from backend.app.agent_runtime.repository import AgentRunRepository
        return AgentRunRepository(self.SessionFactory)

    def test_no_running_returns_queued(self) -> None:
        """无 running 任务时返回 queued 任务"""
        with self.SessionFactory() as db:
            db.add(AgentRun(
                run_id="queued-001",
                session_id=self.session_a_id,
                user_id=self.user_id,
                engine="task_board",
                chat_mode="stock",
                status="queued",
                priority=100,
                assignee_profile_id=self.agent_a.id,
                request_json="{}",
                board_visible=True,
                queued_at=datetime.now(),
            ))
            db.commit()

        repo = self._make_repo()
        result = repo.next_queued_run()
        self.assertEqual(result, "queued-001")

    def test_running_blocks_same_agent(self) -> None:
        """同一 Agent 有 running 任务时，其他 queued 任务不应被选取"""
        with self.SessionFactory() as db:
            db.add_all([
                AgentRun(
                    run_id="running-001",
                    session_id=self.session_a_id,
                    user_id=self.user_id,
                    engine="task_board",
                    chat_mode="stock",
                    status="running",
                    priority=100,
                    assignee_profile_id=self.agent_a.id,
                    request_json="{}",
                    board_visible=True,
                    started_at=datetime.now(),
                ),
                AgentRun(
                    run_id="queued-blocked",
                    # 不同 session 以隔离 session 级约束，只测 agent 级串行
                    session_id=self.session_b_id,
                    user_id=self.user_id,
                    engine="task_board",
                    chat_mode="stock",
                    status="queued",
                    priority=200,
                    assignee_profile_id=self.agent_a.id,
                    request_json="{}",
                    board_visible=True,
                    queued_at=datetime.now(),
                ),
            ])
            db.commit()

        repo = self._make_repo()
        result = repo.next_queued_run()
        self.assertIsNone(result)

    def test_running_does_not_block_different_agent(self) -> None:
        """Agent A running 不阻塞 Agent B 的 queued 任务（不同 session）"""
        with self.SessionFactory() as db:
            db.add_all([
                AgentRun(
                    run_id="running-a",
                    session_id=self.session_a_id,
                    user_id=self.user_id,
                    engine="task_board",
                    chat_mode="stock",
                    status="running",
                    priority=100,
                    assignee_profile_id=self.agent_a.id,
                    request_json="{}",
                    board_visible=True,
                    started_at=datetime.now(),
                ),
                AgentRun(
                    run_id="queued-b",
                    session_id=self.session_b_id,
                    user_id=self.user_id,
                    engine="task_board",
                    chat_mode="stock",
                    status="queued",
                    priority=100,
                    assignee_profile_id=self.agent_b.id,
                    request_json="{}",
                    board_visible=True,
                    queued_at=datetime.now(),
                ),
            ])
            db.commit()

        repo = self._make_repo()
        result = repo.next_queued_run()
        self.assertEqual(result, "queued-b")

    def test_no_assignee_not_blocked(self) -> None:
        """没有 assignee_profile_id 的任务不受 agent 串行约束影响"""
        with self.SessionFactory() as db:
            db.add(AgentRun(
                run_id="no-assignee",
                session_id=self.session_a_id,
                user_id=self.user_id,
                engine="agentscope",
                chat_mode="research",
                status="queued",
                priority=100,
                assignee_profile_id=None,
                request_json="{}",
                board_visible=False,
                queued_at=datetime.now(),
            ))
            db.commit()

        repo = self._make_repo()
        result = repo.next_queued_run()
        self.assertEqual(result, "no-assignee")

    def test_waiting_children_also_blocks(self) -> None:
        """waiting_children 状态也应阻塞同一 Agent 的其他 queued 任务"""
        with self.SessionFactory() as db:
            db.add_all([
                AgentRun(
                    run_id="waiting-001",
                    session_id=self.session_a_id,
                    user_id=self.user_id,
                    engine="task_board",
                    chat_mode="stock",
                    status="waiting_children",
                    priority=100,
                    assignee_profile_id=self.agent_a.id,
                    request_json="{}",
                    board_visible=True,
                    started_at=datetime.now(),
                ),
                AgentRun(
                    run_id="queued-after-waiting",
                    session_id=self.session_b_id,
                    user_id=self.user_id,
                    engine="task_board",
                    chat_mode="stock",
                    status="queued",
                    priority=200,
                    assignee_profile_id=self.agent_a.id,
                    request_json="{}",
                    board_visible=True,
                    queued_at=datetime.now(),
                ),
            ])
            db.commit()

        repo = self._make_repo()
        result = repo.next_queued_run()
        self.assertIsNone(result)


class OrphanRecoveryTests(unittest.TestCase):
    """测试 Worker._recover_orphan_board_tasks 孤儿扫描"""

    def setUp(self) -> None:
        from backend.app.agent_runtime.repository import _utcnow
        self._utcnow = _utcnow

        self.engine = _make_engine()
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)

        with self.SessionFactory() as db:
            user = User(
                username="orphan-test",
                display_name="Tester",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            db.add(user)
            db.flush()
            self.user_id = user.id

            session = AiChatSession(
                user_id=self.user_id,
                digital_human_id=0,
                title="test session",
                chat_mode="research",
                source_type="web",
            )
            db.add(session)
            db.flush()
            self.session_id = session.id

    def tearDown(self) -> None:
        self.engine.dispose()

    def _make_worker(self):
        from backend.app.agent_runtime.repository import AgentRunRepository
        from backend.app.agent_runtime.redis_broker import RedisBroker
        from backend.app.agent_runtime.worker import AgentRuntimeWorker

        repo = AgentRunRepository(self.SessionFactory)
        broker = MagicMock(spec=RedisBroker)
        return AgentRuntimeWorker(repo, broker)

    def test_recovers_old_queued_board_tasks(self) -> None:
        """queued 超过 5 分钟的 board_visible 任务应被回收"""
        old_time = self._utcnow() - timedelta(minutes=10)
        with self.SessionFactory() as db:
            db.add(AgentRun(
                run_id="orphan-old",
                session_id=self.session_id,
                user_id=self.user_id,
                engine="task_board",
                chat_mode="stock",
                status="queued",
                priority=100,
                request_json="{}",
                board_visible=True,
                queued_at=old_time,
            ))
            db.commit()

        worker = self._make_worker()
        with patch("backend.app.core.database.SessionLocal", self.SessionFactory):
            orphans = worker._recover_orphan_board_tasks()

        self.assertIn("orphan-old", orphans)

    def test_skips_recent_queued_tasks(self) -> None:
        """刚入队不到 5 分钟的任务不应被回收"""
        recent_time = self._utcnow() - timedelta(minutes=2)
        with self.SessionFactory() as db:
            db.add(AgentRun(
                run_id="orphan-recent",
                session_id=self.session_id,
                user_id=self.user_id,
                engine="task_board",
                chat_mode="stock",
                status="queued",
                priority=100,
                request_json="{}",
                board_visible=True,
                queued_at=recent_time,
            ))
            db.commit()

        worker = self._make_worker()
        with patch("backend.app.core.database.SessionLocal", self.SessionFactory):
            orphans = worker._recover_orphan_board_tasks()

        self.assertNotIn("orphan-recent", orphans)

    def test_skips_non_board_tasks(self) -> None:
        """board_visible=False 的任务不应被回收"""
        old_time = self._utcnow() - timedelta(minutes=10)
        with self.SessionFactory() as db:
            db.add(AgentRun(
                run_id="non-board",
                session_id=self.session_id,
                user_id=self.user_id,
                engine="agentscope",
                chat_mode="research",
                status="queued",
                priority=100,
                request_json="{}",
                board_visible=False,
                queued_at=old_time,
            ))
            db.commit()

        worker = self._make_worker()
        with patch("backend.app.core.database.SessionLocal", self.SessionFactory):
            orphans = worker._recover_orphan_board_tasks()

        self.assertNotIn("non-board", orphans)

    def test_skips_cancel_requested_tasks(self) -> None:
        """已取消的任务不应被回收"""
        old_time = self._utcnow() - timedelta(minutes=10)
        with self.SessionFactory() as db:
            db.add(AgentRun(
                run_id="cancelled-orphan",
                session_id=self.session_id,
                user_id=self.user_id,
                engine="task_board",
                chat_mode="stock",
                status="queued",
                priority=100,
                request_json="{}",
                board_visible=True,
                queued_at=old_time,
                cancel_requested=True,
            ))
            db.commit()

        worker = self._make_worker()
        with patch("backend.app.core.database.SessionLocal", self.SessionFactory):
            orphans = worker._recover_orphan_board_tasks()

        self.assertNotIn("cancelled-orphan", orphans)

    def test_empty_when_no_orphans(self) -> None:
        """没有孤儿任务时返回空列表"""
        worker = self._make_worker()
        with patch("backend.app.core.database.SessionLocal", self.SessionFactory):
            orphans = worker._recover_orphan_board_tasks()

        self.assertEqual(orphans, [])


class WorkerResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_reconcile_failure_does_not_stop_queue_polling(self) -> None:
        from backend.app.agent_runtime.worker import AgentRuntimeWorker
        from backend.app.agent_runtime.redis_broker import RedisBroker

        repository = MagicMock()
        repository.session_factory = MagicMock()
        repository.recover_stale_runs.return_value = []
        repository.prune_terminal_events.return_value = 0
        broker = MagicMock(spec=RedisBroker)
        broker.dequeue = AsyncMock(return_value=None)
        broker.enqueue = AsyncMock(return_value=False)
        worker = AgentRuntimeWorker(repository, broker)
        worker._record_heartbeat = MagicMock()
        worker._recover_orphan_board_tasks = MagicMock(return_value=[])
        worker.team_service.reconcile = MagicMock(side_effect=RuntimeError("oversized workflow"))
        worker.workflow_service.reconcile = MagicMock(return_value=[])

        def stop_after_queue_poll():
            worker._stopping.set()
            return None

        repository.next_queued_run.side_effect = stop_after_queue_poll

        with patch(
            "backend.app.agent_runtime.worker.expire_pending_run_approvals",
            return_value=[],
        ):
            await worker._loop()

        repository.next_queued_run.assert_called_once_with()
        worker.workflow_service.reconcile.assert_called_once_with()


class OrchestrationToolSchemaTests(unittest.TestCase):
    """测试 orchestration_tools.py 中 create_agent_task_batch schema"""

    def test_schema_registered(self) -> None:
        from backend.app.agent_runtime.orchestration_tools import (
            ORCHESTRATION_TOOL_NAMES,
            TOOL_DISPLAY_NAMES,
        )
        self.assertIn("create_agent_task_batch", ORCHESTRATION_TOOL_NAMES)
        self.assertIn("create_agent_task_batch", TOOL_DISPLAY_NAMES)

    def test_schema_structure(self) -> None:
        from backend.app.agent_runtime.orchestration_tools import TEAM_AND_SCHEDULE_TOOL_SCHEMAS

        schema = next(
            s for s in TEAM_AND_SCHEDULE_TOOL_SCHEMAS
            if s["function"]["name"] == "create_agent_task_batch"
        )
        params = schema["function"]["parameters"]
        self.assertIn("objective", params["properties"])
        self.assertIn("tasks", params["properties"])
        self.assertIn("execution_mode", params["properties"])
        self.assertIn("completion_policy", params["properties"])
        self.assertIn("child_timeout_seconds", params["properties"])

        task_schema = params["properties"]["tasks"]["items"]
        self.assertIn("action_key", task_schema["properties"])
        self.assertIn("title", task_schema["properties"])
        self.assertIn("agent_code", task_schema["properties"])
        self.assertIn("instruction", task_schema["properties"])
        self.assertEqual(
            set(task_schema["required"]),
            {"action_key", "title", "agent_code", "instruction"},
        )

    def test_prompt_rule_mentions_tool(self) -> None:
        from backend.app.agent_runtime.orchestration_tools import ORCHESTRATION_PROMPT
        self.assertIn("create_agent_task_batch", ORCHESTRATION_PROMPT)
        self.assertIn("待完成", ORCHESTRATION_PROMPT)


class StructuredTaskHandoffDefinitionTests(unittest.TestCase):
    def _definition(self, *, mode: str = "execute_and_wait", extraction_mode: str = "structured_tool"):
        from backend.app.agent_runtime.agentscope_engine import _build_definition

        payload = {
            "chat_mode": "research",
            "digital_human_code": "intelligence-officer",
            "user_id": 1,
            "session_id": 1,
            "user_text": "生成盘前简报",
            "delegation_config": {
                "mode": mode,
                "extraction_mode": extraction_mode,
            },
        }
        with patch(
            "backend.app.agent_runtime.agentscope_engine.artifact_service.active_context",
            return_value="",
        ):
            return _build_definition(payload)

    def test_structured_handoff_is_available_to_non_coordinator_agent(self) -> None:
        system_prompt, tools, _ = self._definition()
        tool_names = {item["function"]["name"] for item in tools}

        self.assertIn("list_delegatable_agents", tool_names)
        self.assertIn("create_agent_task_batch", tool_names)
        self.assertIn("create_agent_task_batch", system_prompt)
        self.assertNotIn("create_agent_schedule", tool_names)

    def test_disabled_delegation_does_not_expose_handoff_tools(self) -> None:
        system_prompt, tools, _ = self._definition(mode="none")
        tool_names = {item["function"]["name"] for item in tools}

        self.assertNotIn("list_delegatable_agents", tool_names)
        self.assertNotIn("create_agent_task_batch", tool_names)
        self.assertNotIn("create_agent_task_batch", system_prompt)


class ScheduledTaskFallbackTests(unittest.TestCase):
    def test_structured_mode_extracts_explicit_pending_agent_tasks_when_batch_missing(self) -> None:
        from backend.app.scheduler.rhythm_scheduler import _should_extract_report_tasks

        report = """
## 核验动作清单（转交）
| 动作 | 负责 Agent | 状态 |
| 读公告全文 | 研究员 | ❌ 待完成 |
"""
        self.assertTrue(
            _should_extract_report_tasks(
                extraction_mode="structured_tool",
                output_content=report,
                has_task_batch=False,
            )
        )

    def test_structured_mode_does_not_duplicate_existing_task_batch(self) -> None:
        from backend.app.scheduler.rhythm_scheduler import _should_extract_report_tasks

        report = "| 动作 | 负责 Agent | 状态 |\n| 读公告 | 研究员 | 待完成 |"
        self.assertFalse(
            _should_extract_report_tasks(
                extraction_mode="structured_tool",
                output_content=report,
                has_task_batch=True,
            )
        )

    def test_structured_mode_ignores_plain_report_without_explicit_handoff(self) -> None:
        from backend.app.scheduler.rhythm_scheduler import _should_extract_report_tasks

        self.assertFalse(
            _should_extract_report_tasks(
                extraction_mode="structured_tool",
                output_content="今日仍有数据待完成采集，但无需委派 Agent。",
                has_task_batch=False,
            )
        )

    def test_legacy_mode_always_extracts_non_empty_report(self) -> None:
        from backend.app.scheduler.rhythm_scheduler import _should_extract_report_tasks

        self.assertTrue(
            _should_extract_report_tasks(
                extraction_mode="legacy_report",
                output_content="普通报告正文",
                has_task_batch=False,
            )
        )


class ChannelRunsDelegationConfigTests(unittest.TestCase):
    """测试 channel_runs.build_channel_request 传递 delegation_config"""

    def test_build_request_includes_delegation_config(self) -> None:
        """验证 delegation_config 被透传到 request_payload"""
        from backend.app.agent_runtime.channel_runs import build_channel_request

        engine = _make_engine()
        SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)

        with SessionFactory() as db:
            user = User(
                username="delegation-test",
                display_name="Tester",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            db.add(user)
            db.flush()
            user_id = user.id

            session = AiChatSession(
                user_id=user_id,
                digital_human_id=0,
                title="delegation test session",
                chat_mode="research",
                source_type="web",
            )
            db.add(session)
            db.commit()
            session_id = session.id

        delegation_config = {
            "mode": "execute_and_wait",
            "extraction_mode": "structured_tool",
            "completion_policy": "all_required",
            "child_timeout_seconds": 300,
            "max_delegated_tasks": 10,
        }

        with patch("backend.app.agent_runtime.channel_runs.SessionLocal", SessionFactory):
            payload = build_channel_request(
                session_id=session_id,
                question="测试",
                history=[],
                digital_human_code=None,
                source_type="scheduler",
                source_ref="test-job",
                user_id=user_id,
                delegation_config=delegation_config,
            )

        self.assertIn("delegation_config", payload)
        self.assertEqual(payload["delegation_config"]["mode"], "execute_and_wait")
        self.assertEqual(payload["delegation_config"]["extraction_mode"], "structured_tool")
        engine.dispose()

    def test_build_request_without_delegation_config(self) -> None:
        """验证 delegation_config 默认值为 None"""
        from backend.app.agent_runtime.channel_runs import build_channel_request

        engine = _make_engine()
        SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)

        with SessionFactory() as db:
            user = User(
                username="no-delegation-test",
                display_name="Tester",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            db.add(user)
            db.flush()
            user_id = user.id

            session = AiChatSession(
                user_id=user_id,
                digital_human_id=0,
                title="no delegation test session",
                chat_mode="research",
                source_type="web",
            )
            db.add(session)
            db.commit()
            session_id = session.id

        with patch("backend.app.agent_runtime.channel_runs.SessionLocal", SessionFactory):
            payload = build_channel_request(
                session_id=session_id,
                question="测试",
                history=[],
                digital_human_code=None,
                source_type="web",
                source_ref=None,
                user_id=user_id,
            )

        self.assertIn("delegation_config", payload)
        self.assertIsNone(payload["delegation_config"])
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
