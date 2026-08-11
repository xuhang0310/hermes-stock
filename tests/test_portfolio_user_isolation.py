from __future__ import annotations

import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.core.models import Instrument, StockDailyBasic, StockLatestPrice, User
from backend.app.data.errors import PositionNotFoundError
from backend.app.data.repository import StockRepository


class PortfolioUserIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user_a = User(
            username="owner-a",
            display_name="Owner A",
            role="researcher",
            permission_scope="dashboard:view,data:write",
            password_hash="test",
        )
        self.user_b = User(
            username="owner-b",
            display_name="Owner B",
            role="researcher",
            permission_scope="dashboard:view,data:write",
            password_hash="test",
        )
        self.db.add_all(
            [
                self.user_a,
                self.user_b,
                Instrument(
                    symbol="600036.SH",
                    name="招商银行",
                    market="SSE",
                    type="stock",
                    status="active",
                ),
            ]
        )
        self.db.commit()
        self.repo_a = StockRepository(self.db, owner_user_id=self.user_a.id)
        self.repo_b = StockRepository(self.db, owner_user_id=self.user_b.id)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_watchlist_rows_with_same_symbol_and_group_are_private(self) -> None:
        item_a = self.repo_a.add_watchlist_item(
            "600036.SH",
            "我的自选",
            "A 的理由",
            added_by="owner-a",
        )
        item_b = self.repo_b.add_watchlist_item(
            "600036.SH",
            "我的自选",
            "B 的理由",
            added_by="owner-b",
        )
        self.db.add_all(
            [
                StockLatestPrice(
                    symbol="600036.SH",
                    name="招商银行",
                    price=38.25,
                    pre_close=37.5,
                    pct_chg=2.0,
                    trade_date="20260811",
                    source="tushare.rt_k",
                    fetched_at=datetime(2026, 8, 11, 10, 1),
                ),
                StockDailyBasic(
                    symbol="600036.SH",
                    trade_date="20260808",
                    total_mv=900_000,
                ),
                StockDailyBasic(
                    symbol="600036.SH",
                    trade_date="20260811",
                    total_mv=1_024_500,
                ),
            ]
        )
        self.db.commit()

        self.assertNotEqual(item_a["id"], item_b["id"])
        row_a = self.repo_a.watchlist()[0]
        row_b = self.repo_b.watchlist()[0]
        self.assertEqual(row_a["reason"], "A 的理由")
        self.assertEqual(row_b["reason"], "B 的理由")
        self.assertEqual(row_a["current_price"], 38.25)
        self.assertEqual(row_a["current_pct_chg"], 2.0)
        self.assertEqual(row_a["current_change"], 0.75)
        self.assertEqual(row_a["total_mv"], 1_024_500)
        self.assertEqual(row_a["total_mv_yi"], 102.45)
        self.assertEqual(row_a["market_trade_date"], "20260811")
        self.assertIn("tushare.rt_k", row_a["market_source"])
        with self.assertRaises(ValueError):
            self.repo_b.remove_watchlist_item(item_a["id"], removed_by="owner-b")

    def test_accounts_positions_snapshots_and_trades_are_private(self) -> None:
        account_payload = {
            "broker_code": "test",
            "broker_name": "测试证券",
            "account_name": "主账户",
            "account_no_masked": "****001",
            "account_type": "cash",
            "is_default": True,
        }
        account_a = self.repo_a.create_position_account(account_payload)
        account_b = self.repo_b.create_position_account(account_payload)
        self.assertNotEqual(account_a["id"], account_b["id"])

        self.repo_a.merge_position_holding(
            {
                "symbol": "600036.SH",
                "name": "招商银行",
                "account_id": account_a["id"],
                "position_quantity": 100,
                "cost_price": 35.0,
            },
            action="buy",
            source_type="manual",
        )
        self.repo_b.merge_position_holding(
            {
                "symbol": "600036.SH",
                "name": "招商银行",
                "account_id": account_b["id"],
                "position_quantity": 200,
                "cost_price": 34.0,
            },
            action="buy",
            source_type="manual",
        )

        self.assertEqual(self.repo_a.positions()[0]["position_quantity"], 100)
        self.assertEqual(self.repo_b.positions()[0]["position_quantity"], 200)
        self.assertEqual(len(self.repo_a.position_trades()), 1)
        self.assertEqual(len(self.repo_b.position_trades()), 1)
        with self.assertRaises(PositionNotFoundError):
            self.repo_b.update_account_funds(account_a["id"], 10000)
        foreign_delete = self.repo_b.delete_position_trade(self.repo_a.position_trades()[0]["id"])
        self.assertFalse(foreign_delete["deleted"])


if __name__ == "__main__":
    unittest.main()
