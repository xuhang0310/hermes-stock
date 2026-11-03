"""持仓同步的识别逻辑与 SQLite 写入、撤销、隔离测试，不连接线上数据库或模型。"""
from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from backend.app.data import position_sync
from backend.app.core.database import Base
from backend.app.core.models import Instrument, PositionHoldingSnapshot, PositionImportBatch, PositionTrade, User
from backend.app.data.errors import PositionNotFoundError
from backend.app.data.position_sync_mixin import PositionSyncMixin
from backend.app.data.repository import StockRepository

ACCOUNTS = [
    {"id": 3, "broker_code": "fangzheng", "broker_name": "方正证券", "account_no_masked": None, "status": "active"},
    {"id": 4, "broker_code": "gf", "broker_name": "广发证券", "account_no_masked": "8800****1642", "status": "active"},
    {"id": 5, "broker_code": "citic", "broker_name": "中信证券", "account_no_masked": None, "status": "active"},
]


def _valid_rows(rows, prefer_name_on_conflict=False):
    """桩：名称“同力天启”的行纠正为 605286.SH，代码为空的行判无效，其余原样通过。"""
    out, validations = [], []
    for index, row in enumerate(rows):
        row = dict(row)
        if row.get("name") == "同力天启":
            row["symbol"] = "605286.SH"
            validations.append({"index": index, "valid": True, "status": "symbol_corrected_from_name_conflict", "message": "已按名称校正"})
        elif not row.get("symbol"):
            validations.append({"index": index, "valid": False, "status": "invalid_format", "input_symbol": "", "message": "代码格式不对"})
        else:
            validations.append({"index": index, "valid": True, "status": "valid", "message": "代码校验通过"})
        out.append(row)
    return out, validations


class AccountDetectionTests(unittest.TestCase):
    def test_account_number_tail_wins(self) -> None:
        account_id, reason = position_sync.detect_account(ACCOUNTS, [{"account_no_masked": "88****642"}])
        self.assertEqual(account_id, 4)
        self.assertIn("642", reason)

    def test_broker_app_name_maps_to_account(self) -> None:
        self.assertEqual(position_sync.detect_account(ACCOUNTS, [{"app_name": "小方"}])[0], 3)
        self.assertEqual(position_sync.detect_account(ACCOUNTS, [{"broker_name": "广发证券易淘金"}])[0], 4)

    def test_longer_broker_names_match_first(self) -> None:
        self.assertEqual(position_sync.broker_code_from_text("中信建投证券"), "csc")
        self.assertEqual(position_sync.broker_code_from_text("中信证券 信e投"), "citic")

    def test_ambiguous_or_unknown_returns_none(self) -> None:
        two_fangzheng = [*ACCOUNTS, {"id": 9, "broker_code": "fangzheng", "broker_name": "方正证券", "status": "active"}]
        self.assertEqual(position_sync.detect_account(two_fangzheng, [{"broker_name": "方正证券"}]), (None, ""))
        self.assertEqual(position_sync.detect_account(ACCOUNTS, [{"broker_name": "某某证券"}]), (None, ""))


class MergeTests(unittest.TestCase):
    def test_merges_overlapping_screens_and_keeps_first_funds(self) -> None:
        extractions = [
            {
                "account": {"app_name": "小方", "account_no_masked": "28****642"},
                "funds": {"available_cash": "538,969.15", "total_asset": 1265295.75},
                "rows": [
                    {"symbol": "300143", "name": "盈康生命", "position_quantity": 12940, "cost_price": 8.4},
                    {"symbol": "002642", "name": "同力天启", "position_quantity": 7200},
                ],
            },
            {
                "funds": {"available_cash": 1, "holding_market_value": 726326.6},
                "rows": [
                    {"symbol": "300143", "name": "盈康生命", "position_quantity": 12940, "latest_price": 7.1},
                    {"symbol": "", "name": "看不清", "position_quantity": 100},
                ],
            },
        ]
        with patch("backend.app.agents.position_agent.validate_position_rows", side_effect=_valid_rows):
            merged = position_sync.merge_extractions(extractions)
        by_key = {row["key"]: row for row in merged["rows"]}
        self.assertEqual(set(by_key), {"300143", "605286.SH", "name:看不清"})
        self.assertEqual(by_key["300143"]["latest_price"], 7.1)  # 第二张补上第一张缺的字段
        self.assertTrue(by_key["605286.SH"]["corrected"])
        self.assertFalse(by_key["name:看不清"]["valid"])
        self.assertEqual(merged["funds"], {"available_cash": 538969.15, "total_asset": 1265295.75, "holding_market_value": 726326.6})
        self.assertEqual(merged["account_no_masked"], "28****642")
        self.assertEqual(merged["warnings"], [])

    def test_conflicting_quantities_warn(self) -> None:
        extractions = [
            {"rows": [{"symbol": "300143", "name": "盈康生命", "position_quantity": 12940}]},
            {"rows": [{"symbol": "300143", "name": "盈康生命", "position_quantity": 12000}]},
        ]
        with patch("backend.app.agents.position_agent.validate_position_rows", side_effect=_valid_rows):
            merged = position_sync.merge_extractions(extractions)
        self.assertEqual(merged["rows"][0]["position_quantity"], 12940)
        self.assertEqual(len(merged["warnings"]), 1)


