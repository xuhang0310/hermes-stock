from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.ai import ai_api
from backend.app.agent_runtime.agentscope_engine import _build_definition, _resume_event
from backend.app.agent_runtime.orchestration_tools import TEAM_AND_SCHEDULE_TOOL_SCHEMAS
from backend.app.agent_runtime.repository import AgentRunRepository
from backend.app.agent_runtime.team_service import AgentTeamService
from backend.app.auth.auth import AuthContext
from backend.app.core.database import Base
from backend.app.core.models import (
    AiChatMessage,
    AiChatSession,
    AgentDelegation,
    AgentReport,
    AgentRun,
    AgentTeamRun,
    DigitalHumanProfile,
    JobRun,
    ScheduledJob,
    SystemLog,
    User,
)
from backend.app.scheduler.rhythm_api import _run_output_content
from backend.app.scheduler.rhythm_scheduler import (
    _finalize_run,
    _is_due,
    _recover_stale_runs,
    _run_health_check,
)
from backend.app.scheduler.schedule_service import AgentScheduleService


class AgentTeamRuntimeTests(unittest.TestCase):
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
                username="team-user",
                display_name="Team User",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            pm = DigitalHumanProfile(
                code="stock-pm",
                name="总调度",
                avatar_tone="calm",
                target_roles="*",
                description="拆解和统稿",
                system_prompt="你是总调度",
                quick_prompts_json="[]",
                status="enabled",
                sort_order=1,
                delegation_enabled=False,
            )
            intelligence = DigitalHumanProfile(
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
            risk = DigitalHumanProfile(
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
            research = DigitalHumanProfile(
                code="research-analyst",
                name="个股研究员",
                avatar_tone="structured",
                target_roles="*",
                description="个股研究",
                system_prompt="研究个股",
                quick_prompts_json="[]",
                status="enabled",
                sort_order=4,
                delegation_enabled=True,
            )
            position_entry = DigitalHumanProfile(
                code="position-entry-agent",
                name="持仓录入 Agent",
                avatar_tone="careful",
                target_roles="*",
                description="录入持仓",
                system_prompt="受控录入持仓",
                quick_prompts_json="[]",
                status="enabled",
                sort_order=5,
                can_write_positions=True,
                delegation_enabled=True,
            )
            watchlist_entry = DigitalHumanProfile(
                code="watchlist-entry-agent",
                name="自选录入 Agent",
                avatar_tone="careful",
                target_roles="*",
                description="管理自选",
                system_prompt="受控管理自选",
                quick_prompts_json="[]",
                status="enabled",
                sort_order=6,
                delegation_enabled=True,
            )
            ask_stock = DigitalHumanProfile(
                code="ask_stock",
                name="问股入口",
                avatar_tone="neutral",
                target_roles="*",
                description="虚拟入口档案",
                system_prompt="入口",
                quick_prompts_json="[]",
                status="enabled",
                sort_order=7,
                delegation_enabled=True,
            )
            db.add_all(
                [
                    user,
                    pm,
                    intelligence,
                    risk,
                    research,
                    position_entry,
                    watchlist_entry,
                    ask_stock,
                ]
            )
            db.flush()
            session = AiChatSession(
                user_id=user.id,
                digital_human_id=pm.id,
                title="leader",
                chat_mode="research",
                source_type="web",
            )
            db.add(session)
            db.commit()
            self.user_id = user.id
            self.session_id = session.id

        self.repository = AgentRunRepository(self.SessionFactory)
        self.team_service = AgentTeamService(self.SessionFactory, self.repository)
        self.parent, _ = self.repository.create_run(
            session_id=self.session_id,
            user_id=self.user_id,
            engine="agentscope",
            chat_mode="research",
            request_payload={
                "session_id": self.session_id,
                "user_id": self.user_id,
                "user_label": "Team User",
                "question": "同时研究新闻和风险",
                "digital_human_code": "stock-pm",
                "chat_mode": "research",
            },
        )
        self.repository.claim_run(self.parent["run_id"], "leader-worker")

    def tearDown(self) -> None:
        self.engine.dispose()

    def _start_team(self) -> dict:
        return self.team_service.start_team(
            leader_run_id=self.parent["run_id"],
            tool_call_id="call-team-1",
            arguments={
                "objective": "形成新闻与风险联合结论",
                "tasks": [
                    {
                        "agent_code": "intelligence-officer",
                        "instruction": "核验今日重要新闻",
                        "expected_output": "来源、可信度、影响",
                    },
                    {
                        "agent_code": "risk-officer",
                        "instruction": "检查相关持仓风险",
                        "expected_output": "风险级别和反方论证",
                    },
                ],
            },
        )

    def _complete_child(self, child_run_id: str, answer: str, status: str = "completed") -> dict | None:
        message_id = None
        if status == "completed":
            with self.SessionFactory() as db:
                child = db.get(AgentRun, child_run_id)
                message = AiChatMessage(
                    session_id=child.session_id,
                    role="assistant",
                    content=answer,
                    event_count=0,
                )
                db.add(message)
                db.commit()
                message_id = message.id
        self.repository.mark_terminal(
            child_run_id,
            status,
            error=None if status == "completed" else answer,
            result_message_id=message_id,
        )
        return self.team_service.on_child_terminal(child_run_id)

    def test_team_fans_out_into_read_only_child_runs_and_is_idempotent(self) -> None:
        team = self._start_team()
        duplicate = self._start_team()

        self.assertTrue(team["created"])
        self.assertFalse(duplicate["created"])
        self.assertEqual(team["team_id"], duplicate["team_id"])
        self.assertEqual(len(team["members"]), 2)
        self.assertEqual(
            {item["code"] for item in self.team_service.list_delegatable_agents()},
            {
                "intelligence-officer",
                "risk-officer",
                "research-analyst",
                "position-entry-agent",
                "watchlist-entry-agent",
            },
        )
        with self.SessionFactory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(AgentTeamRun)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(AgentDelegation)), 2)
            children = db.scalars(
                select(AgentRun).where(
                    AgentRun.team_id == team["team_id"],
                    AgentRun.parent_run_id == self.parent["run_id"],
                )
            ).all()
            self.assertEqual(len(children), 2)
            for child in children:
                payload = __import__("json").loads(child.request_json)
                self.assertTrue(payload["delegation_read_only"])
                self.assertFalse(payload["allow_code_execution"])
                self.assertEqual(child.parent_run_id, self.parent["run_id"])
                self.assertEqual(child.orchestration_depth, 1)

            child_sessions = db.scalars(
                select(AiChatSession)
                .where(AiChatSession.source_type == "delegation")
                .order_by(AiChatSession.id)
            ).all()
            self.assertEqual(len(child_sessions), 2)
            first_prompt = db.scalar(
                select(AiChatMessage)
                .where(AiChatMessage.session_id == child_sessions[0].id)
                .order_by(AiChatMessage.id)
                .limit(1)
            )
            self.assertIn("# Agent Team 子任务 #1", first_prompt.content)
            self.assertIn("## 派发提示词\n核验今日重要新闻", first_prompt.content)
            self.assertIn("## 交付要求\n来源、可信度、影响", first_prompt.content)

            owner = db.get(User, self.user_id)
            visible = db.scalars(
                select(AiChatSession).where(
                    ai_api._session_visibility(
                        AuthContext(user=owner, permissions={"agent:use"})
                    )
                )
            ).all()
            self.assertTrue({row.id for row in child_sessions}.issubset({row.id for row in visible}))
            marker = ai_api._session_source_out(child_sessions[0])
            self.assertTrue(marker["is_child_task"])
            self.assertEqual(marker["child_task_index"], 1)
            self.assertRegex(marker["source_identifier"], r"^TEAM-[0-9A-F]{8}-01$")

    def test_alias_is_normalized_to_the_registered_agent_code(self) -> None:
        team = self.team_service.start_team(
            leader_run_id=self.parent["run_id"],
            tool_call_id="call-team-alias",
            arguments={
                "objective": "研究目标公司",
                "tasks": [
                    {
                        "agent_code": "stock_researcher",
                        "instruction": "输出目标公司的研究备忘录",
                    }
                ],
            },
        )

        self.assertEqual(team["members"][0]["agent_code"], "research-analyst")
        self.assertEqual(team["members"][0]["requested_agent_code"], "stock_researcher")
        self.assertTrue(team["members"][0]["agent_code_normalized"])
        with self.SessionFactory() as db:
            child = db.get(AgentRun, team["members"][0]["run_id"])
            payload = __import__("json").loads(child.request_json)
            self.assertEqual(payload["digital_human_code"], "research-analyst")
            self.assertTrue(payload["delegation_read_only"])

    def test_transaction_agents_are_delegatable_with_scoped_write_access(self) -> None:
        agents = {
            item["code"]: item for item in self.team_service.list_delegatable_agents(self.user_id)
        }
        self.assertFalse(agents["position-entry-agent"]["read_only"])
        self.assertEqual(agents["position-entry-agent"]["business_write_scope"], "positions")
        self.assertFalse(agents["watchlist-entry-agent"]["read_only"])
        self.assertEqual(agents["watchlist-entry-agent"]["business_write_scope"], "watchlist")

        with patch(
            "backend.app.agent_runtime.agentscope_engine.transaction_state_service.prompt_context",
            return_value="",
        ):
            _, position_tools, position_writes = _build_definition(
                {"digital_human_code": "position-entry-agent", "chat_mode": "research"}
            )
            _, watchlist_tools, watchlist_writes = _build_definition(
                {"digital_human_code": "watchlist-entry-agent", "chat_mode": "research"}
            )
        position_names = {item["function"]["name"] for item in position_tools}
        watchlist_names = {item["function"]["name"] for item in watchlist_tools}
        self.assertEqual(position_writes, {"save_position_holdings"})
        self.assertNotIn("ws_run_python", position_names)
        self.assertEqual(
            watchlist_writes,
            {"add_watchlist_items", "remove_watchlist_items"},
        )
        self.assertNotIn("ws_run_python", watchlist_names)

        team = self.team_service.start_team(
            leader_run_id=self.parent["run_id"],
            tool_call_id="call-team-transaction-agents",
            arguments={
                "objective": "同步持仓和自选",
                "tasks": [
                    {"agent_code": "持仓录入Agent", "instruction": "录入已确认的持仓"},
                    {"agent_code": "watchlist_agent", "instruction": "增加已确认的自选"},
                ],
            },
        )

        with self.SessionFactory() as db:
            payloads = {
                child.request_json: __import__("json").loads(child.request_json)
                for child in db.scalars(
                    select(AgentRun).where(AgentRun.team_id == team["team_id"])
                ).all()
            }
        by_code = {payload["digital_human_code"]: payload for payload in payloads.values()}
        self.assertFalse(by_code["position-entry-agent"]["delegation_read_only"])
        self.assertTrue(by_code["position-entry-agent"]["can_write_positions"])
        self.assertFalse(by_code["position-entry-agent"]["allow_code_execution"])
        self.assertFalse(by_code["watchlist-entry-agent"]["delegation_read_only"])
        self.assertFalse(by_code["watchlist-entry-agent"]["allow_code_execution"])

    def test_builtin_position_agent_keeps_write_access_without_forcing_default_account(self) -> None:
        profile = SimpleNamespace(
            code="position-entry-agent",
            can_write_positions=False,
        )
        self.assertTrue(
            ai_api._profile_can_write_positions(
                profile,
                chat_mode="research",
                permissions={"agent:use", "data:write"},
            )
        )
        self.assertFalse(ai_api._position_agent_uses_default_account(profile))
        self.assertFalse(
            ai_api._profile_can_write_positions(
                profile,
                chat_mode="research",
                permissions={"agent:use"},
            )
        )

    def test_unknown_agent_rejects_only_that_member(self) -> None:
        team = self.team_service.start_team(
            leader_run_id=self.parent["run_id"],
            tool_call_id="call-team-partial-preflight",
            arguments={
                "objective": "并行研究",
                "tasks": [
                    {"agent_code": "made_up_agent", "instruction": "无效任务"},
                    {"agent_code": "risk-officer", "instruction": "执行风险检查"},
                ],
            },
        )

        self.assertEqual(len(team["members"]), 1)
        self.assertEqual(team["members"][0]["agent_code"], "risk-officer")
        self.assertEqual(len(team["rejected_tasks"]), 1)
        self.assertEqual(team["rejected_tasks"][0]["member_index"], 1)
        self.assertIn("可用 agent_code", team["rejected_tasks"][0]["error"])

        self.assertFalse(self.repository.mark_waiting_children(self.parent["run_id"]))
        completed = self._complete_child(team["members"][0]["run_id"], "风险结论")
        self.assertTrue(completed["parent_requeued"])
        result = self.team_service.result_for_tool_call(
            self.parent["run_id"], "call-team-partial-preflight"
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["summary"], "1/2 个子任务完成")
        self.assertEqual(len(result["rejected_tasks"]), 1)

    def test_team_merges_repeated_agents_into_one_child_each(self) -> None:
        tasks = [
            {
                "agent_code": "intelligence-officer" if index % 2 else "risk-officer",
                "instruction": f"并行处理第 {index} 个高优任务",
                "expected_output": f"第 {index} 个任务结论",
            }
            for index in range(1, 6)
        ]
        team = self.team_service.start_team(
            leader_run_id=self.parent["run_id"],
            tool_call_id="call-team-five-members",
            arguments={"objective": "并行完成五项研究", "tasks": tasks, "timeout_seconds": 600},
        )

        self.assertEqual(len(team["members"]), 2)
        self.assertEqual(team["dispatch_stats"]["requested_task_count"], 5)
        self.assertEqual(team["dispatch_stats"]["agent_count"], 2)
        self.assertEqual(team["dispatch_stats"]["merged_task_count"], 3)
        members = {item["agent_code"]: item for item in team["members"]}
        self.assertEqual(members["intelligence-officer"]["work_item_count"], 3)
        self.assertEqual(members["intelligence-officer"]["source_task_indexes"], [1, 3, 5])
        self.assertEqual(members["risk-officer"]["work_item_count"], 2)
        with self.SessionFactory() as db:
            child_ids = set(
                db.scalars(
                    select(AgentRun.run_id).where(
                        AgentRun.team_id == team["team_id"],
                        AgentRun.parent_run_id == self.parent["run_id"],
                    )
                ).all()
            )
            delegated_child_ids = set(
                db.scalars(
                    select(AgentDelegation.child_run_id).where(AgentDelegation.team_id == team["team_id"])
                ).all()
            )
            self.assertEqual(len(child_ids), 2)
            self.assertEqual(delegated_child_ids, child_ids)
            delegations = db.scalars(
                select(AgentDelegation)
                .where(AgentDelegation.team_id == team["team_id"])
                .order_by(AgentDelegation.member_index)
            ).all()
            self.assertEqual(
                {row.target_profile_code for row in delegations},
                {"intelligence-officer", "risk-officer"},
            )
            self.assertIn("1. 并行处理第 1 个高优任务", delegations[0].instruction)
            self.assertIn("3. 并行处理第 5 个高优任务", delegations[0].instruction)

    def test_team_collapses_twenty_directives_and_schema_caps_model_output(self) -> None:
        tasks = [
            {
                "agent_code": "intelligence-officer" if index % 2 else "risk-officer",
                "instruction": f"并行处理第 {index} 个子任务",
            }
            for index in range(1, 21)
        ]
        team = self.team_service.start_team(
            leader_run_id=self.parent["run_id"],
            tool_call_id="call-team-twenty-members",
            arguments={"objective": "并行完成二十项研究", "tasks": tasks},
        )
        self.assertEqual(len(team["members"]), 2)
        self.assertEqual(team["dispatch_stats"]["merged_task_count"], 18)
        extra = self.team_service.start_team(
            leader_run_id=self.parent["run_id"],
            tool_call_id="call-team-extra-duplicate",
            arguments={"objective": "任务很多但角色重复", "tasks": tasks + [tasks[0]]},
        )
        self.assertEqual(len(extra["members"]), 2)
        self.assertEqual(extra["dispatch_stats"]["requested_task_count"], 21)

        dispatch_schema = next(
            item
            for item in TEAM_AND_SCHEDULE_TOOL_SCHEMAS
            if item["function"]["name"] == "dispatch_agent_tasks"
        )
        self.assertEqual(
            dispatch_schema["function"]["parameters"]["properties"]["tasks"]["maxItems"],
            20,
        )

    def test_alias_and_canonical_code_share_one_child_run(self) -> None:
        team = self.team_service.start_team(
            leader_run_id=self.parent["run_id"],
            tool_call_id="call-team-alias-merge",
            arguments={
                "objective": "研究两家公司",
                "tasks": [
                    {"agent_code": "stock_researcher", "instruction": "研究公司 A"},
                    {"agent_code": "research-analyst", "instruction": "研究公司 B"},
                ],
            },
        )

        self.assertEqual(len(team["members"]), 1)
        member = team["members"][0]
        self.assertEqual(member["agent_code"], "research-analyst")
        self.assertEqual(member["work_item_count"], 2)
        self.assertEqual(member["requested_agent_codes"], ["stock_researcher", "research-analyst"])
        self.assertTrue(member["agent_code_normalized"])
        with self.SessionFactory() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(AgentRun)
                    .where(
                        AgentRun.team_id == team["team_id"],
                        AgentRun.parent_run_id == self.parent["run_id"],
                    )
                ),
                1,
            )
            child = db.scalar(
                select(AgentRun).where(
                    AgentRun.team_id == team["team_id"],
                    AgentRun.parent_run_id == self.parent["run_id"],
                )
            )
            payload = __import__("json").loads(child.request_json)
            self.assertEqual(len(payload["delegated_work_items"]), 2)

    def test_parent_parks_and_resumes_after_all_children_with_partial_result(self) -> None:
        team = self._start_team()
        self.assertFalse(self.repository.mark_waiting_children(self.parent["run_id"]))
        self.assertEqual(self.repository.get_run(self.parent["run_id"])["status"], "waiting_children")

        first = self._complete_child(team["members"][0]["run_id"], "新闻结论")
        self.assertFalse(first["parent_requeued"])
        second = self._complete_child(team["members"][1]["run_id"], "上游超时", status="failed")
        self.assertTrue(second["parent_requeued"])
        self.assertEqual(self.repository.get_run(self.parent["run_id"])["status"], "queued")

        aggregate = self.team_service.result_for_tool_call(self.parent["run_id"], "call-team-1")
        self.assertEqual(aggregate["status"], "partial")
        self.assertEqual(len(aggregate["members"]), 2)
        self.assertEqual(aggregate["members"][0]["answer"], "新闻结论")

    def test_children_finishing_before_parent_parks_requeues_without_deadlock(self) -> None:
        team = self._start_team()
        for member in team["members"]:
            result = self._complete_child(member["run_id"], f"{member['agent_name']} result")
        self.assertFalse(result["parent_requeued"])
        self.assertTrue(self.repository.mark_waiting_children(self.parent["run_id"]))
        self.assertEqual(self.repository.get_run(self.parent["run_id"])["status"], "queued")

    def test_multiple_external_team_calls_wait_for_every_team(self) -> None:
        first = self.team_service.start_team(
            leader_run_id=self.parent["run_id"],
            tool_call_id="call-team-a",
            arguments={
                "objective": "并行研究",
                "tasks": [{"agent_code": "intelligence-officer", "instruction": "检查新闻"}],
            },
        )
        second = self.team_service.start_team(
            leader_run_id=self.parent["run_id"],
            tool_call_id="call-team-b",
            arguments={
                "objective": "并行研究",
                "tasks": [{"agent_code": "risk-officer", "instruction": "检查风险"}],
            },
        )
        self.assertFalse(self.repository.mark_waiting_children(self.parent["run_id"]))

        first_result = self._complete_child(first["members"][0]["run_id"], "新闻结论")
        self.assertFalse(first_result["parent_requeued"])
        self.assertEqual(self.repository.get_run(self.parent["run_id"])["status"], "waiting_children")

        second_result = self._complete_child(second["members"][0]["run_id"], "风险结论")
        self.assertTrue(second_result["parent_requeued"])
        self.assertEqual(self.repository.get_run(self.parent["run_id"])["status"], "queued")

    def test_agentscope_external_result_resumes_the_submitted_tool_call(self) -> None:
        from agentscope.message import AssistantMsg, ToolCallBlock, ToolCallState
        from agentscope.state import AgentState

        team = self._start_team()
        self.repository.mark_waiting_children(self.parent["run_id"])
        for member in team["members"]:
            self._complete_child(member["run_id"], f"{member['agent_name']} result")
        state = AgentState(
            session_id=str(self.session_id),
            reply_id="reply-team",
            context=[
                AssistantMsg(
                    "assistant",
                    [
                        ToolCallBlock(
                            id="call-team-1",
                            name="dispatch_agent_tasks",
                            input="{}",
                            state=ToolCallState.SUBMITTED,
                        )
                    ],
                )
            ],
        )
        with (
            patch("backend.app.agent_runtime.agentscope_engine.SessionLocal", self.SessionFactory),
            patch("backend.app.agent_runtime.team_service.agent_team_service", self.team_service),
        ):
            event, emitted = _resume_event(self.parent["run_id"], state)

        self.assertEqual(event.execution_results[0].id, "call-team-1")
        self.assertEqual(str(event.execution_results[0].state), "success")
        self.assertEqual(emitted[0]["type"], "team_resumed")


class AgentScheduleServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.SessionFactory() as db:
            user = User(
                username="schedule-owner",
                display_name="Schedule Owner",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            profile = DigitalHumanProfile(
                code="intelligence-officer",
                name="情报官",
                avatar_tone="evidence",
                target_roles="*",
                description="新闻检索",
                system_prompt="核验新闻来源",
                quick_prompts_json="[]",
                status="enabled",
            )
            pm = DigitalHumanProfile(
                code="stock-pm",
                name="总调度",
                avatar_tone="calm",
                target_roles="*",
                description="拆解和统稿",
                system_prompt="你是总调度",
                quick_prompts_json="[]",
                status="enabled",
            )
            db.add_all([user, profile, pm])
            db.commit()
            self.user_id = user.id
        self.service = AgentScheduleService(self.SessionFactory)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_schedule_tools_only_expose_the_current_users_tasks(self) -> None:
        with self.SessionFactory() as db:
            viewer = User(
                username="schedule-viewer",
                display_name="Schedule Viewer",
                role="viewer",
                permission_scope="agent:use",
                password_hash="test",
            )
            db.add(viewer)
            db.flush()
            db.add_all(
                [
                    ScheduledJob(
                        job_key="system-health",
                        name="系统健康检查",
                        job_kind="health_check",
                        schedule_type="daily",
                        run_time="08:15",
                        owner_user_id=None,
                        created_via="admin",
                        enabled=True,
                    ),
                    ScheduledJob(
                        job_key="viewer-agent-job",
                        name="用户日报",
                        job_kind="agent_task",
                        schedule_type="daily",
                        run_time="09:00",
                        owner_user_id=viewer.id,
                        created_via="agent",
                        enabled=True,
                    ),
                ]
            )
            db.commit()
            viewer_id = viewer.id

        admin_result = self.service.execute(
            "list_agent_schedules",
            {"include_disabled": True},
            {"user_id": self.user_id},
        )
        self.assertEqual(admin_result["total"], 0)
        self.assertEqual(admin_result["enabled_total"], 0)
        with self.assertRaises(ValueError):
            self.service.update_job(
                user_id=self.user_id,
                run_id=None,
                args={"job_key": "system-health", "schedule": {"type": "daily", "run_time": "08:30"}},
            )

        viewer_result = self.service.execute(
            "list_agent_schedules",
            {"include_disabled": True},
            {"user_id": viewer_id},
        )
        self.assertEqual(viewer_result["total"], 1)
        jobs = {job["job_key"]: job for job in viewer_result["jobs"]}
        self.assertTrue(jobs["viewer-agent-job"]["editable"])
        with self.assertRaises(ValueError):
            self.service.update_job(
                user_id=viewer_id,
                run_id=None,
                args={"job_key": "system-health", "enabled": False},
            )

    def test_create_pause_update_and_delete_sandbox_schedule(self) -> None:
        run_at = (datetime.now() + timedelta(minutes=10)).replace(microsecond=0)
        created = self.service.create_job(
            user_id=self.user_id,
            run_id="leader-run",
            args={
                "name": "定时新闻整理",
                "target_agent_code": "intelligence-officer",
                "instruction": "搜索新闻并用 Python 生成 CSV",
                "schedule": {"type": "once", "run_at": run_at.isoformat()},
                "allow_code_execution": True,
                "push_to_feishu": True,
            },
        )
        job_key = created["job"]["job_key"]
        self.assertTrue(created["job"]["allow_code_execution"])
        self.assertTrue(created["job"]["push_to_feishu"])
        with self.SessionFactory() as db:
            job = db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == job_key))
            self.assertEqual(job.owner_user_id, self.user_id)
            self.assertEqual(job.created_by_run_id, "leader-run")
            self.assertTrue(_is_due(job, run_at + timedelta(minutes=1)))

        paused = self.service.set_enabled(user_id=self.user_id, job_key=job_key, enabled=False)
        self.assertFalse(paused["job"]["enabled"])
        updated = self.service.update_job(
            user_id=self.user_id,
            run_id="leader-run-2",
            args={
                "job_key": job_key,
                "schedule": {"type": "weekly", "run_time": "08:30", "weekdays": [1, 3, 5]},
                "enabled": True,
            },
        )
        self.assertEqual(updated["job"]["schedule_type"], "weekly")
        self.assertEqual(updated["job"]["weekday_mask"], "1,3,5")
        self.assertTrue(self.service.delete_job(user_id=self.user_id, job_key=job_key)["deleted"])
        self.assertEqual(self.service.list_jobs(self.user_id), [])

    def test_create_alias_is_verified_and_idempotent(self) -> None:
        args = {
            "name": "午间持仓全员分析",
            "target_agent_code": "orchestrator",
            "instruction": "协调 7 个数字人分析持仓并统稿。",
            "schedule": {"type": "daily", "run_time": "12:00"},
            "trading_day_only": True,
            "push_to_feishu": True,
        }
        created = self.service.create_job(
            user_id=self.user_id,
            run_id="leader-create",
            args=args,
        )
        repeated = self.service.create_job(
            user_id=self.user_id,
            run_id="leader-retry",
            args=args,
        )

        self.assertTrue(created["created"])
        self.assertTrue(created["verified"])
        self.assertEqual(created["job"]["target_agent_code"], "stock-pm")
        self.assertFalse(repeated["created"])
        self.assertTrue(repeated["already_exists"])
        self.assertTrue(repeated["verified"])
        self.assertEqual(repeated["job"]["job_key"], created["job"]["job_key"])
        with self.SessionFactory() as db:
            count = db.scalar(
                select(func.count()).select_from(ScheduledJob).where(
                    ScheduledJob.owner_user_id == self.user_id,
                    ScheduledJob.name == args["name"],
                )
            )
        self.assertEqual(count, 1)

    def test_same_name_with_different_configuration_is_rejected(self) -> None:
        args = {
            "name": "午间持仓全员分析",
            "target_agent_code": "总调度",
            "instruction": "分析持仓。",
            "schedule": {"type": "daily", "run_time": "12:00"},
        }
        created = self.service.create_job(user_id=self.user_id, run_id="first", args=args)
        with self.assertRaisesRegex(ValueError, created["job"]["job_key"]):
            self.service.create_job(
                user_id=self.user_id,
                run_id="second",
                args={**args, "schedule": {"type": "daily", "run_time": "13:00"}},
            )

    def test_code_schedule_requires_explicit_session_code_permission(self) -> None:
        run_at = (datetime.now() + timedelta(minutes=10)).replace(microsecond=0)
        args = {
            "name": "定时代码",
            "target_agent_code": "intelligence-officer",
            "instruction": "运行 Python 整理新闻",
            "schedule": {"type": "once", "run_at": run_at.isoformat()},
            "allow_code_execution": True,
        }
        with self.assertRaises(PermissionError):
            self.service.execute(
                "create_agent_schedule",
                args,
                {"user_id": self.user_id, "run_id": "leader", "allow_code_execution": False},
            )
        created = self.service.execute(
            "create_agent_schedule",
            args,
            {"user_id": self.user_id, "run_id": "leader", "allow_code_execution": True},
        )
        self.assertTrue(created["job"]["allow_code_execution"])

    def test_restart_reenables_claimed_one_shot_job(self) -> None:
        run_at = datetime.now() - timedelta(minutes=1)
        with self.SessionFactory() as db:
            db.add(
                ScheduledJob(
                    job_key="once-crash-window",
                    name="一次性任务",
                    job_kind="agent_task",
                    schedule_type="once",
                    run_once_at=run_at,
                    trading_day_only=False,
                    enabled=False,
                )
            )
            db.add(
                JobRun(
                    run_id="stale-once-run",
                    job_key="once-crash-window",
                    fire_key="once-crash-window@stale",
                    trigger_type="schedule",
                    status="running",
                    started_at=run_at,
                )
            )
            db.commit()

        with patch("backend.app.scheduler.rhythm_scheduler.SessionLocal", self.SessionFactory):
            self.assertEqual(
                _recover_stale_runs(),
                [("once-crash-window", "stale-once-run")],
            )

        with self.SessionFactory() as db:
            job = db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == "once-crash-window"))
            run = db.scalar(select(JobRun).where(JobRun.run_id == "stale-once-run"))
            self.assertFalse(job.enabled)
            self.assertEqual(run.status, "running")
            self.assertIsNone(run.finished_at)

    def test_health_check_notification_is_idempotent_per_job_run(self) -> None:
        with self.SessionFactory() as db:
            db.add(
                SystemLog(
                    level="ERROR",
                    module="test",
                    message="force a health-check alert",
                    created_at=datetime.now(),
                )
            )
            db.commit()

        notification = SimpleNamespace(id=42, status="sent", error=None)
        with (
            patch("backend.app.scheduler.rhythm_scheduler.SessionLocal", self.SessionFactory),
            patch("backend.app.scheduler.rhythm_scheduler.send_feishu", return_value=notification) as send_mock,
            patch("backend.app.core.applog.prune_system_logs"),
        ):
            message, notification_id, error = _run_health_check(
                datetime.now(),
                "feishu-channel",
                "health-run-1",
            )

        self.assertIn("发现 1 项异常", message)
        self.assertEqual(notification_id, 42)
        self.assertIsNone(error)
        self.assertEqual(send_mock.call_args.kwargs["event_id"], "rhythm-health-health-run-1")

    def test_job_run_preserves_full_output_and_derives_preview(self) -> None:
        full_output = "完整结果" * 1200
        with self.SessionFactory() as db:
            db.add(
                JobRun(
                    run_id="full-output-run",
                    job_key="full-output-job",
                    fire_key="full-output-job@manual",
                    trigger_type="manual",
                    status="running",
                    started_at=datetime.now(),
                )
            )
            db.commit()

        with patch("backend.app.scheduler.rhythm_scheduler.SessionLocal", self.SessionFactory):
            _finalize_run(
                "full-output-run",
                "success",
                output_content=full_output,
                duration_ms=1234,
            )

        with self.SessionFactory() as db:
            run = db.scalar(select(JobRun).where(JobRun.run_id == "full-output-run"))
            self.assertEqual(run.output_content, full_output)
            self.assertEqual(run.output_preview, full_output[:1000])

    def test_historical_job_run_recovers_full_report_content(self) -> None:
        full_output = "历史完整结果" * 800
        with self.SessionFactory() as db:
            db.add(
                AgentReport(
                    report_id="historical-report",
                    task_id="historical-job",
                    agent_name="pm",
                    title="历史报告",
                    action_suggestion="observe",
                    content=full_output,
                )
            )
            run = JobRun(
                run_id="historical-run",
                job_key="historical-job",
                fire_key="historical-job@schedule",
                trigger_type="schedule",
                status="success",
                started_at=datetime.now(),
                report_id="historical-report",
                output_preview=full_output[:500],
            )
            db.add(run)
            db.commit()
            db.refresh(run)

            self.assertEqual(_run_output_content(db, run), full_output)


if __name__ == "__main__":
    unittest.main()
