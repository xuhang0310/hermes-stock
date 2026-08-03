from __future__ import annotations

import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.core.database import Base
from backend.app.core.models import Instrument, PositionHoldingSnapshot, User
from backend.app.data.repository import StockRepository


class PositionCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = User(
            username="position-editor",
            display_name="Position Editor",
            role="researcher",
            permission_scope="dashboard:view,data:write",
            password_hash="test",
        )
        self.other_user = User(
            username="other-editor",
            display_name="Other Editor",
            role="researcher",
            permission_scope="dashboard:view,data:write",
            password_hash="test",
        )
        self.db.add_all(
            [
                self.user,
                self.other_user,
                Instrument(
                    symbol="600036.SH",
                    name="招商银行",
                    market="SSE",
                    type="stock",
                    status="active",
                ),
                Instrument(
                    symbol="601398.SH",
                    name="工商银行",
                    market="SSE",
                    type="stock",
                    status="active",
                ),
            ]
        )
        self.db.commit()
        self.repo = StockRepository(self.db, owner_user_id=self.user.id)
        self.other_repo = StockRepository(self.db, owner_user_id=self.other_user.id)
        self.account = self.repo.create_position_account(
            {
                "broker_code": "test",
                "broker_name": "测试证券",
                "account_name": "主账户",
                "account_no_masked": "****001",
                "account_type": "cash",
            }
        )

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_correct_symbol_and_latest_values_without_leaving_old_position(self) -> None:
        first = self.repo.save_position_holding(
            {
                "symbol": "600036.SH",
                "name": "招商银行",
                "account_id": self.account["id"],
                "as_of_date": "2026-07-22",
                "position_quantity": 100,
                "available_quantity": 100,
                "cost_price": 35,
                "latest_price": 36,
            }
        )
        self.repo.save_position_holding(
            {
                "symbol": "600036.SH",
                "name": "招商银行",
                "account_id": self.account["id"],
                "as_of_date": "2026-07-23",
                "position_quantity": 120,
                "available_quantity": 120,
                "cost_price": 34,
                "latest_price": 36,
            }
        )

        result = self.repo.correct_position_holding(
            first["id"],
            {
                "symbol": "601398.SH",
                "name": "工商银行",
                "position_quantity": 150,
                "cost_price": 6.25,
            },
        )

        self.assertEqual(result["symbol"], "601398.SH")
        self.assertEqual(result["name"], "工商银行")
        self.assertEqual(result["position_quantity"], 150)
        self.assertEqual(result["cost_price"], 6.25)
        positions = self.repo.positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["symbol"], "601398.SH")
        self.assertEqual(positions[0]["position_quantity"], 150)

        snapshots = self.db.scalars(
            select(PositionHoldingSnapshot).order_by(PositionHoldingSnapshot.as_of_date)
        ).all()
        self.assertEqual([item.symbol for item in snapshots], ["601398.SH", "601398.SH"])
        self.assertEqual([item.position_quantity for item in snapshots], [100, 150])
        self.assertEqual(snapshots[-1].available_quantity, 150)

    def test_correct_name_quantity_and_cost_for_same_symbol(self) -> None:
        saved = self.repo.save_position_holding(
            {
                "symbol": "600036.SH",
                "name": "招商银行",
                "account_id": self.account["id"],
                "position_quantity": 100,
                "available_quantity": 100,
                "cost_price": 35,
                "latest_price": 36,
            }
        )

        result = self.repo.correct_position_holding(
            saved["id"],
            {
                "symbol": "600036.SH",
                "name": "招商银行（纠正）",
                "position_quantity": 125,
                "cost_price": 34.5,
            },
        )

        self.assertEqual(result["name"], "招商银行（纠正）")
        self.assertEqual(result["position_quantity"], 125)
        self.assertEqual(result["cost_price"], 34.5)
        refreshed = self.repo.positions()[0]
        self.assertEqual(refreshed["name"], "招商银行（纠正）")
        self.assertEqual(refreshed["position_quantity"], 125)
        self.assertEqual(refreshed["cost_price"], 34.5)
        self.assertEqual(self.db.get(Instrument, "600036.SH").name, "招商银行")

    def test_position_payload_cannot_overwrite_instrument_master_name(self) -> None:
        self.repo.save_position_holding(
            {
                "symbol": "600036.SH",
                "name": "民德电子",
                "account_id": self.account["id"],
                "position_quantity": 100,
                "cost_price": 35,
            }
        )

        self.assertEqual(self.db.get(Instrument, "600036.SH").name, "招商银行")

    def test_correct_quantity_scales_day_pnl(self) -> None:
        saved = self.repo.save_position_holding(
            {
                "symbol": "600036.SH",
                "name": "招商银行",
                "account_id": self.account["id"],
                "position_quantity": 100,
                "available_quantity": 100,
                "cost_price": 35,
                "latest_price": 36,
                "day_pnl": 200,
            }
        )

        result = self.repo.correct_position_holding(
            saved["id"],
            {
                "symbol": "600036.SH",
                "name": "招商银行",
                "position_quantity": 150,
                "cost_price": 35,
            },
        )

        self.assertEqual(result["position_quantity"], 150)
        self.assertEqual(result["day_pnl"], 300.0)
        account = next(a for a in self.repo.list_position_accounts() if a["id"] == self.account["id"])
        self.assertEqual(account["day_pnl"], 300.0)

    def test_other_user_cannot_correct_position(self) -> None:
        saved = self.repo.save_position_holding(
            {
                "symbol": "600036.SH",
                "name": "招商银行",
                "account_id": self.account["id"],
                "position_quantity": 100,
                "cost_price": 35,
            }
        )
        with self.assertRaisesRegex(ValueError, "不属于当前用户"):
            self.other_repo.correct_position_holding(
                saved["id"],
                {
                    "symbol": "601398.SH",
                    "name": "工商银行",
                    "position_quantity": 100,
                    "cost_price": 6,
                },
            )


if __name__ == "__main__":
    unittest.main()