class PreviewTests(unittest.TestCase):
    def _repo(self, current):
        account = SimpleNamespace(
            id=3, broker_name="方正证券", account_name=None, available_cash=500000.0,
            total_asset=1200000.0, holding_market_value=700000.0, fund_updated_at=None,
        )
        return SimpleNamespace(_owned_account=lambda _id: account, positions=lambda account_id: current)

    def _row(self, symbol, quantity, **extra):
        return {"key": symbol, "symbol": symbol, "name": symbol, "valid": True, "corrected": False, "position_quantity": quantity, **extra}

    def test_classifies_changes_against_current_holdings(self) -> None:
        current = [
            {"symbol": "300143.SZ", "name": "盈康生命", "quantity": 12940, "cost_price": 8.4, "latest_price": 7.0, "market_value": 90580},
            {"symbol": "605286.SH", "name": "同力天启", "quantity": 7200, "cost_price": 31.74, "latest_price": 22.0, "market_value": 158400},
            {"symbol": "002642.SZ", "name": "荣联科技", "quantity": 9600, "cost_price": 33.28, "latest_price": 7.0, "market_value": 67200},
            {"symbol": "603566.SH", "name": "普莱柯", "quantity": 8300, "cost_price": 20.0, "latest_price": 15.0, "market_value": 124500},
        ]
        rows = [
            self._row("300143.SZ", 12940, cost_price=8.4),
            self._row("605286.SH", 7000, cost_price=31.74, latest_price=24.15),
            self._row("003011.SZ", 6600, cost_price=18.2),
            self._row("603566.SH", 0),
            {"key": "name:看不清", "symbol": "", "name": "看不清", "valid": False, "position_quantity": 100, "validation_message": "代码格式不对"},
        ]
        preview = PositionSyncMixin.position_sync_preview(self._repo(current), account_id=3, rows=rows, funds={})
        kinds = {item["key"]: (item["kind"], item["apply"]) for item in preview["items"]}
        self.assertEqual(kinds["300143.SZ"], ("unchanged", False))
        self.assertEqual(kinds["605286.SH"], ("changed", True))
        self.assertEqual(kinds["003011.SZ"], ("new", True))
        self.assertEqual(kinds["002642.SZ"], ("missing", True))
        self.assertEqual(kinds["603566.SH"], ("missing", True))  # 截图上数量为 0 视同清仓
        self.assertEqual(kinds["name:看不清"], ("invalid", False))
        changed = next(item for item in preview["items"] if item["key"] == "605286.SH")
        self.assertEqual(changed["trade_hint"], {"action": "sell", "quantity": 200, "price": 24.15})
        self.assertEqual(preview["counts"]["missing"], 2)

    def test_incomplete_screenshot_does_not_close_missing_by_default(self) -> None:
        current = [
            {"symbol": "300143.SZ", "name": "盈康生命", "quantity": 100, "cost_price": 8.0, "latest_price": 8.0, "market_value": 800},
            {"symbol": "605286.SH", "name": "同力天启", "quantity": 100, "cost_price": 30.0, "latest_price": 30.0, "market_value": 3000},
        ]
        rows = [self._row("300143.SZ", 100, cost_price=8.0, market_value=800)]
        preview = PositionSyncMixin.position_sync_preview(
            self._repo(current), account_id=3, rows=rows, funds={"holding_market_value": 3800, "available_cash": 1000}
        )
        missing = next(item for item in preview["items"] if item["kind"] == "missing")
        self.assertFalse(missing["apply"])
        self.assertFalse(preview["completeness"]["complete"])
        self.assertEqual(len(preview["warnings"]), 1)
        self.assertTrue(preview["apply_funds_default"])


