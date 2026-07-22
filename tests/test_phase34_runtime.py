from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.agent_runtime.artifact_service import ArtifactService
from backend.app.agent_runtime.repository import AgentRunRepository
from backend.app.agent_runtime.tool_repository import ToolExecutionRepository
from backend.app.agent_runtime.tool_specs import ToolSpec
from backend.app.agent_runtime.tool_worker import ToolWorker, initialize_tool_worker_runtime
from backend.app.agent_runtime.transaction_state import TransactionStateService
from backend.app.core.database import Base
from backend.app.core.models import (
    AgentArtifactVersion,
    AgentOutbox,
    AgentRun,
    AgentToolExecution,
    AiChatSession,
    User,
)


class RecordingToolBroker:
    def __init__(self) -> None:
        self.results: list[tuple[str, str]] = []
        self.dead: list[tuple[str, str, str | None]] = []

    async def publish_tool_result(self, execution_id: str, status: str) -> None:
        self.results.append((execution_id, status))

    async def dead_letter_tool(
        self,
        execution_id: str,
        status: str,
        error: str | None,
    ) -> None:
        self.dead.append((execution_id, status, error))

    async def acknowledge_tool_task(self, _message_id: str) -> None:
        return None


class Phase34RuntimeTests(unittest.IsolatedAsyncioTestCase):
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
                username="phase34-user",
                display_name="Phase 34 User",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            db.add(user)
            db.flush()
            chat_session = AiChatSession(
                user_id=user.id,
                title="phase 3/4",
                chat_mode="research",
                source_type="web",
            )
            db.add(chat_session)
            db.commit()
            self.user_id = user.id
            self.session_id = chat_session.id

        self.run_repository = AgentRunRepository(self.SessionFactory)
        self.tool_repository = ToolExecutionRepository(self.SessionFactory)
        self.run, _created = self.run_repository.create_run(
            session_id=self.session_id,
            user_id=self.user_id,
            engine="agentscope",
            chat_mode="research",
            request_payload={
                "session_id": self.session_id,
                "user_id": self.user_id,
                "user_label": "phase34-user",
                "question": "test",
                "chat_mode": "research",
            },
            source_type="scheduler",
            source_ref="fire-20260722-1",
        )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_tool_outbox_is_idempotent_and_source_is_auditable(self) -> None:
        spec = ToolSpec(name="read_data", execution_mode="worker", max_attempts=3)
        first, created = self.tool_repository.submit(
            run_id=self.run["run_id"],
            tool_name="read_data",
            arguments={"symbol": "600519.SH"},
            spec=spec,
        )
        duplicate, duplicate_created = self.tool_repository.submit(
            run_id=self.run["run_id"],
            tool_name="read_data",
            arguments={"symbol": "600519.SH"},
            spec=spec,
        )

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first["execution_id"], duplicate["execution_id"])
        with self.SessionFactory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(AgentToolExecution)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(AgentOutbox)), 1)
            run = db.get(AgentRun, self.run["run_id"])
            self.assertEqual(run.source_type, "scheduler")
            self.assertEqual(run.source_ref, "fire-20260722-1")

    def test_external_tool_promotes_action_placeholder(self) -> None:
        arguments = {"query": "内部情报", "limit": 8}
        canonical = '{"limit":8,"query":"内部情报"}'
        self.run_repository.append_event(
            self.run["run_id"],
            {
                "type": "action",
                "tool_call_id": "call-intel-1",
                "tool": "get_intelligence_digest",
                "content": json.dumps(arguments, ensure_ascii=False),
                "arguments_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "risk_level": "low",
                "has_side_effects": False,
            },
        )

        execution, created = self.tool_repository.submit(
            run_id=self.run["run_id"],
            tool_name="get_intelligence_digest",
            arguments=arguments,
            spec=ToolSpec(name="get_intelligence_digest", execution_mode="worker"),
        )

        self.assertTrue(created)
        self.assertEqual(execution["tool_call_id"], "call-intel-1")
        self.assertEqual(execution["execution_mode"], "worker")
        with self.SessionFactory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(AgentToolExecution)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(AgentOutbox)), 1)

    def test_tool_worker_runtime_discovers_skill_executors(self) -> None:
        with patch("backend.app.agent_runtime.tool_worker.skill_registry.discover") as discover:
            initialize_tool_worker_runtime()
        discover.assert_called_once_with()

    def test_retry_and_expired_write_lease_have_distinct_outcomes(self) -> None:
        read_spec = ToolSpec(name="read_data", execution_mode="worker", max_attempts=3)
        read, _ = self.tool_repository.submit(
            run_id=self.run["run_id"],
            tool_name="read_data",
            arguments={"page": 1},
            spec=read_spec,
        )
        self.tool_repository.claim(read["execution_id"], "worker-a", 60)
        status = self.tool_repository.fail(
            read["execution_id"],
            "worker-a",
            error="HTTP 502",
            error_code="upstream",
            retryable=True,
        )
        self.assertEqual(status, "retry")

        write_spec = ToolSpec(
            name="save_position_holdings",
            side_effect="business",
            idempotency="non_retryable",
            execution_mode="worker",
            max_attempts=1,
            is_read_only=False,
            is_concurrency_safe=False,
        )
        write, _ = self.tool_repository.submit(
            run_id=self.run["run_id"],
            tool_name="save_position_holdings",
            arguments={"rows": [{"symbol": "600519.SH"}]},
            spec=write_spec,
        )
        self.tool_repository.claim(write["execution_id"], "worker-b", 60)
        with self.SessionFactory() as db:
            row = db.get(AgentToolExecution, write["execution_id"])
            row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()

        recovered = self.tool_repository.recover_expired()
        self.assertIn(
            {"execution_id": write["execution_id"], "status": "unknown"},
            recovered,
        )
        self.assertEqual(self.tool_repository.get(write["execution_id"])["status"], "unknown")

    async def test_returned_business_error_is_preserved_as_completed_execution(self) -> None:
        spec = ToolSpec(
            name="save_position_holdings",
            side_effect="business",
            idempotency="non_retryable",
            execution_mode="worker",
            max_attempts=1,
            is_read_only=False,
            is_concurrency_safe=False,
        )
        execution, _ = self.tool_repository.submit(
            run_id=self.run["run_id"],
            tool_name="save_position_holdings",
            arguments={"rows": []},
            spec=spec,
        )
        broker = RecordingToolBroker()
        worker = ToolWorker(
            repository=self.tool_repository,
            broker=broker,  # type: ignore[arg-type]
            worker_id="worker-domain-error",
        )
        with (
            patch(
                "backend.app.agent_runtime.agentscope_engine._build_definition",
                return_value=("system", [], {"save_position_holdings"}),
            ),
            patch(
                "backend.app.agent_runtime.agentscope_engine._execute_tool",
                return_value={"error": "请选择证券账户", "need_account": True},
            ),
        ):
            await worker._execute(execution["execution_id"], None)

        resolved = self.tool_repository.get(execution["execution_id"])
        self.assertEqual(resolved["status"], "completed")
        self.assertEqual(resolved["result"]["error"], "请选择证券账户")
        self.assertEqual(broker.results, [(execution["execution_id"], "completed")])
        self.assertEqual(broker.dead, [])

    def test_artifact_versions_are_immutable_and_restorable(self) -> None:
        from backend.app.skills import agent_workspace

        service = ArtifactService(self.SessionFactory)
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            agent_workspace,
            "_WORKSPACE_ROOT",
            Path(temp_dir),
        ):
            user_root = Path(temp_dir) / "users" / str(self.user_id)
            target = user_root / "reports" / "research.md"
            target.parent.mkdir(parents=True)
            target.write_text("version one", encoding="utf-8")
            first = service.register_file(
                user_id=self.user_id,
                workspace_path="reports/research.md",
                session_id=self.session_id,
                run_id=self.run["run_id"],
                created_by="test",
            )
            target.write_text("version two", encoding="utf-8")
            second = service.register_file(
                user_id=self.user_id,
                workspace_path="reports/research.md",
                session_id=self.session_id,
                run_id=self.run["run_id"],
                created_by="test",
            )
            restored = service.restore(
                first["artifact_id"],
                1,
                user_id=self.user_id,
                run_id=self.run["run_id"],
            )

            self.assertEqual(first["version"], 1)
            self.assertEqual(second["version"], 2)
            self.assertEqual(restored["version"], 3)
            self.assertEqual(target.read_text(encoding="utf-8"), "version one")
            versions = service.list_versions(first["artifact_id"], user_id=self.user_id)
            self.assertEqual([item["version"] for item in versions], [3, 2, 1])
            with self.SessionFactory() as db:
                v3 = db.scalar(
                    select(AgentArtifactVersion).where(
                        AgentArtifactVersion.artifact_id == first["artifact_id"],
                        AgentArtifactVersion.version == 3,
                    )
                )
                self.assertEqual(v3.parent_version, 1)

    def test_position_transaction_slots_survive_follow_up_turns(self) -> None:
        service = TransactionStateService(self.SessionFactory)
        pending = {
            "tool": "save_position_holdings",
            "arguments": {
                "rows": [
                    {
                        "symbol": "600519.SH",
                        "position_quantity": 100,
                        "cost_price": 1500,
                    }
                ]
            },
        }
        request_payload = {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "digital_human_code": "position-entry-agent",
            "default_account_id": 7,
            "pending_action": pending,
        }
        state = service.initialize(request_payload)
        merged, state = service.before_tool(
            request_payload,
            "save_position_holdings",
            {"account": {"account_id": 7}},
        )

        self.assertEqual(state["slots"]["account_id"], 7)
        self.assertEqual(merged["rows"], pending["arguments"]["rows"])
        service.after_tool(
            request_payload,
            "save_position_holdings",
            merged,
            {"saved_count": 1, "rows": merged["rows"]},
        )
        persisted = service.load(self.session_id, "position-entry-agent")
        self.assertEqual(persisted["slots"]["account_id"], 7)
        self.assertIsNone(persisted["pending_action"])
        self.assertEqual(persisted["status"], "completed")


if __name__ == "__main__":
    unittest.main()
