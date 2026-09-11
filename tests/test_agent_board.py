"""Agent 任务看板服务测试

测试 create_task_batch / retry / cancel / priority / queue 五个核心方法。
使用 SQLite 内存数据库，不需要真实 MySQL 连接。
"""

from __future__ import annotations

import unittest
import json
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.ai import ai_api
from backend.app.auth.auth import AuthContext
from backend.app.core.models import (
    AiChatSession,
    AgentDelegation,
    AgentRun,
    AgentTeamRun,
    DigitalHumanProfile,
    User,
)
from backend.app.agent_runtime.repository import AgentRunRepository
from backend.app.agent_runtime.team_service import AgentTeamService
from backend.app.quant import agent_board_service


def _make_session_scope(session_factory):
    """返回一个用指定 session_factory 的 session_scope 替代品"""
    @contextmanager
    def _scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    return _scope


class AgentBoardServiceTests(unittest.TestCase):
    """测试 agent_board_service 的原子操作"""

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)

        with self.SessionFactory() as db:
            user = User(
                username="board-test-user",
                display_name="Board Tester",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            db.add(user)
            db.flush()
            self.user_id = user.id

            self.pm = DigitalHumanProfile(
                code="stock-pm",
                name="总调度",
                avatar_tone="calm",
                target_roles="*",
                description="拆解和统稿",
                system_prompt="你是总调度",
                quick_prompts_json="[]",
                status="enabled",
                sort_order=1,
                delegation_enabled=True,
            )
            self.intelligence = DigitalHumanProfile(
                code="intelligence-officer",
                name="情报官",
                avatar_tone="evidence",
                target_roles="*",
                description="新闻检索",
                system_prompt="核验新闻来源",
                quick_prompts_json="[]",
                status="enabled",
                sort_order=2,
                delegation_enabled=True,
            )
            self.risk = DigitalHumanProfile(
                code="risk-officer",
                name="风控官",
                avatar_tone="strict",
                target_roles="*",
                description="风险复核",
                system_prompt="检查风险",
                quick_prompts_json="[]",
                status="enabled",
                sort_order=3,
                delegation_enabled=True,
            )
            db.add_all([self.pm, self.intelligence, self.risk])
            db.flush()

            session = AiChatSession(
                user_id=self.user_id,
                digital_human_id=self.pm.id,
                title="test session",
                chat_mode="research",
                source_type="web",
            )
            db.add(session)
            db.commit()
            db.refresh(session)
            self.session_id = session.id

    def tearDown(self) -> None:
        self.engine.dispose()

    def _make_leader_run(self) -> str:
        """创建一个 running 状态的父 AgentRun"""
        with self.SessionFactory() as db:
            leader = AgentRun(
                run_id="leader-run-001",
                session_id=self.session_id,
                user_id=self.user_id,
                engine="agentscope",
                chat_mode="research",
                status="running",
                request_json='{}',
                board_visible=False,
            )
            db.add(leader)
            db.commit()
            return leader.run_id

    def test_create_task_batch_creates_team_and_children(self) -> None:
        """测试 create_task_batch 创建 TeamRun + Delegations + 子 AgentRun"""
        leader_run_id = self._make_leader_run()

        batch = agent_board_service.TaskBatchSpec(
            objective="并行研究新闻与风险",
            execution_mode="serial_items",
            completion_policy="all_required",
            child_timeout_seconds=600,
            tasks=[
                agent_board_service.TaskSpec(
                    action_key="intel_news_check",
                    title="新闻检索",
                    agent_code="intelligence-officer",
                    instruction="搜索今日重要新闻",
                    priority=200,
                ),
                agent_board_service.TaskSpec(
                    action_key="risk_assessment",
                    title="风险评估",
                    agent_code="risk-officer",
                    instruction="评估持仓风险",
                    priority=150,
                ),
            ],
        )

        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            result = agent_board_service.create_task_batch(
                batch, leader_run_id=leader_run_id, user_id=self.user_id
            )

        self.assertIn("team_id", result)
        self.assertEqual(result["created_count"], 2)
        self.assertEqual(len(result["task_ids"]), 2)

        with self.SessionFactory() as db:
            team = db.get(AgentTeamRun, result["team_id"])
            self.assertIsNotNone(team)
            self.assertEqual(team.objective, "并行研究新闻与风险")
            self.assertEqual(team.execution_mode, "serial_items")

            children = db.scalars(
                select(AgentRun).where(AgentRun.team_id == result["team_id"])
            ).all()
            self.assertEqual(len(children), 2)

            for child in children:
                self.assertTrue(child.board_visible)
                self.assertEqual(child.status, "queued")
                self.assertEqual(child.engine, "agentscope")
                self.assertIsNotNone(child.assignee_profile_id)
                self.assertIsNotNone(child.task_title)
                self.assertIsNotNone(child.queued_at)
                request = json.loads(child.request_json)
                self.assertEqual(request["session_id"], child.session_id)
                self.assertEqual(request["user_id"], self.user_id)
                self.assertEqual(request["delegation_config"], {"mode": "none"})
                self.assertTrue(request["question"])

            delegations = db.scalars(
                select(AgentDelegation).where(AgentDelegation.team_id == result["team_id"])
            ).all()
            self.assertEqual(len(delegations), 2)
            action_keys = {d.action_key for d in delegations}
            self.assertEqual(action_keys, {"intel_news_check", "risk_assessment"})

            child_sessions = db.scalars(
                select(AiChatSession).where(AiChatSession.source_type == "task_batch")
            ).all()
            self.assertEqual(len(child_sessions), 2)
            owner = db.get(User, self.user_id)
            visible = db.scalars(
                select(AiChatSession).where(
                    ai_api._session_visibility(
                        AuthContext(user=owner, permissions={"agent:use"})
                    )
                )
            ).all()
            self.assertTrue(
                {row.id for row in child_sessions}.issubset({row.id for row in visible})
            )
            marker = ai_api._session_source_out(child_sessions[0])
            self.assertEqual(marker["source_type"], "task_batch")
            self.assertEqual(marker["source_label"], "Agent 任务看板")
            self.assertTrue(marker["read_only"])
            self.assertTrue(marker["is_child_task"])
            self.assertRegex(marker["source_identifier"], r"^TASK-[0-9A-F]{8}-0[01]$")

    def test_task_batch_keeps_tool_call_id_and_old_checkpoint_mapping_can_resume(self) -> None:
        leader_run_id = self._make_leader_run()
        batch = agent_board_service.TaskBatchSpec(
            objective="核验盘前证据",
            tasks=[
                agent_board_service.TaskSpec(
                    action_key="premarket_evidence_check",
                    title="盘前证据核验",
                    agent_code="risk-officer",
                    instruction="核验盘前风险",
                )
            ],
        )

        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            current = agent_board_service.create_task_batch(
                batch,
                leader_run_id=leader_run_id,
                user_id=self.user_id,
                tool_call_id="call-task-batch",
            )

        with self.SessionFactory() as db:
            team = db.get(AgentTeamRun, current["team_id"])
            self.assertEqual(team.tool_call_id, "call-task-batch")
            team.status = "completed"
            team.result_json = '{"members": [], "summary": "done"}'
            db.commit()

        repository = AgentRunRepository(self.SessionFactory)
        service = AgentTeamService(self.SessionFactory, repository)
        self.assertEqual(
            service.result_for_tool_call(leader_run_id, "call-task-batch")["summary"],
            "done",
        )

        # Older rows used batch_<team_id>.  Their checkpoint still records
        # the original tool call and remains safely recoverable.
        with self.SessionFactory() as db:
            team = db.get(AgentTeamRun, current["team_id"])
            team.tool_call_id = f'batch_{current["team_id"]}'
            db.commit()
        repository.save_checkpoint(
            leader_run_id,
            engine="agentscope",
            state_json="{}",
            waiting=[
                {
                    "type": "task_batch",
                    "team_id": current["team_id"],
                    "tool_call_id": "call-task-batch",
                }
            ],
        )
        self.assertEqual(
            service.result_for_tool_call(leader_run_id, "call-task-batch")["summary"],
            "done",
        )

    def test_create_task_batch_skips_disabled_agent(self) -> None:
        """测试 create_task_batch 跳过未启用的数字人"""
        with self.SessionFactory() as db:
            pm = db.get(DigitalHumanProfile, self.pm.id)
            pm.delegation_enabled = False
            db.commit()

        leader_run_id = self._make_leader_run()
        batch = agent_board_service.TaskBatchSpec(
            objective="测试跳过禁用",
            tasks=[
                agent_board_service.TaskSpec(
                    action_key="disabled_task",
                    title="禁用任务",
                    agent_code="stock-pm",
                    instruction="这个不应该被创建",
                ),
                agent_board_service.TaskSpec(
                    action_key="enabled_task",
                    title="正常任务",
                    agent_code="intelligence-officer",
                    instruction="这个应该被创建",
                ),
            ],
        )

        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            result = agent_board_service.create_task_batch(
                batch, leader_run_id=leader_run_id, user_id=self.user_id
            )

        self.assertEqual(result["created_count"], 1)

    def test_create_task_batch_rejects_duplicate_action_key(self) -> None:
        """测试 create_task_batch 拒绝重复的 action_key"""
        leader_run_id = self._make_leader_run()

        batch = agent_board_service.TaskBatchSpec(
            objective="重复 key 测试",
            tasks=[
                agent_board_service.TaskSpec(
                    action_key="duplicate_key",
                    title="任务 A",
                    agent_code="intelligence-officer",
                    instruction="指令 A",
                ),
                agent_board_service.TaskSpec(
                    action_key="duplicate_key",
                    title="任务 B",
                    agent_code="risk-officer",
                    instruction="指令 B",
                ),
            ],
        )

        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            with self.assertRaises(ValueError) as ctx:
                agent_board_service.create_task_batch(
                    batch, leader_run_id=leader_run_id, user_id=self.user_id
                )
        self.assertIn("duplicate_key", str(ctx.exception))

    def test_create_task_batch_rejects_empty_batch(self) -> None:
        """测试 create_task_batch 拒绝空批次"""
        batch = agent_board_service.TaskBatchSpec(objective="空批次", tasks=[])
        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            with self.assertRaises(ValueError) as ctx:
                agent_board_service.create_task_batch(batch)
        self.assertIn("不能为空", str(ctx.exception))

    def test_retry_failed_task_resets_status_to_queued(self) -> None:
        """测试 retry_failed_task 将失败任务重置为 queued"""
        with self.SessionFactory() as db:
            failed_run = AgentRun(
                run_id="failed-run-001",
                session_id=self.session_id,
                user_id=self.user_id,
                engine="task_board",
                chat_mode="stock",
                status="failed",
                attempts=1,
                max_attempts=3,
                error="测试失败原因",
                request_json='{}',
                board_visible=True,
            )
            db.add(failed_run)
            db.commit()

        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            result = agent_board_service.retry_failed_task("failed-run-001")

        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["attempts"], 2)

        with self.SessionFactory() as db:
            run = db.get(AgentRun, "failed-run-001")
            self.assertEqual(run.status, "queued")
            self.assertEqual(run.attempts, 2)
            self.assertIsNone(run.error)
            self.assertIsNotNone(run.queued_at)

    def test_retry_failed_task_rejects_non_failed_task(self) -> None:
        """测试 retry_failed_task 拒绝非失败状态的任务"""
        with self.SessionFactory() as db:
            running_run = AgentRun(
                run_id="running-run-001",
                session_id=self.session_id,
                user_id=self.user_id,
                engine="task_board",
                chat_mode="stock",
                status="running",
                request_json='{}',
                board_visible=True,
            )
            db.add(running_run)
            db.commit()

        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            with self.assertRaises(ValueError) as ctx:
                agent_board_service.retry_failed_task("running-run-001")
        self.assertIn("只有失败", str(ctx.exception))

    def test_retry_failed_task_rejects_max_attempts(self) -> None:
        """测试 retry_failed_task 拒绝已达最大重试次数的任务"""
        with self.SessionFactory() as db:
            exhausted_run = AgentRun(
                run_id="exhausted-run-001",
                session_id=self.session_id,
                user_id=self.user_id,
                engine="task_board",
                chat_mode="stock",
                status="failed",
                attempts=3,
                max_attempts=3,
                request_json='{}',
                board_visible=True,
            )
            db.add(exhausted_run)
            db.commit()

        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            with self.assertRaises(ValueError) as ctx:
                agent_board_service.retry_failed_task("exhausted-run-001", max_retries=3)
        self.assertIn("最大重试次数", str(ctx.exception))

    def test_cancel_task_sets_cancel_requested(self) -> None:
        """测试 cancel_task 设置 cancel_requested 并直接取消 queued 任务"""
        with self.SessionFactory() as db:
            queued_run = AgentRun(
                run_id="queued-run-001",
                session_id=self.session_id,
                user_id=self.user_id,
                engine="task_board",
                chat_mode="stock",
                status="queued",
                request_json='{}',
                board_visible=True,
            )
            db.add(queued_run)
            db.commit()

        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            result = agent_board_service.cancel_task("queued-run-001")

        self.assertEqual(result["status"], "cancelled")

        with self.SessionFactory() as db:
            run = db.get(AgentRun, "queued-run-001")
            self.assertEqual(run.status, "cancelled")
            self.assertTrue(run.cancel_requested)
            self.assertIsNotNone(run.finished_at)

    def test_cancel_task_running_only_requests_cancel(self) -> None:
        """测试 cancel_task 对 running 任务只设置 cancel_requested，不直接取消"""
        with self.SessionFactory() as db:
            running_run = AgentRun(
                run_id="running-cancel-001",
                session_id=self.session_id,
                user_id=self.user_id,
                engine="task_board",
                chat_mode="stock",
                status="running",
                request_json='{}',
                board_visible=True,
            )
            db.add(running_run)
            db.commit()

        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            result = agent_board_service.cancel_task("running-cancel-001")

        self.assertEqual(result["status"], "running")

        with self.SessionFactory() as db:
            run = db.get(AgentRun, "running-cancel-001")
            self.assertEqual(run.status, "running")
            self.assertTrue(run.cancel_requested)

    def test_update_task_priority(self) -> None:
        """测试 update_task_priority 修改优先级"""
        with self.SessionFactory() as db:
            run = AgentRun(
                run_id="priority-run-001",
                session_id=self.session_id,
                user_id=self.user_id,
                engine="task_board",
                chat_mode="stock",
                status="queued",
                priority=100,
                request_json='{}',
                board_visible=True,
            )
            db.add(run)
            db.commit()

        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            result = agent_board_service.update_task_priority("priority-run-001", 500)

        self.assertEqual(result["priority"], 500)
        self.assertEqual(result["old_priority"], 100)

        with self.SessionFactory() as db:
            run = db.get(AgentRun, "priority-run-001")
            self.assertEqual(run.priority, 500)

    def test_get_task_queue_for_agent_returns_queued_sorted_by_priority(self) -> None:
        """测试 get_task_queue_for_agent 按优先级降序返回 queued 任务"""
        with self.SessionFactory() as db:
            db.add_all([
                AgentRun(
                    run_id="queue-low",
                    session_id=self.session_id,
                    user_id=self.user_id,
                    engine="task_board",
                    chat_mode="stock",
                    status="queued",
                    priority=50,
                    assignee_profile_id=self.intelligence.id,
                    request_json='{}',
                    board_visible=True,
                    queued_at=datetime(2026, 8, 1, 10, 0),
                ),
                AgentRun(
                    run_id="queue-high",
                    session_id=self.session_id,
                    user_id=self.user_id,
                    engine="task_board",
                    chat_mode="stock",
                    status="queued",
                    priority=200,
                    assignee_profile_id=self.intelligence.id,
                    request_json='{}',
                    board_visible=True,
                    queued_at=datetime(2026, 8, 1, 10, 5),
                ),
                AgentRun(
                    run_id="queue-med",
                    session_id=self.session_id,
                    user_id=self.user_id,
                    engine="task_board",
                    chat_mode="stock",
                    status="queued",
                    priority=100,
                    assignee_profile_id=self.intelligence.id,
                    request_json='{}',
                    board_visible=True,
                    queued_at=datetime(2026, 8, 1, 10, 2),
                ),
            ])
            db.commit()

        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            queue = agent_board_service.get_task_queue_for_agent(self.intelligence.id)

        self.assertEqual(len(queue), 3)
        self.assertEqual(queue[0]["run_id"], "queue-high")
        self.assertEqual(queue[0]["priority"], 200)
        self.assertEqual(queue[1]["run_id"], "queue-med")
        self.assertEqual(queue[2]["run_id"], "queue-low")

    def test_get_task_queue_for_agent_returns_serial_when_running(self) -> None:
        """测试同数字人串行：有 running 任务时只返回它和后续 queued"""
        with self.SessionFactory() as db:
            db.add_all([
                AgentRun(
                    run_id="queue-running",
                    session_id=self.session_id,
                    user_id=self.user_id,
                    engine="task_board",
                    chat_mode="stock",
                    status="running",
                    priority=100,
                    assignee_profile_id=self.risk.id,
                    request_json='{}',
                    board_visible=True,
                    started_at=datetime(2026, 8, 1, 9, 0),
                ),
                AgentRun(
                    run_id="queue-waiting",
                    session_id=self.session_id,
                    user_id=self.user_id,
                    engine="task_board",
                    chat_mode="stock",
                    status="queued",
                    priority=100,
                    assignee_profile_id=self.risk.id,
                    request_json='{}',
                    board_visible=True,
                    queued_at=datetime(2026, 8, 1, 10, 0),
                ),
            ])
            db.commit()

        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            queue = agent_board_service.get_task_queue_for_agent(self.risk.id)

        self.assertEqual(len(queue), 2)
        self.assertEqual(queue[0]["run_id"], "queue-running")
        self.assertEqual(queue[0]["status"], "running")
        self.assertEqual(queue[1]["run_id"], "queue-waiting")

    def test_get_task_queue_for_agent_empty(self) -> None:
        """测试无任务时返回空列表"""
        with patch.object(agent_board_service, "session_scope", _make_session_scope(self.SessionFactory)):
            queue = agent_board_service.get_task_queue_for_agent(9999)
        self.assertEqual(queue, [])


if __name__ == "__main__":
    unittest.main()