class ApplyValidationTests(unittest.TestCase):
    def _validate(self, symbol, name=None, **_kwargs):
        if symbol in {"300143.SZ", "605286.SH"}:
            return {"valid": True, "normalized_symbol": symbol, "canonical_name": "x"}
        return {"valid": False, "message": "未找到"}

    def test_skips_unchecked_and_requires_trade_price(self) -> None:
        items = [
            {"kind": "changed", "symbol": "300143.SZ", "name": "盈康生命", "apply": False, "after": {"position_quantity": 1}},
            {"kind": "missing", "symbol": "605286.SH", "name": "同力天启", "apply": True,
             "trade": {"enabled": True, "action": "sell", "quantity": 7200, "price": 24.15}},
        ]
        with patch("backend.app.agents.position_agent.validate_position_symbol", side_effect=self._validate):
            cleaned = position_sync.validate_apply_items(items)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["trade"], {"action": "sell", "quantity": 7200.0, "price": 24.15})
        items[1]["trade"]["price"] = None
        with patch("backend.app.agents.position_agent.validate_position_symbol", side_effect=self._validate):
            with self.assertRaises(ValueError):
                position_sync.validate_apply_items(items)

    def test_rejects_invalid_symbol(self) -> None:
        items = [{"kind": "new", "symbol": "999999", "name": "看不清", "apply": True, "after": {"position_quantity": 100}}]
        with patch("backend.app.agents.position_agent.validate_position_symbol", side_effect=self._validate):
            with self.assertRaises(ValueError):
                position_sync.validate_apply_items(items)


class PositionSyncDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        owner = User(username="sync-owner", display_name="Owner", role="researcher", password_hash="test")
        other = User(username="sync-other", display_name="Other", role="researcher", password_hash="test")
        self.db.add_all([
            owner, other,
            Instrument(symbol="600036.SH", name="招商银行", market="SSE", type="stock", status="active"),
            Instrument(symbol="601398.SH", name="工商银行", market="SSE", type="stock", status="active"),
            Instrument(symbol="600000.SH", name="浦发银行", market="SSE", type="stock", status="active"),
        ])
        self.db.commit()
        self.repo = StockRepository(self.db, owner_user_id=owner.id)
        self.other_repo = StockRepository(self.db, owner_user_id=other.id)
        self.account_id = self.repo.create_position_account({"broker_code": "test", "broker_name": "测试证券"})["id"]
        self.today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        self.repo.save_position_holding({
            "account_id": self.account_id, "symbol": "600036.SH", "position_quantity": 100,
            "cost_price": 35, "latest_price": 36, "as_of_date": self.today,
            "stop_price": 30, "take_profit_price": 45, "thesis": "保留持仓理由",
        })
        self.repo.save_position_holding({
            "account_id": self.account_id, "symbol": "601398.SH", "position_quantity": 50,
            "cost_price": 5, "latest_price": 6, "as_of_date": self.today,
        })
        self.repo.update_account_funds(self.account_id, 10000)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _sync(self, items=None, *, funds=True):
        return self.repo.apply_position_sync(
            account_id=self.account_id,
            items=items if items is not None else [{
                "kind": "changed", "symbol": "600036.SH", "name": "招商银行",
                "after": {"position_quantity": 200, "cost_price": 35, "latest_price": 36},
            }],
            funds={"available_cash": 9000} if funds else None,
            as_of_date=self.today, image_sha256=None, account_no_masked=None, created_by="test",
        )

    def _positions(self):
        return {row["symbol"]: row for row in self.repo.positions(account_id=self.account_id)}

    def test_sync_and_undo_restore_added_changed_closed_positions_and_funds(self) -> None:
        result = self._sync([
            {"kind": "changed", "symbol": "600036.SH", "name": "招商银行",
             "after": {"position_quantity": 200, "cost_price": 35, "latest_price": 36}},
            {"kind": "missing", "symbol": "601398.SH", "name": "工商银行"},
            {"kind": "new", "symbol": "600000.SH", "name": "浦发银行",
             "after": {"position_quantity": 300, "cost_price": 10, "latest_price": 11}},
        ])
        self.assertEqual(result["counts"], {"added": 1, "updated": 1, "closed": 1, "trades": 0})
        self.assertEqual(self._positions()["600036.SH"]["quantity"], 200)
        latest = self.repo._latest_open_snapshot("600036.SH", self.account_id)
        self.assertEqual((latest.stop_price, latest.take_profit_price, latest.thesis), (30, 45, "保留持仓理由"))
        self.assertEqual(self.db.scalar(select(func.count(PositionTrade.id))), 0)
        undone = self.repo.undo_position_sync(result["batch_id"])
        self.assertEqual({symbol: row["quantity"] for symbol, row in self._positions().items()},
                         {"600036.SH": 100, "601398.SH": 50})
        self.assertEqual(undone["account"]["available_cash"], 10000)
        self.assertEqual(undone["account"]["total_asset"], 13900)
        self.assertEqual(self.db.get(PositionImportBatch, result["batch_id"]).status, "rolled_back")

    def test_undo_after_price_refresh_restores_quantity_and_preserves_unrelated_quotes(self) -> None:
        result = self._sync()
        for symbol, quantity, cost, price in (("600036.SH", 200, 35, 40), ("601398.SH", 50, 5, 7)):
            self.repo._save_position_holding({
                "account_id": self.account_id, "symbol": symbol, "position_quantity": quantity,
                "cost_price": cost, "latest_price": price, "as_of_date": self.today,
            }, source_type="price_sync", commit=True)
        undone = self.repo.undo_position_sync(result["batch_id"])
        positions = self._positions()
        self.assertEqual(positions["600036.SH"]["quantity"], 100)
        self.assertEqual(positions["601398.SH"]["quantity"], 50)
        self.assertEqual(positions["601398.SH"]["latest_price"], 7)
        self.assertEqual(undone["account"]["available_cash"], 10000)
        self.assertEqual(self.db.scalar(select(func.count(PositionHoldingSnapshot.id)).where(
            PositionHoldingSnapshot.source_type == "price_sync")), 1)

    def test_manual_fund_change_blocks_undo_without_changing_holdings_or_cash(self) -> None:
        result = self._sync()
        self.repo.update_account_funds(self.account_id, 11111)
        with self.assertRaisesRegex(ValueError, "可用资金又有新的改动"):
            self.repo.undo_position_sync(result["batch_id"])
        self.assertEqual(self._positions()["600036.SH"]["quantity"], 200)
        account = next(row for row in self.repo.list_position_accounts() if row["id"] == self.account_id)
        self.assertEqual(account["available_cash"], 11111)
        self.assertEqual(self.db.get(PositionImportBatch, result["batch_id"]).status, "done")

    def test_explicit_trade_is_settled_once_and_removed_on_undo(self) -> None:
        result = self._sync([{
            "kind": "changed", "symbol": "600036.SH", "name": "招商银行",
            "after": {"position_quantity": 50, "cost_price": 35, "latest_price": 37},
            "trade": {"action": "sell", "quantity": 50, "price": 37},
        }], funds=False)
        self.assertEqual(result["counts"]["trades"], 1)
        self.assertEqual(result["account"]["available_cash"], 11850)
        trade = self.db.scalar(select(PositionTrade))
        self.assertEqual((trade.action, trade.quantity, trade.price), ("sell", 50, 37))
        undone = self.repo.undo_position_sync(result["batch_id"])
        self.assertEqual(undone["account"]["available_cash"], 10000)
        self.assertEqual(self._positions()["600036.SH"]["quantity"], 100)
        self.assertEqual(self.db.scalar(select(func.count(PositionTrade.id))), 0)

    def test_other_user_cannot_preview_apply_or_undo_this_account(self) -> None:
        result = self._sync()
        with self.assertRaises(PositionNotFoundError):
            self.other_repo.position_sync_preview(account_id=self.account_id, rows=[], funds=None)
        with self.assertRaises(PositionNotFoundError):
            self.other_repo.apply_position_sync(
                account_id=self.account_id, items=[], funds={"available_cash": 0}, as_of_date=self.today,
                image_sha256=None, account_no_masked=None, created_by="test",
            )
        with self.assertRaises(PositionNotFoundError):
            self.other_repo.undo_position_sync(result["batch_id"])
        self.assertEqual(self.db.scalar(select(func.count(PositionImportBatch.id))), 1)
        self.assertEqual(self._positions()["600036.SH"]["quantity"], 200)


if __name__ == "__main__":
    unittest.main()
