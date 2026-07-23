from __future__ import annotations

import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.agent_runtime.repository import AgentRunRepository
from backend.app.agent_runtime.workflow_service import AgentWorkflowService
from backend.app.agent_runtime.workflow_templates import BUILTIN_WORKFLOW_TEMPLATES
from backend.app.core.database import Base
from backend.app.core.models import (
    AiChatMessage,
    AiChatSession,
    AgentRun,
    DigitalHumanProfile,
    User,
)


class AgentWorkflowRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.SessionFactory() as db:
            user = User(
                username="workflow-user",
                display_name="Workflow User",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            profiles = [
                DigitalHumanProfile(
                    code=code,
                    name=name,
                    avatar_tone="professional",
                    target_roles="*",
                    description=name,
                    system_prompt=f"你是{name}",
                    quick_prompts_json="[]",
                    status="enabled",
                    sort_order=index,
                    delegation_enabled=code != "stock-pm",
                )
                for index, (code, name) in enumerate(
                    [
                        ("stock-pm", "总调度"),
                        ("research-analyst", "研究员"),
                        ("quant-analyst", "量化分析师"),
                        ("risk-officer", "风控官"),
                        ("intelligence-officer", "情报官"),
                        ("macro-strategist", "宏观策略师"),
                    ],
                    start=1,
                )
            ]
            db.add_all([user, *profiles])
            db.flush()
            session = AiChatSession(
                user_id=user.id,
                digital_human_id=profiles[0].id,
                title="leader",
                chat_mode="research",
                source_type="web",
            )
            db.add(session)
            db.commit()
            self.user_id = user.id
            self.session_id = session.id

        self.repository = AgentRunRepository(self.SessionFactory)
        self.service = AgentWorkflowService(self.SessionFactory, self.repository)
        self.parent, _ = self.repository.create_run(
            session_id=self.session_id,
            user_id=self.user_id,
            engine="agentscope",
            chat_mode="research",
            request_payload={
                "session_id": self.session_id,
                "user_id": self.user_id,
                "user_label": "Workflow User",
                "question": "研究平安银行量化策略",
                "digital_human_code": "stock-pm",
                "chat_mode": "research",
            },
        )
        self.repository.claim_run(self.parent["run_id"], "leader-worker")

    def tearDown(self) -> None:
        self.engine.dispose()

    def _complete(self, run_id: str, answer: str, status: str = "completed") -> dict:
        message_id = None
        with self.SessionFactory() as db:
            run = db.get(AgentRun, run_id)
            if answer:
                message = AiChatMessage(
                    session_id=run.session_id,
                    role="assistant",
                    content=answer,
                    event_count=0,
                )
                db.add(message)
                db.commit()
                message_id = message.id
        self.repository.mark_terminal(
            run_id,
            status,
            error=None if status == "completed" else answer,
            result_message_id=message_id,
        )
        return self.service.on_child_terminal(run_id)

    def test_five_builtin_templates_are_versioned_and_valid(self) -> None:
        templates = self.service.list_templates()
        self.assertEqual({item["code"] for item in templates}, set(BUILTIN_WORKFLOW_TEMPLATES))
        self.assertEqual(len(templates), 5)
        self.assertTrue(all(item["version"] >= 1 for item in templates))
        self.assertTrue(all(item["nodes"] for item in templates))

    def test_quant_workflow_unlocks_dependencies_and_resumes_parent(self) -> None:
        workflow = self.service.start_workflow(
            leader_run_id=self.parent["run_id"],
            tool_call_id="call-workflow-1",
            arguments={
                "template_code": "quant_strategy_desk",
                "objective": "验证平安银行 MACD 与因子组合",
                "variables": {
                    "target": "000001.SZ 平安银行",
                    "market": "A股",
                    "period": "近3年",
                },
            },
        )
        self.assertTrue(workflow["created"])
        roots = [node for node in workflow["nodes"] if not node["depends_on"]]
        self.assertEqual({node["node_key"] for node in roots}, {"screen", "factor_research"})
        self.assertTrue(all(node["status"] == "queued" for node in roots))
        self.assertEqual(
            next(node for node in workflow["nodes"] if node["node_key"] == "backtest")["status"],
            "waiting_dependency",
        )
        self.assertFalse(self.repository.mark_waiting_children(self.parent["run_id"]))

        root_ids = {node["node_key"]: node["child_run_id"] for node in roots}
        first = self._complete(root_ids["screen"], "候选池结果")
        self.assertEqual(first["queued_run_ids"], [])
        second = self._complete(root_ids["factor_research"], "因子研究结果")
        self.assertEqual(len(second["queued_run_ids"]), 1)

        state = self.service.workflow_for_run(workflow["workflow_run_id"], user_id=self.user_id)
        backtest = next(node for node in state["nodes"] if node["node_key"] == "backtest")
        self.assertEqual(backtest["status"], "queued")
        self.assertIsNotNone(backtest["child_run_id"])
        with self.SessionFactory() as db:
            child = db.get(AgentRun, backtest["child_run_id"])
            prompt = db.scalar(
                select(AiChatMessage)
                .where(AiChatMessage.session_id == child.session_id)
                .order_by(AiChatMessage.id)
                .limit(1)
            )
            self.assertIn("候选池结果", prompt.content)
            self.assertIn("因子研究结果", prompt.content)
            session = db.get(AiChatSession, child.session_id)
            self.assertEqual(session.source_type, "workflow")

        third = self._complete(backtest["child_run_id"], "回测报告")
        self.assertEqual(len(third["queued_run_ids"]), 1)
        state = self.service.workflow_for_run(workflow["workflow_run_id"], user_id=self.user_id)
        audit = next(node for node in state["nodes"] if node["node_key"] == "risk_audit")
        final = self._complete(audit["child_run_id"], "风险复核通过")
        self.assertTrue(final["parent_requeued"])
        self.assertEqual(self.repository.get_run(self.parent["run_id"])["status"], "queued")
        result = self.service.result_for_tool_call(self.parent["run_id"], "call-workflow-1")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"], "4/4 个工作流节点完成")
        self.assertEqual(result["final_outputs"][0]["node_key"], "risk_audit")

    def test_failed_node_blocks_descendants(self) -> None:
        workflow = self.service.start_workflow(
            leader_run_id=self.parent["run_id"],
            tool_call_id="call-workflow-failure",
            arguments={
                "template_code": "investment_committee",
                "objective": "审议测试标的",
                "variables": {"target": "测试标的"},
            },
        )
        self.repository.mark_waiting_children(self.parent["run_id"])
        roots = [node for node in workflow["nodes"] if not node["depends_on"]]
        self._complete(roots[0]["child_run_id"], "多头材料")
        result = self._complete(roots[1]["child_run_id"], "空头节点失败", status="failed")
        self.assertTrue(result["parent_requeued"])
        state = self.service.workflow_for_run(workflow["workflow_run_id"], user_id=self.user_id)
        statuses = {node["node_key"]: node["status"] for node in state["nodes"]}
        self.assertEqual(statuses["cross_exam"], "blocked")
        self.assertEqual(statuses["decision"], "blocked")
        self.assertEqual(state["status"], "partial")

        retried = self.service.retry_node(
            workflow["workflow_run_id"],
            "bear_case",
            user_id=self.user_id,
        )
        retried_statuses = {node["node_key"]: node["status"] for node in retried["nodes"]}
        self.assertEqual(retried_statuses["bear_case"], "queued")
        self.assertEqual(retried_statuses["cross_exam"], "waiting_dependency")
        self.assertEqual(retried_statuses["decision"], "waiting_dependency")
        self.assertEqual(self.repository.get_run(self.parent["run_id"])["status"], "waiting_children")


if __name__ == "__main__":
    unittest.main()
