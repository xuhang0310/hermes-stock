from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.agent_runtime.agentscope_engine import _resume_event
from backend.app.agent_runtime.repository import AgentRunRepository
from backend.app.agent_runtime.team_service import AgentTeamService
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
    User,
)
from backend.app.scheduler.rhythm_api import _run_output_content
from backend.app.scheduler.rhythm_scheduler import _finalize_run, _is_due, _recover_stale_runs
from backend.app.scheduler.schedule_service import AgentScheduleService


class AgentTeamRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
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
            ask_stock = DigitalHumanProfile(
                code="ask_stock",
                name="问股入口",
                avatar_tone="neutral",
                target_roles="*",
                description="虚拟入口档案",
                system_prompt="入口",
                quick_prompts_json="[]",
                status="enabled",
                sort_order=4,
                delegation_enabled=True,
            )
            db.add_all([user, pm, intelligence, risk, ask_stock])
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
            {"intelligence-officer", "risk-officer"},
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
            db.add_all([user, profile])
            db.commit()
            self.user_id = user.id
        self.service = AgentScheduleService(self.SessionFactory)

    def tearDown(self) -> None:
        self.engine.dispose()

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
            self.assertEqual(_recover_stale_runs(), 1)

        with self.SessionFactory() as db:
            job = db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == "once-crash-window"))
            run = db.scalar(select(JobRun).where(JobRun.run_id == "stale-once-run"))
            self.assertTrue(job.enabled)
            self.assertEqual(run.status, "failed")
            self.assertTrue(_is_due(job, datetime.now()))

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
