from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.agent_runtime.evidence_service import ResearchEvidenceService
from backend.app.agent_runtime.repository import AgentRunRepository
from backend.app.core.database import Base
from backend.app.core.models import (
    AiChatSession,
    ResearchCompletionAudit,
    ResearchEvidence,
    ResearchGoal,
    ResearchGoalCriterion,
    User,
)
from backend.app.skills.registry import SkillRegistry
from backend.app.skills.manifests import load_core_skill_manifests


class EvidenceDrivenResearchTests(unittest.TestCase):
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
                username="evidence-user",
                display_name="Evidence User",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            db.add(user)
            db.flush()
            session = AiChatSession(
                user_id=user.id,
                title="research",
                chat_mode="research",
                source_type="web",
            )
            db.add(session)
            db.commit()
            self.user_id = user.id
            self.session_id = session.id
        repository = AgentRunRepository(self.SessionFactory)
        run, _ = repository.create_run(
            session_id=self.session_id,
            user_id=self.user_id,
            engine="agentscope",
            chat_mode="research",
            request_payload={
                "session_id": self.session_id,
                "user_id": self.user_id,
                "question": "分析最新股价",
            },
        )
        self.run_id = run["run_id"]
        self.registry = SkillRegistry()
        self.service = ResearchEvidenceService(self.SessionFactory)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_missing_required_evidence_is_incomplete_then_becomes_complete(self) -> None:
        selected = self.registry.manifests_by_names(["market_snapshot"])
        goal = self.service.ensure_goal(
            run_id=self.run_id,
            user_id=self.user_id,
            objective="分析最新股价",
            selected=selected,
        )
        self.assertIsNotNone(goal)

        first_audit = self.service.audit(self.run_id, "暂时结论")
        self.assertEqual(first_audit["state"], "incomplete")
        self.assertEqual(first_audit["missing"][0]["code"], "market.current_price")

        evidence_ids = self.service.record_tool_result(
            run_id=self.run_id,
            tool_call_id="call-1",
            tool_name="get_latest_price",
            result={"symbol": "600519.SH", "price": 1500.0, "trade_date": datetime.now(timezone.utc).strftime("%Y%m%d")},
            execution_id=None,
            criterion_codes=["market.current_price"],
            success=True,
        )
        self.assertEqual(len(evidence_ids), 1)
        second_audit = self.service.audit(self.run_id, "行情证据已经取得")
        self.assertEqual(second_audit["state"], "complete")
        self.assertEqual(second_audit["satisfied_count"], 1)
        bundle = self.service.audit_bundle(self.run_id)
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle["goal"]["status"], "complete")
        self.assertEqual(bundle["evidence"][0]["tool_name"], "get_latest_price")
        self.assertEqual(bundle["evidence"][0]["verification_status"], "verified")
        self.assertEqual(bundle["evidence"][0]["freshness_status"], "fresh")
        self.assertEqual(bundle["latest_audit"]["state"], "complete")

        with self.SessionFactory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ResearchGoal)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(ResearchEvidence)), 1)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(ResearchCompletionAudit)), 2
            )

    def test_tool_call_evidence_is_idempotent(self) -> None:
        selected = self.registry.manifests_by_names(["market_snapshot"])
        self.service.ensure_goal(
            run_id=self.run_id,
            user_id=self.user_id,
            objective="分析最新股价",
            selected=selected,
        )
        kwargs = {
            "run_id": self.run_id,
            "tool_call_id": "same-call",
            "tool_name": "get_latest_price",
            "result": {"symbol": "600519.SH", "price": 1500.0},
            "execution_id": None,
            "criterion_codes": ["market.current_price"],
            "success": True,
        }
        first = self.service.record_tool_result(**kwargs)
        second = self.service.record_tool_result(**kwargs)
        self.assertEqual(first, second)
        with self.SessionFactory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ResearchEvidence)), 1)

    def test_empty_or_undated_result_does_not_satisfy_criterion(self) -> None:
        selected = self.registry.manifests_by_names(["news_and_events"])
        self.service.ensure_goal(
            run_id=self.run_id,
            user_id=self.user_id,
            objective="检查近期公告",
            selected=selected,
        )
        self.service.record_tool_result(
            run_id=self.run_id,
            tool_call_id="empty-news",
            tool_name="search_stock_news",
            result={"source": "search", "results": []},
            execution_id=None,
            criterion_codes=["events.source"],
            success=True,
        )
        audit = self.service.audit(self.run_id, "没有检索到事件")
        self.assertEqual(audit["state"], "incomplete")
        with self.SessionFactory() as db:
            evidence = db.scalar(select(ResearchEvidence))
            criterion = db.scalar(select(ResearchGoalCriterion))
            self.assertEqual(evidence.verification_status, "unverified")
            self.assertEqual(criterion.evidence_count, 0)

    def test_future_dated_evidence_is_rejected(self) -> None:
        selected = self.registry.manifests_by_names(["market_snapshot"])
        self.service.ensure_goal(
            run_id=self.run_id,
            user_id=self.user_id,
            objective="检查行情日期",
            selected=selected,
        )
        self.service.record_tool_result(
            run_id=self.run_id,
            tool_call_id="future-price",
            tool_name="get_latest_price",
            result={
                "symbol": "600519.SH",
                "price": 1500.0,
                "trade_date": "20990101",
                "source": "test",
            },
            execution_id=None,
            criterion_codes=["market.current_price"],
            success=True,
        )
        audit = self.service.audit(self.run_id, "未来日期不能作为证据")
        self.assertEqual(audit["state"], "incomplete")
        bundle = self.service.audit_bundle(self.run_id)
        self.assertEqual(bundle["evidence"][0]["freshness_status"], "invalid")
        self.assertEqual(bundle["evidence"][0]["verification_status"], "unverified")
        self.assertIn("future", " ".join(bundle["evidence"][0]["validation"]["reasons"]))


class ProgressiveSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = SkillRegistry()

    def test_resolver_loads_only_relevant_skill_details(self) -> None:
        selected = self.registry.resolve_manifests(
            "请分析均线、MACD 和近期量价趋势",
            "research",
            "quant-analyst",
        )
        names = [item.name for item in selected]
        self.assertIn("technical-analysis", names)
        self.assertGreaterEqual(len(names), 8)
        self.assertLessEqual(len(names), 12)

        context = self.registry.build_progressive_context(
            selected, "research", "quant-analyst"
        )
        self.assertIn("已激活", context)
        self.assertIn("technical-analysis", context)
        self.assertIn("可按需加载", context)

    def test_explicit_skill_is_pinned_before_automatic_routing(self) -> None:
        selected = self.registry.resolve_manifests(
            "分析这只股票",
            "research",
            "stock-pm",
            selected_names=["strategy-backtest"],
        )
        self.assertEqual(selected[0].name, "strategy-backtest")
        self.assertGreaterEqual(len(selected), 8)

    def test_explicit_skill_selection_is_canonical_and_role_scoped(self) -> None:
        selected = self.registry.validate_manifest_selection(
            ["news_and_events", "event-intelligence"],
            "research",
            "risk-officer",
        )
        self.assertEqual(selected, ["event-intelligence"])
        with self.assertRaisesRegex(ValueError, "不适用于当前数字人"):
            self.registry.validate_manifest_selection(
                ["financial-statement-analysis"],
                "research",
                "risk-officer",
            )

    def test_load_skill_returns_detailed_contract(self) -> None:
        result = self.registry.load_manifest("news_and_events")
        self.assertEqual(result["name"], "event-intelligence")
        self.assertTrue(result["instructions"])
        self.assertIn("执行流程", result["professional_body"])
        self.assertEqual(result["version"], "3.0.0")
        self.assertEqual(len(result["content_sha256"]), 64)
        self.assertTrue(result["output_contract"])
        self.assertEqual(result["criteria"][0]["code"], "events.source")
        self.assertIn("references/source-hierarchy.md", result["resources"])

    def test_load_skill_resource_is_scoped_to_skill_directory(self) -> None:
        result = self.registry.load_manifest(
            "event-intelligence", "references/source-hierarchy.md"
        )
        self.assertEqual(result["name"], "event-intelligence")
        self.assertIn("事件来源核验", result["content"])
        self.assertEqual(len(result["content_sha256"]), 64)
        escaped = self.registry.load_manifest(
            "event-intelligence", "../market-snapshot/SKILL.md"
        )
        self.assertIn("error", escaped)

    def test_tool_maps_to_selected_criterion(self) -> None:
        self.assertEqual(
            self.registry.criterion_codes_for_tool(
                "get_latest_price", ["market_snapshot", "technical_analysis"]
            ),
            ["market.current_price"],
        )

    def test_catalog_conforms_to_agent_skills_structure(self) -> None:
        manifests = load_core_skill_manifests()
        self.assertEqual(len(manifests), 20)
        for manifest in manifests:
            self.assertRegex(manifest.name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertEqual(manifest.skill_dir.name, manifest.name)
            self.assertTrue((manifest.skill_dir / "SKILL.md").is_file())
            self.assertGreater(len(manifest.summary), 20)
            self.assertLessEqual(len(manifest.summary), 1024)
            self.assertLess(len(manifest.instructions.splitlines()), 500)
            self.assertEqual(len(manifest.content_sha256), 64)
            self.assertTrue(all(isinstance(value, str) for value in manifest.metadata.values()))
            for resource in manifest.resources:
                self.assertLessEqual(len(Path(resource).parts), 2)

    def test_all_contract_tools_are_declared_and_real(self) -> None:
        self.registry.discover()
        executable = set(self.registry.all_tool_names()) | {"search_knowledge", "ws_run_python"}
        for manifest in load_core_skill_manifests():
            self.assertTrue(set(manifest.tool_names).issubset(set(manifest.allowed_tools)))
            self.assertFalse(set(manifest.tool_names) - executable, manifest.name)


if __name__ == "__main__":
    unittest.main()
