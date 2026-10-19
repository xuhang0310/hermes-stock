from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.agents import stock_agent
from backend.app.core.database import Base
from backend.app.core.models import (
    AgentProfile,
    AgentReport,
    DigitalHumanProfile,
    Event,
    Instrument,
    Position,
    PositionHoldingSnapshot,
    RiskCheck,
    User,
    WatchlistGroup,
    WatchlistItem,
)
from backend.app.data.repository import StockRepository
from backend.app.data.user_scope import data_user_scope
from backend.app.market import digital_human_tools
from backend.app.scheduler import rhythm_scheduler


class MultiUserSecurityRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_agent_sql_is_user_scoped_and_sensitive_schema_stays_closed(self) -> None:
        # 完整的隔离与绕过用例见 tests/test_agent_sql_sandbox.py。
        self.assertIn("没有用户身份", stock_agent.run_sql("SELECT * FROM positions")["error"])
        self.assertIn("不在可查询范围", stock_agent.validate_sql("SELECT password_hash FROM users", owner_user_id=1))
        self.assertIn("不开放查询", stock_agent.get_schema("users")["error"])
        self.assertEqual(stock_agent.get_schema("positions")["scope"], "personal")
        self.assertIn("run_sql", {item["function"]["name"] for item in stock_agent.TOOLS})

    def test_reports_and_risks_are_private_to_repository_owner(self) -> None:
        with self.SessionFactory() as db:
            user_a = User(username="report-a", display_name="Report A", password_hash="x", role="researcher", is_enabled=True)
            user_b = User(username="report-b", display_name="Report B", password_hash="x", role="researcher", is_enabled=True)
            profile = AgentProfile(agent_name="risk", display_name="风控", enabled=True)
            db.add_all([user_a, user_b, profile])
            db.flush()
            db.add_all(
                [
                    AgentReport(
                        owner_user_id=user_a.id,
                        report_id="report-a",
                        agent_name="risk",
                        title="A 的持仓报告",
                        content="private-a",
                    ),
                    AgentReport(
                        owner_user_id=user_b.id,
                        report_id="report-b",
                        agent_name="risk",
                        title="B 的持仓报告",
                        content="private-b",
                    ),
                    RiskCheck(
                        owner_user_id=user_a.id,
                        risk_level="high",
                        risk_type="position",
                        finding="A 风险",
                    ),
                    RiskCheck(
                        owner_user_id=user_b.id,
                        risk_level="low",
                        risk_type="position",
                        finding="B 风险",
                    ),
                ]
            )
            db.commit()

            repo_a = StockRepository(db, owner_user_id=user_a.id)
            self.assertEqual([row["report_id"] for row in repo_a.reports()], ["report-a"])
            self.assertIsNone(repo_a.report_detail("report-b"))
            self.assertEqual([row["finding"] for row in repo_a.risk_checks()], ["A 风险"])
            self.assertEqual(next(row for row in repo_a.agents() if row["agent_name"] == "risk")["report_count"], 1)

    def test_symbol_context_joins_snapshot_by_account_and_open_status(self) -> None:
        one_results = [
            {"symbol": "600000.SH", "name": "浦发银行"},
            {"symbol": "600000.SH", "account_id": 11, "status": "open"},
        ]
        with data_user_scope(7), patch.object(
            digital_human_tools, "_one", side_effect=one_results
        ) as one_mock, patch.object(digital_human_tools, "_query", return_value=[]):
            result = digital_human_tools.get_symbol_context("600000")

        position_sql = one_mock.call_args_list[1].args[0]
        self.assertIn("GROUP BY account_id, symbol", position_sql)
        self.assertIn("COALESCE(latest.account_id, 0) = COALESCE(p.account_id, 0)", position_sql)
        self.assertIn("p.status = 'open'", position_sql)
        self.assertEqual(result["position"]["account_id"], 11)

    def test_internal_report_queries_include_current_owner(self) -> None:
        captured: list[tuple[str, dict | None]] = []

        def fake_query(sql: str, params: dict | None = None) -> list[dict]:
            captured.append((sql, params))
            return []

        with data_user_scope(23), patch.object(digital_human_tools, "_query", side_effect=fake_query):
            digital_human_tools.get_recent_internal_evidence("600000", limit=3)
            digital_human_tools.get_symbol_reports("600000", limit=3)

        private_queries = [(sql, params) for sql, params in captured if "agent_reports" in sql or "risk_checks" in sql]
        self.assertTrue(private_queries)
        self.assertTrue(all("owner_user_id = :owner_user_id" in sql for sql, _ in private_queries))
        self.assertTrue(all(params and params.get("owner_user_id") == 23 for _, params in private_queries))

    def test_personal_events_are_hidden_but_public_events_remain_visible(self) -> None:
        with self.SessionFactory() as db:
            user_a = User(username="event-a", display_name="Event A", password_hash="x", role="researcher")
            user_b = User(username="event-b", display_name="Event B", password_hash="x", role="researcher")
            db.add_all([user_a, user_b])
            db.flush()
            db.add_all(
                [
                    Event(owner_user_id=user_a.id, event_id="event-a", level="L0", event_type="stop", title="A", source_type="rule_engine"),
                    Event(owner_user_id=user_b.id, event_id="event-b", level="L0", event_type="stop", title="B", source_type="rule_engine"),
                    Event(owner_user_id=None, event_id="event-public", level="L2", event_type="macro", title="Public", source_type="market"),
                ]
            )
            db.commit()

            repo_a = StockRepository(db, owner_user_id=user_a.id)
            self.assertEqual({row["event_id"] for row in repo_a.events()}, {"event-a", "event-public"})
            self.assertIsNone(repo_a.event_detail("event-b"))

    def test_price_sync_keeps_same_symbol_snapshots_separate_by_owner(self) -> None:
        with self.SessionFactory() as db:
            user_a = User(username="sync-a", display_name="Sync A", password_hash="x", role="researcher")
            user_b = User(username="sync-b", display_name="Sync B", password_hash="x", role="researcher")
            instrument = Instrument(symbol="600000.SH", name="浦发银行", market="SH", type="stock", status="active")
            db.add_all([user_a, user_b, instrument])
            db.flush()
            db.add_all(
                [
                    Position(owner_user_id=user_a.id, symbol=instrument.symbol, position_pct=0, quantity=100, cost_price=10, status="open"),
                    Position(owner_user_id=user_b.id, symbol=instrument.symbol, position_pct=0, quantity=200, cost_price=11, status="open"),
                ]
            )
            db.commit()
            owner_ids = {user_a.id, user_b.id}

        quotes = {
            "quotes": {"600000.SH": {"price": 12.0, "pre_close": 11.5}},
            "missing_symbols": [],
            "source": "test",
            "ms": 1,
        }
        with patch.object(rhythm_scheduler, "SessionLocal", self.SessionFactory), patch(
            "backend.app.market.stock_tools.get_realtime_quotes", return_value=quotes
        ):
            rhythm_scheduler._run_position_price_sync_locked(datetime(2026, 8, 10, 10, 0))

        with self.SessionFactory() as db:
            snapshots = db.query(PositionHoldingSnapshot).all()
            self.assertEqual(len(snapshots), 2)
            self.assertEqual({item.owner_user_id for item in snapshots}, owner_ids)
            self.assertEqual({item.position_quantity for item in snapshots}, {100, 200})

    def test_price_sync_fetches_symbols_that_only_exist_in_watchlists(self) -> None:
        with self.SessionFactory() as db:
            owner = User(
                username="watch-sync-owner",
                display_name="Watch Sync Owner",
                password_hash="x",
                role="researcher",
            )
            instrument = Instrument(
                symbol="600000.SH",
                name="浦发银行",
                market="SH",
                type="stock",
                status="active",
            )
            db.add_all([owner, instrument])
            db.flush()
            group = WatchlistGroup(owner_user_id=owner.id, name="我的自选")
            db.add(group)
            db.flush()
            db.add(
                WatchlistItem(
                    owner_user_id=owner.id,
                    symbol=instrument.symbol,
                    group_id=group.id,
                    status="active",
                )
            )
            db.commit()

        quotes = {
            "quotes": {"600000.SH": {"price": 12.0, "pre_close": 11.5}},
            "missing_symbols": [],
            "source": "tushare.rt_k",
            "ms": 1,
        }
        with patch.object(rhythm_scheduler, "SessionLocal", self.SessionFactory), patch(
            "backend.app.market.stock_tools.get_realtime_quotes",
            return_value=quotes,
        ) as quote_batch:
            result = rhythm_scheduler._run_position_price_sync_locked(
                datetime(2026, 8, 10, 10, 0)
            )

        quote_batch.assert_called_once_with(["600000.SH"])
        self.assertIn("1 个持仓/自选标的行情", result)
        with self.SessionFactory() as db:
            self.assertEqual(db.query(PositionHoldingSnapshot).count(), 0)

    def test_scheduled_report_retry_updates_same_owned_report(self) -> None:
        with self.SessionFactory() as db:
            owner = User(username="report-owner", display_name="Report Owner", password_hash="x", role="researcher")
            db.add_all(
                [
                    owner,
                    AgentProfile(agent_name="pm", display_name="投研总监", enabled=True),
                    DigitalHumanProfile(
                        code="stock-pm",
                        name="投研总监",
                        avatar_tone="professional",
                        target_roles="*",
                        description="测试",
                        system_prompt="测试",
                        quick_prompts_json="[]",
                        status="enabled",
                    ),
                ]
            )
            db.commit()
            owner_id = owner.id

        snap = {
            "owner_user_id": owner_id,
            "job_key": "owned-report-job",
            "name": "持仓日报",
            "prompt_template": "生成日报",
            "digital_human_code": "stock-pm",
            "push_channel_key": None,
        }
        with patch.object(rhythm_scheduler, "SessionLocal", self.SessionFactory), patch.object(
            rhythm_scheduler, "_run_scheduled_agent", side_effect=[("first", "model"), ("second", "model")]
        ):
            rhythm_scheduler._run_agent_report(snap, datetime(2026, 8, 10, 9, 0), "same-run")
            rhythm_scheduler._run_agent_report(snap, datetime(2026, 8, 10, 9, 1), "same-run")

        with self.SessionFactory() as db:
            reports = db.query(AgentReport).all()
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0].owner_user_id, owner_id)
            self.assertEqual(reports[0].content, "second")

    def test_legacy_report_documents_are_excluded_from_structured_tools(self) -> None:
        captured: list[str] = []

        def fake_query(sql: str, params: dict | None = None) -> list[dict]:
            captured.append(sql)
            return []

        with patch.object(digital_human_tools, "_query", side_effect=fake_query):
            digital_human_tools.get_symbol_documents("600000")
            digital_human_tools._recent_documents(5)

        self.assertEqual(len(captured), 2)
        self.assertTrue(all("doc_type <> 'report'" in sql for sql in captured))


if __name__ == "__main__":
    unittest.main()
