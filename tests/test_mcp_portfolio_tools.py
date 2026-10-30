from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.core.models import (
    Base, Instrument, McpMutationReceipt, Position, PositionAccount,
    PositionHoldingSnapshot, PositionTrade, User,
)
from backend.app.data.repository import StockRepository
from backend.app.data.user_scope import current_data_owner_user_id, data_user_scope
from backend.app.mcp_gateway import portfolio_tools
from backend.app.mcp_gateway.mutations import run_idempotent_mutation


class McpPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("pymysql.connections.Connection.connect", side_effect=AssertionError("unexpected MySQL")))
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.enterContext(patch.object(portfolio_tools, "SessionLocal", self.sessions))
        self.lookup = self.enterContext(patch.object(portfolio_tools.stock_tools, "lookup_a_share_instrument"))
        self.lookup.return_value = {"available": True, "instrument": {"ts_code": "600036.SH", "name": "招商银行"}}
        with self.sessions() as db:
            users = [User(username=name, display_name=name, password_hash="test") for name in ("owner", "other")]
            db.add_all(users)
            db.add(Instrument(symbol="600036.SH", name="招商银行", market="SSE", type="stock"))
            db.flush()
            self.owner, self.other = [row.id for row in users]
            accounts = [PositionAccount(owner_user_id=row.id, broker_code="test", broker_name="测试券商",
                                        account_name="主账户", available_cash=10000) for row in users]
            db.add_all(accounts)
            db.commit()
            self.account, self.foreign_account = [row.id for row in accounts]

    def _add(self, **changes):
        args = {"user_id": self.owner, "account_id": self.account, "symbol": "600036.SH",
                "quantity": 100, "execution_price": 30, "idempotency_key": "buy-1"}
        args.update(changes)
        return portfolio_tools.add_position(**args)

    def _count(self, model):
        with self.sessions() as db:
            return db.scalar(select(func.count()).select_from(model))

    def test_add_accumulates_cost_and_cash_and_same_request_replays_once(self):
        first = self._add()
        self.assertFalse(first["broker_order_placed"])
        replay = self._add(symbol="600036")
        self.assertEqual(first, replay)
        self.assertEqual(self.lookup.call_count, 1)
        self.assertEqual(self._count(PositionTrade), 1)
        self.assertEqual(self._count(McpMutationReceipt), 1)
        result = self._add(quantity=100, execution_price=40, idempotency_key="buy-2")
        self.assertEqual(result["position"]["position_quantity"], 200)
        self.assertEqual(result["position"]["cost_price"], 35)
        with self.sessions() as db:
            self.assertEqual(db.get(PositionAccount, self.account).available_cash, 3000)
        self.assertEqual(self._count(PositionTrade), 2)
        self.assertEqual(self._count(PositionHoldingSnapshot), 2)

    def test_same_id_with_changed_amount_rejected_without_extra_write(self):
        self._add()
        with self.assertRaisesRegex(ValueError, "不同参数"):
            self._add(quantity=200)
        self.assertEqual(self._count(PositionTrade), 1)
        self.assertEqual(self._count(PositionHoldingSnapshot), 1)

    def test_receipt_keys_are_private_per_user(self):
        self._add()
        self._add(user_id=self.other, account_id=self.foreign_account)
        self.assertEqual(self._count(McpMutationReceipt), 2)
        self.assertEqual(len(portfolio_tools.get_positions(self.owner)["positions"]), 1)
        self.assertEqual(portfolio_tools.get_positions(self.other)["positions"][0]["account_id"], self.foreign_account)

    def test_account_isolation_for_all_entry_points(self):
        with self.assertRaisesRegex(ValueError, "不属于"):
            self._add(account_id=self.foreign_account)
        for read in (portfolio_tools.get_positions, portfolio_tools.analyze_portfolio):
            with self.subTest(read=read.__name__), self.assertRaisesRegex(ValueError, "不属于"):
                read(self.owner, self.foreign_account)
        with self.assertRaisesRegex(ValueError, "不属于"):
            portfolio_tools.analyze_position(self.owner, "600036.SH", self.foreign_account)
        result = portfolio_tools.list_position_accounts(self.owner)
        self.assertEqual([row["id"] for row in result["accounts"]], [self.account])
        self.assertEqual(self._count(McpMutationReceipt), 0)
        self.assertEqual(self._count(Position), 0)

    def test_missing_user_scope_is_rejected_before_database_access(self):
        for user_id in (None, 0, -1, True):
            with self.subTest(user_id=user_id), self.assertRaises(ValueError):
                portfolio_tools.get_positions(user_id)

    def test_finite_positive_numbers_and_valid_codes_required(self):
        for number in (float("nan"), float("inf"), float("-inf"), 0, -1, True):
            for key in ("quantity", "execution_price"):
                with self.subTest(number=number, key=key), self.assertRaises(ValueError):
                    self._add(**{key: number})
        with self.assertRaises(ValueError):
            self._add(symbol="招商银行")
        self.lookup.return_value = {"available": True, "instrument": None}
        with self.assertRaisesRegex(ValueError, "未匹配"):
            self._add()
        self.lookup.return_value = {"available": False, "instrument": None}
        with self.assertRaisesRegex(ValueError, "暂不可用"):
            self._add()
        self.assertEqual(self._count(Position), 0)
        self.assertEqual(self._count(McpMutationReceipt), 0)

    def test_insufficient_cash_rolls_back_everything(self):
        with self.assertRaisesRegex(ValueError, "可用资金不足"):
            self._add(quantity=1000)
        self.assertEqual(self._count(PositionTrade), 0)
        self.assertEqual(self._count(McpMutationReceipt), 0)
        with self.sessions() as db:
            self.assertEqual(db.get(PositionAccount, self.account).available_cash, 10000)

    def test_receipt_failure_rolls_back_bookkeeping_and_permits_retry(self):
        from sqlalchemy import event

        def fail_receipt(_mapper, _connection, _target):
            raise RuntimeError("receipt storage failure")

        event.listen(McpMutationReceipt, "before_insert", fail_receipt)
        try:
            with self.assertRaisesRegex(RuntimeError, "receipt storage"):
                self._add()
        finally:
            event.remove(McpMutationReceipt, "before_insert", fail_receipt)
        self.assertEqual(self._count(Position), 0)
        self.assertEqual(self._count(PositionTrade), 0)
        self.assertEqual(self._count(PositionHoldingSnapshot), 0)
        with self.sessions() as db:
            self.assertEqual(db.get(PositionAccount, self.account).available_cash, 10000)
        self.assertTrue(self._add()["recorded"])

    def test_reads_do_not_recompute_or_commit_account_balance(self):
        self._add()
        with self.sessions() as db:
            account = db.get(PositionAccount, self.account)
            account.total_asset = 123456
            db.commit()
        with patch.object(StockRepository, "_recompute_account_funds", side_effect=AssertionError("unexpected write")):
            portfolio_tools.list_position_accounts(self.owner)
            portfolio_tools.get_positions(self.owner)
            portfolio_tools.analyze_portfolio(self.owner)
        with self.sessions() as db:
            self.assertEqual(db.get(PositionAccount, self.account).total_asset, 123456)

    def test_portfolio_missing_prices_do_not_create_zero_weights(self):
        self._add()
        result = portfolio_tools.analyze_portfolio(self.owner)
        self.assertFalse(result["summary"]["valuation_complete"])
        self.assertEqual(result["summary"]["weights"], [])
        self.assertTrue(result["data_gaps"])
        self.assertIsNone(result["decision_guidance"]["action"])

    def test_analysis_scopes_owner_preserves_freshness_gaps_and_ambiguity(self):
        evidence = {"market": {"price": 35, "trade_date": "20261028", "stale": True,
                               "intraday_unavailable": True, "freshness_note": "行情陈旧"},
                    "account_ambiguous": True, "position_candidates": [{"account_id": 9}],
                    "technical": {"bar_count": 60},
                    "research": {"errors": [{"dataset": "cashflow", "error": "unavailable"}]}}
        original = copy.deepcopy(evidence)

        def context(*_args, **_kwargs):
            self.assertEqual(current_data_owner_user_id(), self.owner)
            return evidence

        with data_user_scope(self.other), patch.object(portfolio_tools.digital_human_tools, "get_position_decision_context", side_effect=context):
            result = portfolio_tools.analyze_position(self.owner, "600036.SH")
            self.assertEqual(current_data_owner_user_id(), self.other)
        self.assertEqual(evidence, original)
        self.assertEqual(result["market"], original["market"])
        self.assertEqual(result["data_gaps"], original["research"]["errors"])
        self.assertEqual(result["decision_guidance"]["status"], "needs_data")
        self.assertIsNone(result["decision_guidance"]["action"])
        self.assertTrue(result["account_ambiguous"])

    def test_analysis_missing_price_or_position_remains_incomplete(self):
        with patch.object(portfolio_tools.digital_human_tools, "get_position_decision_context", return_value={"error": "无行情数据"}):
            result = portfolio_tools.analyze_position(self.owner, "600036.SH")
        self.assertEqual(result["decision_guidance"]["status"], "needs_data")
        self.assertIn("无行情数据", result["decision_guidance"]["blockers"])

    def test_history_fallback_without_expected_day_cannot_claim_current_price(self):
        evidence = {"market": {"price": 35, "trade_date": "20261028", "stale": False,
                               "expected_trade_date": None},
                    "position": {"quantity": 100, "cost_price": 30},
                    "technical": {"bar_count": 60}}
        with patch.object(portfolio_tools.digital_human_tools, "get_position_decision_context", return_value=evidence):
            result = portfolio_tools.analyze_position(self.owner, "600036.SH")
        self.assertEqual(result["decision_guidance"]["status"], "needs_data")
        self.assertTrue(any("新鲜度" in item for item in result["decision_guidance"]["blockers"]))
        self.assertEqual(result["market"], evidence["market"])

    def test_non_positive_price_is_blocked_and_technical_gaps_are_reported(self):
        for price, technical in ((0, {}), (-1, {"error": "technical calculation failed"})):
            evidence = {"market": {"price": price, "trade_date": "20261030", "expected_trade_date": "20261030"},
                        "position": {"quantity": 100, "cost_price": 30}, "technical": technical}
            with self.subTest(price=price), patch.object(portfolio_tools.digital_human_tools, "get_position_decision_context", return_value=evidence):
                result = portfolio_tools.analyze_position(self.owner, "600036.SH")
            self.assertIn("缺少可核对日期的有效价格", result["decision_guidance"]["blockers"])
            gap = next(item for item in result["data_gaps"] if item["dataset"] == "technical")
            self.assertTrue(gap["error"])

    def test_position_plans_include_owned_risk_fields_without_choosing_multiple_accounts(self):
        with self.sessions() as db:
            second = PositionAccount(owner_user_id=self.owner, broker_code="test", broker_name="第二券商",
                                     account_name="第二账户", available_cash=10000)
            db.add(second)
            db.commit()
            second_id = second.id
            for uid, account_id, thesis in ((self.owner, self.account, "我的主账户逻辑"),
                                            (self.owner, second_id, "我的第二账户逻辑"),
                                            (self.other, self.foreign_account, "其他用户的私密逻辑")):
                StockRepository(db).for_user(uid).save_position_holding({
                    "account_id": account_id, "symbol": "600036.SH", "position_quantity": 100,
                    "cost_price": 30, "stop_price": 28, "take_profit_price": 40,
                    "thesis": thesis, "risk_note": "关注现金流", "as_of_date": "2026-10-30",
                })
        evidence = {"market": {"price": 35, "trade_date": "20261030", "expected_trade_date": "20261030"},
                    "position": {"quantity": 100, "cost_price": 30}, "technical": {"bar_count": 60}}
        with patch.object(portfolio_tools.digital_human_tools, "get_position_decision_context", return_value=evidence):
            result = portfolio_tools.analyze_position(self.owner, "600036.SH")
            selected = portfolio_tools.analyze_position(self.owner, "600036.SH", self.account)
        self.assertIsNone(result["position_plan"])
        self.assertTrue(result["account_ambiguous"])
        self.assertEqual({row["account_id"] for row in result["position_plans"]}, {self.account, second_id})
        self.assertNotIn("其他用户的私密逻辑", str(result))
        plan = selected["position_plan"]
        self.assertEqual(plan["stop_price"], 28)
        self.assertEqual(plan["take_profit_price"], 40)
        self.assertEqual(plan["thesis"], "我的主账户逻辑")
        self.assertEqual(plan["risk_note"], "关注现金流")
        self.assertEqual(plan["as_of_date"], "2026-10-30")
        self.assertFalse(selected["account_ambiguous"])

    def test_mutation_helper_rejects_disabled_user_and_invalid_keys(self):
        with self.sessions() as db:
            for key in ("", " ", " x", "x" * 81):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    run_idempotent_mutation(db, self.owner, "test", key, {}, lambda: {"ok": True})
            user = db.get(User, self.owner)
            user.is_enabled = False
            db.commit()
            with self.assertRaisesRegex(ValueError, "已停用"):
                run_idempotent_mutation(db, self.owner, "test", "ok", {}, lambda: {"ok": True})


if __name__ == "__main__":
    unittest.main()
