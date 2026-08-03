from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.agent_runtime.agentscope_engine import (
    _build_tools,
    _resume_event,
    agentscope_available,
    run_agentscope,
    select_engine,
)
from backend.app.agent_runtime.event_sink import DurableEventSink
from backend.app.agent_runtime.executor import AgentRunExecutor
from backend.app.agent_runtime.redis_broker import RedisBroker
from backend.app.agent_runtime.repository import AgentRunRepository
from backend.app.agent_runtime.service import format_sse
from backend.app.ai.ai_api import _discard_duplicate_preparation
from backend.app.core.database import Base
from backend.app.core.models import (
    AiChatMessage,
    AiChatSession,
    AgentRun,
    AgentRunEvent,
    AgentRunStep,
    AgentToolApproval,
    AgentToolExecution,
    User,
)


class RecordingBroker:
    def __init__(self) -> None:
        self.published: list[tuple[str, int]] = []

    async def publish_event(self, run_id: str, seq: int) -> None:
        self.published.append((run_id, seq))


class AgentRuntimeRepositoryTests(unittest.IsolatedAsyncioTestCase):
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
                username="runtime-user",
                display_name="Runtime User",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            db.add(user)
            db.flush()
            first = AiChatSession(
                user_id=user.id,
                title="first",
                chat_mode="stock",
                source_type="web",
            )
            second = AiChatSession(
                user_id=user.id,
                title="second",
                chat_mode="stock",
                source_type="web",
            )
            db.add_all([first, second])
            db.commit()
            self.user_id = user.id
            self.session_id = first.id
            self.other_session_id = second.id
        self.repository = AgentRunRepository(self.SessionFactory)

    def tearDown(self) -> None:
        self.engine.dispose()

    def create_run(
        self,
        *,
        session_id: int | None = None,
        idempotency_key: str | None = None,
        priority: int = 100,
    ) -> tuple[dict, bool]:
        resolved_session_id = session_id or self.session_id
        return self.repository.create_run(
            session_id=resolved_session_id,
            user_id=self.user_id,
            engine="agentscope",
            chat_mode="stock",
            request_payload={
                "session_id": resolved_session_id,
                "user_id": self.user_id,
                "question": "test",
            },
            idempotency_key=idempotency_key,
            priority=priority,
        )

    def test_idempotency_reuses_one_run(self) -> None:
        first, created = self.create_run(idempotency_key="same-request")
        duplicate, duplicate_created = self.create_run(idempotency_key="same-request")

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first["run_id"], duplicate["run_id"])
        with self.SessionFactory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(AgentRun)), 1)

    def test_active_run_lock_is_scoped_to_one_session(self) -> None:
        run, _ = self.create_run()

        self.assertEqual(
            self.repository.active_run_for_session(self.user_id, self.session_id)["run_id"],
            run["run_id"],
        )
        self.assertIsNone(
            self.repository.active_run_for_session(self.user_id, self.other_session_id)
        )

        self.repository.mark_terminal(run["run_id"], "completed")
        self.assertIsNone(
            self.repository.active_run_for_session(self.user_id, self.session_id)
        )

    def test_idempotency_race_compensation_removes_duplicate_message(self) -> None:
        with self.SessionFactory() as db:
            duplicate = AiChatMessage(
                session_id=self.session_id,
                role="user",
                content="duplicate",
                event_count=0,
            )
            db.add(duplicate)
            db.commit()
            duplicate_id = duplicate.id
            _discard_duplicate_preparation(
                {
                    "session_id": self.session_id,
                    "user_message_id": duplicate_id,
                    "created_session": False,
                    "attachment_metadata": [],
                },
                db,
                user_id=self.user_id,
                canonical_session_id=self.session_id,
            )
            self.assertIsNone(db.get(AiChatMessage, duplicate_id))

    def test_events_are_ordered_replayable_and_projected(self) -> None:
        run, _ = self.create_run()
        run_id = run["run_id"]
        events = [
            {"type": "stage_start", "stage": "agent_loop"},
            {
                "type": "action",
                "tool_call_id": "call-1",
                "tool": "ws_write_file",
                "content": '{"path":"report.md"}',
                "risk_level": "medium",
                "has_side_effects": True,
            },
            {
                "type": "approval_required",
                "tool_call_id": "call-1",
                "risk_level": "high",
            },
            {
                "type": "approval_resolved",
                "tool_call_id": "call-1",
                "decision": "approved",
            },
            {"type": "tool_done", "tool_call_id": "call-1", "success": True},
            {"type": "stage_done", "stage": "agent_loop", "status": "completed"},
        ]
        for event in events:
            self.repository.append_event(run_id, event)

        replay = self.repository.events_after(run_id, 2)
        self.assertEqual([item.seq for item in replay], [3, 4, 5, 6])
        self.assertTrue(all(item.payload["run_id"] == run_id for item in replay))
        with self.SessionFactory() as db:
            step = db.scalar(select(AgentRunStep).where(AgentRunStep.run_id == run_id))
            tool = db.scalar(select(AgentToolExecution).where(AgentToolExecution.run_id == run_id))
            self.assertIsNotNone(step)
            self.assertEqual(step.status, "completed")
            self.assertIsNotNone(tool)
            self.assertEqual(tool.status, "completed")
            self.assertEqual(tool.tool_call_id, "call-1")
            self.assertTrue(tool.has_side_effects)
        self.assertTrue(self.repository.has_side_effects(run_id))

    def test_terminal_run_closes_unfinished_inline_tool(self) -> None:
        run, _ = self.create_run()
        run_id = run["run_id"]
        self.repository.append_event(
            run_id,
            {
                "type": "action",
                "tool_call_id": "call-failed-dispatch",
                "tool": "dispatch_agent_tasks",
                "content": '{"tasks":[]}',
                "risk_level": "high",
                "has_side_effects": True,
            },
        )

        self.repository.mark_terminal(run_id, "failed", error="子任务创建失败")

        with self.SessionFactory() as db:
            tool = db.scalar(select(AgentToolExecution).where(AgentToolExecution.run_id == run_id))
            self.assertEqual(tool.status, "failed")
            self.assertEqual(tool.error, "子任务创建失败")
            self.assertIsNotNone(tool.finished_at)

    def test_same_session_is_serialized_while_other_sessions_can_run(self) -> None:
        first, _ = self.create_run(priority=300)
        second, _ = self.create_run(priority=200)
        other, _ = self.create_run(session_id=self.other_session_id, priority=100)

        self.assertEqual(self.repository.next_queued_run(), first["run_id"])
        self.assertIsNotNone(self.repository.claim_run(first["run_id"], "worker-a"))
        self.assertEqual(self.repository.next_queued_run(), other["run_id"])

        self.repository.mark_terminal(first["run_id"], "completed")
        self.assertEqual(self.repository.next_queued_run(), second["run_id"])

    def test_retry_checkpoint_and_approval_resume_are_durable(self) -> None:
        run, _ = self.create_run()
        run_id = run["run_id"]
        claimed = self.repository.claim_run(run_id, "worker-a")
        self.assertEqual(claimed["attempts"], 1)

        self.assertTrue(self.repository.requeue(run_id, error="HTTP 502"))
        retried = self.repository.claim_run(run_id, "worker-b")
        self.assertEqual(retried["attempts"], 2)
        with self.SessionFactory() as db:
            db.add(AgentToolApproval(
                approval_id="approval-1",
                run_id=run_id,
                tool_call_id="call-1",
                session_id=self.session_id,
                user_id=self.user_id,
                tool_name="ws_write_file",
                display_name="write",
                risk_level="medium",
                status="pending",
                expires_at=datetime.utcnow() + timedelta(minutes=5),
            ))
            db.commit()
        self.assertFalse(self.repository.mark_waiting(run_id))
        self.repository.save_checkpoint(
            run_id,
            engine="agentscope",
            state_json='{"session_id":"1","context":[]}',
            waiting=[{"tool_call_id": "call-1"}],
        )

        checkpoint = self.repository.load_checkpoint(run_id)
        self.assertEqual(checkpoint["engine"], "agentscope")
        self.assertEqual(checkpoint["waiting"][0]["tool_call_id"], "call-1")
        with self.SessionFactory() as db:
            approval = db.get(AgentToolApproval, "approval-1")
            approval.status = "approved"
            approval.resolved_at = datetime.utcnow()
            db.commit()
        self.assertTrue(self.repository.requeue(run_id))
        resumed = self.repository.get_run(run_id)
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["attempts"], 2)

    def test_approval_resolved_before_worker_parks_requeues_immediately(self) -> None:
        run, _ = self.create_run()
        self.repository.claim_run(run["run_id"], "worker-a")

        self.assertTrue(self.repository.mark_waiting(run["run_id"]))
        self.assertEqual(self.repository.get_run(run["run_id"])["status"], "queued")

    def test_agentscope_checkpoint_resumes_with_the_recorded_decision(self) -> None:
        from agentscope.message import AssistantMsg, ToolCallBlock, ToolCallState
        from agentscope.state import AgentState

        run, _ = self.create_run()
        with self.SessionFactory() as db:
            db.add(AgentToolApproval(
                approval_id="approval-resume",
                run_id=run["run_id"],
                tool_call_id="call-resume",
                session_id=self.session_id,
                user_id=self.user_id,
                tool_name="ws_write_file",
                display_name="write",
                risk_level="medium",
                status="approved",
                resolved_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(minutes=5),
            ))
            db.commit()
        state = AgentState(
            session_id=str(self.session_id),
            reply_id="reply-resume",
            context=[AssistantMsg(
                "assistant",
                [ToolCallBlock(
                    id="call-resume",
                    name="ws_write_file",
                    input='{"path":"report.md"}',
                    state=ToolCallState.ASKING,
                )],
            )],
        )

        with patch(
            "backend.app.agent_runtime.agentscope_engine.SessionLocal",
            self.SessionFactory,
        ):
            resume_event, resolved_events = _resume_event(run["run_id"], state)
        self.assertIsNotNone(resume_event)
        self.assertTrue(resume_event.confirm_results[0].confirmed)
        self.assertEqual(resolved_events[0]["decision"], "approved")

    def test_cancelled_queued_run_is_not_active(self) -> None:
        run, _ = self.create_run()
        self.assertIsNone(self.repository.request_cancel(run["run_id"], user_id=self.user_id + 1))
        cancelled = self.repository.request_cancel(run["run_id"], user_id=self.user_id)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["cancelled_now"])
        repeated = self.repository.request_cancel(run["run_id"], user_id=self.user_id)
        self.assertFalse(repeated["cancelled_now"])
        self.assertEqual(self.repository.active_runs(self.user_id), [])

    def test_cancelled_run_closes_steps_tools_and_pending_approvals(self) -> None:
        run, _ = self.create_run()
        run_id = run["run_id"]
        with self.SessionFactory() as db:
            step = AgentRunStep(
                run_id=run_id,
                seq=1,
                step_key="pending-work",
                status="running",
            )
            db.add(step)
            db.flush()
            db.add_all(
                [
                    AgentToolExecution(
                        execution_id="queued-tool",
                        run_id=run_id,
                        step_id=step.id,
                        tool_call_id="queued-call",
                        tool_name="read_data",
                        execution_mode="external",
                        status="queued",
                    ),
                    AgentToolExecution(
                        execution_id="side-effect-tool",
                        run_id=run_id,
                        step_id=step.id,
                        tool_call_id="side-effect-call",
                        tool_name="write_data",
                        execution_mode="external",
                        status="running",
                        has_side_effects=True,
                        lease_owner="worker-1",
                    ),
                    AgentToolApproval(
                        approval_id="pending-approval",
                        run_id=run_id,
                        tool_call_id="queued-call",
                        session_id=self.session_id,
                        user_id=self.user_id,
                        tool_name="read_data",
                        display_name="读取数据",
                        risk_level="medium",
                        status="pending",
                        expires_at=datetime.utcnow() + timedelta(minutes=5),
                    ),
                ]
            )
            db.commit()

        cancelled = self.repository.request_cancel(run_id, user_id=self.user_id)
        self.assertTrue(cancelled["cancelled_now"])
        with self.SessionFactory() as db:
            step = db.scalar(select(AgentRunStep).where(AgentRunStep.run_id == run_id))
            tools = {
                row.execution_id: row
                for row in db.scalars(
                    select(AgentToolExecution).where(AgentToolExecution.run_id == run_id)
                ).all()
            }
            approval = db.get(AgentToolApproval, "pending-approval")
            self.assertEqual(step.status, "cancelled")
            self.assertEqual(tools["queued-tool"].status, "cancelled")
            self.assertEqual(tools["side-effect-tool"].status, "unknown")
            self.assertIsNone(tools["side-effect-tool"].lease_owner)
            self.assertEqual(approval.status, "cancelled")
            self.assertIsNotNone(approval.resolved_at)

    async def test_event_sink_coalesces_deltas_before_persisting(self) -> None:
        run, _ = self.create_run()
        broker = RecordingBroker()
        sink = DurableEventSink(run["run_id"], self.repository, broker)  # type: ignore[arg-type]

        await sink.emit({"type": "content_delta", "content": "hello "})
        await sink.emit({"type": "content_delta", "content": "world"})
        await sink.emit({"type": "stage_start", "stage": "next"})

        replay = self.repository.events_after(run["run_id"], 0)
        self.assertEqual(len(replay), 2)
        self.assertEqual(replay[0].payload["content"], "hello world")
        self.assertEqual(broker.published, [(run["run_id"], 1), (run["run_id"], 2)])

    async def test_event_sink_redacts_sensitive_tool_arguments(self) -> None:
        run, _ = self.create_run()
        broker = RecordingBroker()
        sink = DurableEventSink(run["run_id"], self.repository, broker)  # type: ignore[arg-type]
        await sink.emit({
            "type": "action",
            "tool_call_id": "call-secret",
            "tool": "ws_write_file",
            "content": '{"path":"report.md","content":"private body","api_key":"secret-value"}',
        })

        event = self.repository.events_after(run["run_id"], 0)[0].payload
        self.assertNotIn("private body", event["content"])
        self.assertNotIn("secret-value", event["content"])
        self.assertIn("characters", event["content"])
        canonical = '{"api_key":"secret-value","content":"private body","path":"report.md"}'
        self.assertEqual(
            event["arguments_sha256"],
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    async def test_event_sink_coalesces_and_redacts_tool_progress(self) -> None:
        run, _ = self.create_run()
        broker = RecordingBroker()
        sink = DurableEventSink(run["run_id"], self.repository, broker)  # type: ignore[arg-type]
        await sink.emit({
            "type": "tool_progress",
            "tool_call_id": "call-progress",
            "tool": "ws_write_file",
            "chars": 10,
            "delta": "private",
        })
        await sink.emit({
            "type": "tool_progress",
            "tool_call_id": "call-progress",
            "tool": "ws_write_file",
            "chars": 20,
            "delta": " body",
        })
        await sink.emit({"type": "stage_start", "stage": "after-tool"})

        events = self.repository.events_after(run["run_id"], 0)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].payload["chars"], 20)
        self.assertEqual(events[0].payload["delta"], "")

    def test_event_retention_never_prunes_active_runs(self) -> None:
        old_terminal, _ = self.create_run()
        active, _ = self.create_run(session_id=self.other_session_id)
        self.repository.append_event(old_terminal["run_id"], {"type": "done"})
        self.repository.append_event(active["run_id"], {"type": "content_delta", "content": "active"})
        old_time = datetime.utcnow() - timedelta(days=30)
        with self.SessionFactory() as db:
            terminal_row = db.get(AgentRun, old_terminal["run_id"])
            terminal_row.status = "completed"
            terminal_row.finished_at = old_time
            for event in db.scalars(select(AgentRunEvent)).all():
                event.created_at = old_time
            db.commit()

        self.assertEqual(self.repository.prune_terminal_events(14), 1)
        self.assertEqual(self.repository.events_after(old_terminal["run_id"], 0), [])
        self.assertEqual(len(self.repository.events_after(active["run_id"], 0)), 1)


class AgentRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    def test_engine_selection_is_direct_with_explicit_legacy_rollback(self) -> None:
        self.assertEqual(select_engine("stock", "agentscope"), "agentscope")
        self.assertEqual(select_engine("research", "agentscope"), "agentscope")
        self.assertEqual(select_engine("knowledge", "agentscope"), "agentscope")
        self.assertEqual(select_engine("research", "legacy"), "legacy")
        self.assertEqual(select_engine("stock", "agentscope-stock"), "agentscope")
        self.assertEqual(select_engine("research", "agentscope-stock"), "legacy")
        available, version = agentscope_available()
        self.assertTrue(available)
        self.assertTrue(version)

    def test_sse_contains_durable_event_id(self) -> None:
        frame = format_sse({"type": "content_delta", "seq": 12, "content": "ok"})
        self.assertIn("id: 12\n", frame)
        self.assertIn("event: content_delta\n", frame)
        self.assertIn('"seq": 12', frame)

    def test_retry_classifier_separates_transient_and_permanent_errors(self) -> None:
        self.assertTrue(AgentRunExecutor._is_retryable("HTTP 502 upstream failed"))
        self.assertTrue(AgentRunExecutor._is_retryable("model request timed out"))
        self.assertFalse(AgentRunExecutor._is_retryable("HTTP 400 invalid request"))
        self.assertFalse(AgentRunExecutor._is_retryable("authentication failed"))

    async def test_redis_absence_falls_back_to_mysql_coordination(self) -> None:
        broker = RedisBroker("")
        self.assertEqual(await broker.acquire_lease("run:1", 60), "mysql-fallback")
        self.assertFalse(await broker.enqueue("run-1"))
        self.assertIsNone(await broker.dequeue())
        self.assertEqual(
            await broker.health(),
            {"configured": False, "ok": False, "version": None},
        )

    async def test_agentscope_tool_success_has_terminal_success_state(self) -> None:
        from agentscope.message import ToolResultState

        schema = {
            "type": "function",
            "function": {
                "name": "contract_tool",
                "description": "Contract test tool",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                },
            },
        }
        with patch(
            "backend.app.agent_runtime.agentscope_engine._execute_tool",
            return_value={"ok": True},
        ):
            tool = _build_tools(
                {"approval_policy": "full", "user_id": 1},
                [schema],
                set(),
            )[0]
            result = await tool.call(value=1)
        self.assertEqual(result.state, ToolResultState.SUCCESS)

    async def test_agentscope_adapter_translates_stream_and_checkpoints_state(self) -> None:
        from agentscope.event import (
            ReplyEndEvent,
            ReplyEndReason,
            TextBlockDeltaEvent,
            TextBlockEndEvent,
            TextBlockStartEvent,
        )

        class FakeModel:
            class Parameters:
                def __init__(self, **kwargs) -> None:
                    self.values = kwargs

            def __init__(self, **kwargs) -> None:
                self.values = kwargs

        class FakeAgent:
            def __init__(self, *, state, **kwargs) -> None:
                self.state = state

            async def reply_stream(self, _inputs):
                yield TextBlockStartEvent(reply_id="reply-1", block_id="text-1")
                yield TextBlockDeltaEvent(reply_id="reply-1", block_id="text-1", delta="hello")
                yield TextBlockDeltaEvent(reply_id="reply-1", block_id="text-1", delta=" world")
                yield TextBlockEndEvent(reply_id="reply-1", block_id="text-1")
                yield ReplyEndEvent(
                    session_id="1",
                    reply_id="reply-1",
                    finished_reason=ReplyEndReason.COMPLETED,
                )

        emitted = []
        checkpoints = []

        async def emit(event):
            emitted.append(event)

        async def save_checkpoint(state_json, waiting):
            checkpoints.append((state_json, waiting))

        async def is_cancelled():
            return False

        llm = {
            "id": 1,
            "display_name": "test",
            "provider_name": "test",
            "base_url": "https://example.invalid/v1",
            "api_key": "test-key",
            "model": "test-model",
            "temperature": 0.2,
            "max_steps": 3,
        }
        with (
            patch(
                "backend.app.agent_runtime.agentscope_engine.active_llm_config",
                return_value=llm,
            ),
            patch(
                "backend.app.agent_runtime.agentscope_engine._build_definition",
                return_value=("system", [], set()),
            ),
            patch(
                "backend.app.agent_runtime.agentscope_engine._build_tools",
                return_value=[],
            ),
            patch("agentscope.model.OpenAIChatModel", FakeModel),
            patch("agentscope.agent.Agent", FakeAgent),
        ):
            outcome = await run_agentscope(
                run_id="run-contract",
                request_payload={
                    "session_id": 1,
                    "user_id": 1,
                    "question": "test",
                    "user_text": "test",
                    "history": [],
                    "chat_mode": "stock",
                },
                checkpoint_json=None,
                emit=emit,
                save_checkpoint=save_checkpoint,
                is_cancelled=is_cancelled,
            )

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.final_answer, "hello world")
        self.assertEqual(
            [event["type"] for event in emitted],
            [
                "model_selected",
                "stage_start",
                "content_delta",
                "content_delta",
                "content_end",
                "stage_done",
            ],
        )
        self.assertTrue(checkpoints)
        self.assertIsNone(checkpoints[-1][1])

    async def _run_schedule_mutation_stream(self, result, state):
        from agentscope.event import (
            TextBlockDeltaEvent,
            TextBlockEndEvent,
            TextBlockStartEvent,
            ToolCallDeltaEvent,
            ToolCallEndEvent,
            ToolCallStartEvent,
            ToolResultEndEvent,
        )

        class FakeModel:
            class Parameters:
                def __init__(self, **kwargs) -> None:
                    self.values = kwargs

            def __init__(self, **kwargs) -> None:
                self.values = kwargs

        class FakeAgent:
            def __init__(self, *, state, **kwargs) -> None:
                self.state = state

            async def reply_stream(self, _inputs):
                yield ToolCallStartEvent(
                    reply_id="reply-schedule",
                    tool_call_id="call-schedule",
                    tool_call_name="create_agent_schedule",
                )
                yield ToolCallDeltaEvent(
                    reply_id="reply-schedule",
                    tool_call_id="call-schedule",
                    delta="{}",
                )
                yield ToolCallEndEvent(
                    reply_id="reply-schedule",
                    tool_call_id="call-schedule",
                )
                yield ToolResultEndEvent(
                    reply_id="reply-schedule",
                    tool_call_id="call-schedule",
                    state=state,
                    metadata={"result": result, "execution_id": "exec-schedule"},
                )
                yield TextBlockStartEvent(reply_id="reply-schedule", block_id="fake-answer")
                yield TextBlockDeltaEvent(
                    reply_id="reply-schedule",
                    block_id="fake-answer",
                    delta="创建成功，任务编号 agent-invented。",
                )
                yield TextBlockEndEvent(reply_id="reply-schedule", block_id="fake-answer")

        emitted = []
        checkpoints = []

        async def emit(event):
            emitted.append(event)

        async def save_checkpoint(state_json, waiting):
            checkpoints.append((state_json, waiting))

        async def is_cancelled():
            return False

        llm = {
            "id": 1,
            "display_name": "test",
            "provider_name": "test",
            "base_url": "https://example.invalid/v1",
            "api_key": "test-key",
            "model": "test-model",
            "temperature": 0.2,
            "max_steps": 3,
        }
        with (
            patch(
                "backend.app.agent_runtime.agentscope_engine.active_llm_config",
                return_value=llm,
            ),
            patch(
                "backend.app.agent_runtime.agentscope_engine._build_definition",
                return_value=("system", [], {"create_agent_schedule"}),
            ),
            patch(
                "backend.app.agent_runtime.agentscope_engine._build_tools",
                return_value=[],
            ),
            patch("agentscope.model.OpenAIChatModel", FakeModel),
            patch("agentscope.agent.Agent", FakeAgent),
        ):
            outcome = await run_agentscope(
                run_id="run-schedule-contract",
                request_payload={
                    "session_id": 1,
                    "user_id": 1,
                    "question": "创建午间定时任务",
                    "user_text": "创建午间定时任务",
                    "history": [],
                    "chat_mode": "research",
                    "digital_human_code": "stock-pm",
                },
                checkpoint_json=None,
                emit=emit,
                save_checkpoint=save_checkpoint,
                is_cancelled=is_cancelled,
            )
        return outcome, emitted

    async def test_failed_schedule_mutation_cannot_stream_fake_success(self) -> None:
        from agentscope.message import ToolResultState

        outcome, emitted = await self._run_schedule_mutation_stream(
            {
                "error": "数字人 invalid-agent 不可用于定时任务。",
                "verified": False,
            },
            ToolResultState.ERROR,
        )

        self.assertEqual(outcome.status, "failed")
        self.assertIn("禁止生成成功回执", outcome.error)
        self.assertIn("invalid-agent", outcome.error)
        self.assertIn("schedule_mutation_rejected", [event["type"] for event in emitted])
        self.assertNotIn(
            "agent-invented",
            "".join(str(event.get("content") or "") for event in emitted),
        )

    async def test_verified_schedule_mutation_uses_server_receipt_and_real_job_key(self) -> None:
        from agentscope.message import ToolResultState

        outcome, emitted = await self._run_schedule_mutation_stream(
            {
                "created": True,
                "already_exists": False,
                "verified": True,
                "job": {
                    "job_key": "agent-1-real123456789",
                    "name": "午间持仓全员分析",
                    "target_agent_code": "stock-pm",
                    "schedule_type": "daily",
                    "run_time": "12:00",
                    "trading_day_only": True,
                    "push_to_feishu": True,
                    "enabled": True,
                },
            },
            ToolResultState.SUCCESS,
        )

        self.assertEqual(outcome.status, "completed")
        self.assertIn("数据库核验", outcome.final_answer)
        self.assertIn("agent-1-real123456789", outcome.final_answer)
        self.assertNotIn("agent-invented", outcome.final_answer)
        streamed = "".join(
            str(event.get("content") or "")
            for event in emitted
            if event["type"] == "content_delta"
        )
        self.assertEqual(streamed, outcome.final_answer)

    def test_unknown_external_tool_error_keeps_the_concrete_cause(self) -> None:
        from backend.app.agent_runtime.external_tools import ExternalToolExecutor

        result = ExternalToolExecutor._error_result(
            {
                "status": "unknown",
                "execution_id": "exec-unknown",
                "error": "数字人 orchestrator 不可用于定时任务。",
            }
        )

        self.assertIn("orchestrator", result["error"])
        self.assertIn("Verify the business result", result["error"])
        self.assertEqual(result["execution_status"], "unknown")


if __name__ == "__main__":
    unittest.main()
