"""Regression coverage for reliability fixes that do not need MySQL or an LLM."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.agents.watchlist_agent import _execute_watchlist_tool
from backend.app.agent_runtime.tool_worker import _heartbeat_now
from backend.app.core.models import (
    Base,
    Instrument,
    Position,
    PositionAccount,
    PositionHoldingSnapshot,
    PositionImportBatch,
    PositionTrade,
)
from backend.app.data.repository import StockRepository
from backend.app.feishu import feishu_bot
from backend.app.scheduler import rhythm_scheduler
from backend.app.skills import agent_workspace
from scripts.deploy import api_upstream_command


class WorkspaceIsolationTests(unittest.TestCase):
    def test_each_user_gets_a_distinct_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(agent_workspace, "_WORKSPACE_ROOT", Path(tmp_dir)):
            with agent_workspace.workspace_user_scope(101):
                first = agent_workspace._ensure_root()
            with agent_workspace.workspace_user_scope(202):
                second = agent_workspace._ensure_root()

        self.assertNotEqual(first, second)
        self.assertEqual(first.name, "101")
        self.assertEqual(second.name, "202")


class DeploymentCommandTests(unittest.TestCase):
    def test_api_upstream_command_uses_real_newlines(self) -> None:
        command = api_upstream_command([18080, 18082])

        self.assertIn("<<'EOF'\nserver 127.0.0.1:18080", command)
        self.assertIn(";\nserver 127.0.0.1:18082", command)
        self.assertIn("\nEOF\nnginx -t", command)
        self.assertNotIn("\\nserver", command)

    def test_feishu_bot_management_is_routed_to_the_runtime_owner(self) -> None:
        config = Path("deploy/nginx/hermes-stock.conf").read_text(encoding="utf-8")

        route = "location ^~ /api/feishu-bots"
        self.assertIn(route, config)
        self.assertIn("proxy_pass http://127.0.0.1:18084;", config[config.index(route):])
        self.assertLess(config.index(route), config.index("location /api/"))


class FeishuBotRuntimeTests(unittest.TestCase):
    def test_missing_worker_is_stopped_instead_of_connecting(self) -> None:
        with patch.object(feishu_bot, "_workers", {}), patch.object(feishu_bot, "_spawn_errors", {}):
            runtime = feishu_bot.bot_runtime(42)

        self.assertEqual(runtime["state"], "stopped")
        self.assertFalse(runtime["running"])

    def test_connected_worker_is_reported_online(self) -> None:
        worker = SimpleNamespace(
            running=True,
            started_at=None,
            today_key="",
            today_count=0,
            last_event_at=None,
            error="旧的非连接错误",
        )
        with patch.object(feishu_bot, "_workers", {42: worker}):
            runtime = feishu_bot.bot_runtime(42)

        self.assertEqual(runtime["state"], "online")
        self.assertTrue(runtime["running"])


class TradingDayTests(unittest.TestCase):
    def setUp(self) -> None:
        rhythm_scheduler._trade_day_cache.clear()

    def test_exchange_calendar_overrides_weekday_fallback_and_is_cached(self) -> None:
        with patch("backend.app.market.stock_tools.get_trade_cal", return_value={"rows": [{"is_open": 0}]}) as trade_cal:
            self.assertFalse(rhythm_scheduler.is_trading_day(date(2026, 10, 1)))
            self.assertFalse(rhythm_scheduler.is_trading_day(date(2026, 10, 1)))

        self.assertEqual(trade_cal.call_count, 1)

    def test_weekends_do_not_query_the_calendar(self) -> None:
        with patch("backend.app.market.stock_tools.get_trade_cal") as trade_cal:
            self.assertFalse(rhythm_scheduler.is_trading_day(date(2026, 10, 3)))
        trade_cal.assert_not_called()


class HeartbeatClockTests(unittest.TestCase):
    def test_tool_worker_heartbeat_uses_naive_shanghai_time(self) -> None:
        timezone = ZoneInfo("Asia/Shanghai")
        before = datetime.now(timezone).replace(tzinfo=None)
        heartbeat_at = _heartbeat_now()
        after = datetime.now(timezone).replace(tzinfo=None)

        self.assertIsNone(heartbeat_at.tzinfo)
        self.assertLessEqual(before, heartbeat_at)
        self.assertLessEqual(heartbeat_at, after)


class PositionReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.repo = StockRepository(self.db)
        self.account = PositionAccount(broker_name="test", account_type="cash", market="A-share", currency="CNY")
        self.db.add(self.account)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _holding(self, quantity: float, *, as_of_date: str | None = None) -> dict:
        return {
            "symbol": "600000",
            "name": "Test Bank",
            "account_id": self.account.id,
            "position_quantity": quantity,
            "cost_price": 10,
            "latest_price": 10,
            "as_of_date": as_of_date,
        }

    def test_sell_rejects_oversell_and_records_execution_price(self) -> None:
        self.repo.merge_position_holding({**self._holding(100), "execution_price": 10}, action="buy")

        with self.assertRaisesRegex(ValueError, "exceeds current holding"):
            self.repo.merge_position_holding({**self._holding(101), "execution_price": 12}, action="sell")

        self.repo.merge_position_holding({**self._holding(60), "execution_price": 12}, action="sell")
        position = self.db.scalar(select(Position))
        trade = self.db.scalar(select(PositionTrade).where(PositionTrade.action == "sell"))

        self.assertEqual(position.quantity, 40)
        self.assertEqual(trade.quantity, 60)
        self.assertEqual(trade.price, 12)
        self.assertEqual(trade.amount, 720)

    def test_stale_import_does_not_replace_authoritative_position_summary(self) -> None:
        self.repo.save_position_holding(self._holding(100, as_of_date="2026-07-10"))
        self.repo.save_position_holding(self._holding(50, as_of_date="2026-07-01"))

        position = self.db.scalar(select(Position))
        self.assertEqual(position.quantity, 100)
        self.assertEqual(self.repo.positions()[0]["quantity"], 100)

    def test_incremental_merge_snapshot_is_linked_to_import_batch(self) -> None:
        batch = self.repo.create_position_import_batch(
            source_name="position-entry-agent",
            image_mime_type="text/plain",
            image_sha256=None,
            raw_text=None,
            parsed_json=None,
            created_by="tester",
        )

        self.repo.merge_position_holding(
            self._holding(100),
            action="buy",
            source_type="ai_ocr",
            import_batch=batch,
        )

        snapshot = self.db.scalar(select(PositionHoldingSnapshot))
        self.assertEqual(snapshot.import_batch_id, batch.id)

    def test_broker_only_reference_reuses_the_sole_active_account(self) -> None:
        self.account.broker_name = "方正证券"
        self.account.account_no_masked = "****1234"
        self.db.commit()

        resolved = self.repo._ensure_position_account(
            {"broker_name": "方正证券"},
            symbol="002594.SZ",
        )

        self.assertEqual(resolved.id, self.account.id)
        self.assertEqual(
            len(self.db.scalars(select(PositionAccount).where(PositionAccount.status == "active")).all()),
            1,
        )

    def test_rollback_restores_previous_snapshot_and_cannot_repeat(self) -> None:
        self.repo.save_position_holding(self._holding(100, as_of_date="2026-07-10"))
        batch = self.repo.create_position_import_batch(
            source_name="statement.png", image_mime_type="image/png", image_sha256=None,
            raw_text=None, parsed_json=None, created_by="tester",
        )
        rows = self.repo.import_position_rows([self._holding(50, as_of_date="2026-07-11")], batch)
        self.repo.finish_position_import_batch(batch, rows)

        result = self.repo.rollback_last_position_batch()
        position = self.db.scalar(select(Position))
        restored = self.db.scalar(
            select(PositionHoldingSnapshot).order_by(PositionHoldingSnapshot.as_of_date.desc()).limit(1)
        )

        self.assertEqual(result["rolled_back_count"], 1)
        self.assertEqual(position.quantity, 100)
        self.assertEqual(restored.position_quantity, 100)
        self.assertEqual(self.db.get(PositionImportBatch, batch.id).status, "rolled_back")
        self.assertIn("error", self.repo.rollback_last_position_batch())


class WatchlistReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.repo = StockRepository(self.db)
        self.db.add_all(
            [
                Instrument(symbol="600036.SH", name="招商银行", market="SSE", type="stock", status="active"),
                Instrument(symbol="000001.SZ", name="平安银行", market="SZSE", type="stock", status="active"),
            ]
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_add_normalizes_symbol_delete_hides_and_readd_restores(self) -> None:
        created = self.repo.add_watchlist_item("600036", "default", "银行板块", added_by="tester")
        self.assertEqual(created["symbol"], "600036.SH")
        self.assertEqual([row["symbol"] for row in self.repo.watchlist()], ["600036.SH"])

        deleted = self.repo.remove_watchlist_item(created["id"], removed_by="tester")
        self.assertTrue(deleted["deleted"])
        self.assertEqual(self.repo.watchlist(), [])

        restored = self.repo.add_watchlist_item("600036.SH", "default", "重新跟踪", added_by="tester")
        self.assertEqual(restored["id"], created["id"])
        self.assertEqual(self.repo.watchlist()[0]["reason"], "重新跟踪")

    def test_watchlist_agent_tools_add_by_name_and_remove_by_symbol(self) -> None:
        added = _execute_watchlist_tool(
            self.repo,
            "add_watchlist_items",
            {"rows": [{"name": "平安银行", "group_name": "default", "reason": "测试 Agent"}]},
            created_by="feishu:test",
        )
        self.assertEqual(added["changed_count"], 1)
        self.assertEqual(added["watchlist"][0]["symbol"], "000001.SZ")
        self.assertEqual(added["watchlist"][0]["group_name"], "我的自选")
        self.assertEqual(added["watchlist"][0]["reason"], "测试 Agent")

        removed = _execute_watchlist_tool(
            self.repo,
            "remove_watchlist_items",
            {"rows": [{"symbol": "000001"}]},
            created_by="feishu:test",
        )
        self.assertEqual(removed["changed_count"], 1)
        self.assertEqual(removed["watchlist"], [])

    def test_watchlist_agent_uses_default_reason_when_user_did_not_provide_one(self) -> None:
        added = _execute_watchlist_tool(
            self.repo,
            "add_watchlist_items",
            {"rows": [{"symbol": "600036.SH", "group_name": "临时分组", "reason": "  "}]},
            created_by="web:test",
        )

        self.assertEqual(added["changed_count"], 1)
        self.assertEqual(added["watchlist"][0]["group_name"], "我的自选")
        self.assertEqual(added["watchlist"][0]["reason"], "Agent录入")


if __name__ == "__main__":
    unittest.main()
