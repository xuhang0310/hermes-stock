"""Regression coverage for reliability fixes that do not need MySQL or an LLM."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.core.models import Base, Position, PositionAccount, PositionHoldingSnapshot, PositionImportBatch, PositionTrade
from backend.app.data.repository import StockRepository
from backend.app.scheduler import rhythm_scheduler
from backend.app.skills import agent_workspace


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


if __name__ == "__main__":
    unittest.main()
