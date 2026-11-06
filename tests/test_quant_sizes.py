"""量化股数：建议怎么算、改了要写理由、补存不退回、页面和提示。内存 SQLite，不连 MySQL。"""
from __future__ import annotations

import dataclasses
import math
import unittest
from datetime import datetime
from unittest.mock import patch

import numpy as np
from sqlalchemy import select

from backend.app.core.models import DailyBar, MarketEvent, PositionHoldingSnapshot, ReportShare, StockModelSignal, TradePlanItem
from backend.app.core.settings import settings as real_settings
from backend.app.events.rules import CRITICAL
from backend.app.plans import quant_orders, quant_sizes, rules
from backend.app.plans.page import render_plan_page
from backend.app.plans.service import to_utc
from backend.app.quant.order_engine import Level, OrderLevels
from backend.app.quant.order_table_build import CELLS
from tests.test_trade_plans import ACCOUNT, BUY_SYMBOL, MORNING, OWNER, SELL_SYMBOL, _PlanCase

ETF = "159650.SZ"
TABLE = {
    "sell": {cell: {"1.0": {"fill": 0.1, "edge": -0.01, "raw": 0, "n": 1000}} for cell in CELLS},
    "buy": {},
    "stop": {cell: {"3.0": {"fill": 0.01, "edge": 0.0, "raw": 0, "n": 1000}} for cell in CELLS},
}


def _levels(close: float = 20.0, atr: float = 1.0, sell: float | None = 21.0) -> OrderLevels:
    return OrderLevels(close=close, atr=atr, trend="range", vol_bucket=1, sell=Level(sell, 1.0, 0.1, -0.01, 0.001) if sell else None, buy=None, stop=None)


class AdviseTests(unittest.TestCase):
    def test_above_target_sells_the_excess(self) -> None:
        target, advised, why = quant_sizes.advise(SELL_SYMBOL, 7300, _levels(), 1_000_000, None, None, 0.1)
        self.assertAlmostEqual(target, 5000)
        self.assertEqual((advised, why), (2300, ""))

    def test_the_rank_moves_the_target(self) -> None:
        self.assertEqual(quant_sizes.advise(SELL_SYMBOL, 7300, _levels(), 1_000_000, 1.0, None, 0.1)[1], 0)  # 目标 7500
        self.assertEqual(quant_sizes.advise(SELL_SYMBOL, 7300, _levels(), 1_000_000, 0.0, None, 0.1)[1], 4800)  # 目标 2500

    def test_no_sell_edge_means_no_order_and_says_why(self) -> None:
        _, advised, why = quant_sizes.advise(SELL_SYMBOL, 7300, _levels(sell=None), 1_000_000, None, None, 0.1)
        self.assertEqual(advised, 0)
        self.assertIn("没有优势", why)

    def test_near_the_target_stays(self) -> None:
        _, advised, why = quant_sizes.advise(SELL_SYMBOL, 5100, _levels(), 1_000_000, None, 1.0, 0.1)
        self.assertEqual((advised, why), (0, "在目标附近，不动"))

    def test_below_target_can_top_up_when_the_model_allows(self) -> None:
        self.assertEqual(quant_sizes.advise(SELL_SYMBOL, 4000, _levels(), 1_000_000, None, 1.0, 0.1)[1], -1000)

    def test_below_target_stays_when_the_model_says_no(self) -> None:
        _, advised, why = quant_sizes.advise(SELL_SYMBOL, 4000, _levels(), 1_000_000, None, None, 0.1)
        self.assertEqual(advised, 0)
        self.assertIn("买入模型", why)

    def test_the_same_numbers_as_shadow_b(self) -> None:
        from backend.app.shadow import rules as shadow_rules

        order = shadow_rules.plan_order(SELL_SYMBOL, 7300, _levels(), 1_000_000, math.inf, tilt=shadow_rules.rank_tilt(0.2))
        self.assertEqual(quant_sizes.advise(SELL_SYMBOL, 7300, _levels(), 1_000_000, 0.2, None, 0.1)[1], order.quantity)


class BudgetTests(unittest.TestCase):
    def test_eight_stocks_or_more_use_the_validated_budget(self) -> None:
        self.assertEqual(quant_sizes.budget_for(8), (0.01, 0.25))
        self.assertEqual(quant_sizes.budget_for(20), (0.01, 0.25))

    def test_fewer_stocks_loosen_the_budget(self) -> None:
        self.assertEqual(quant_sizes.budget_for(4), (0.02, 0.25))
        self.assertEqual(quant_sizes.budget_for(2), (0.04, 0.5))
        self.assertEqual(quant_sizes.budget_for(1), (0.08, 1.0))
        self.assertEqual(quant_sizes.budget_for(0), (0.08, 1.0))

    def test_four_stocks_are_not_cut_in_half(self) -> None:
        # 占 25%、ATR 5%：8 只的规则要砍到 10%，4 只放宽后是 20%
        levels = _levels(close=20.0, atr=1.0)
        self.assertEqual(quant_sizes.advise(SELL_SYMBOL, 12500, levels, 1_000_000, None, None, 0.1, 8)[1], 7500)
        self.assertEqual(quant_sizes.advise(SELL_SYMBOL, 12500, levels, 1_000_000, None, None, 0.1, 4)[1], 2500)

    def test_the_shadow_rule_is_not_touched(self) -> None:
        from backend.app.shadow import rules as shadow_rules

        self.assertAlmostEqual(shadow_rules.target_shares(1_000_000, 20, 1.0), 5000)
        self.assertEqual(shadow_rules.plan_order(SELL_SYMBOL, 12500, _levels(), 1_000_000, 0).quantity, 7500)


class DeviatesTests(unittest.TestCase):
    def test_a_sell_close_to_the_advice_is_fine(self) -> None:
        self.assertFalse(quant_sizes.deviates(900, 900, 0))
        self.assertFalse(quant_sizes.deviates(900, 1000, 0))
        self.assertFalse(quant_sizes.deviates(2000, 1800, 0))

    def test_selling_much_more_or_less_is_a_change(self) -> None:
        self.assertTrue(quant_sizes.deviates(900, 1100, 0))
        self.assertTrue(quant_sizes.deviates(5400, 2000, 0))

    def test_not_selling_when_told_to_is_a_change(self) -> None:
        self.assertTrue(quant_sizes.deviates(900, 0, 0))

    def test_selling_or_buying_when_told_to_stay_is_a_change(self) -> None:
        self.assertTrue(quant_sizes.deviates(0, 500, 0))
        self.assertTrue(quant_sizes.deviates(0, 0, 100))
        self.assertFalse(quant_sizes.deviates(0, 0, 0))

    def test_topping_up_less_is_fine_and_more_is_a_change(self) -> None:
        self.assertFalse(quant_sizes.deviates(-1000, 0, 0))
        self.assertFalse(quant_sizes.deviates(-1000, 0, 500))
        self.assertTrue(quant_sizes.deviates(-1000, 0, 2000))

    def test_selling_when_it_could_top_up_is_a_change(self) -> None:
        self.assertTrue(quant_sizes.deviates(-1000, 500, 0))


class _SizeCase(_PlanCase):
    """账户里同力天启 7200 股，再加一只等市值的兴业银行：2 只，风险预算放宽到 4%，投入资金约 31 万，同力目标约 6,200 股，要卖约 900 股。"""

    def setUp(self) -> None:
        super().setUp()
        quant_orders._table_cache.clear()
        quant_orders._table_cache["table"] = TABLE
        self.addCleanup(quant_orders._table_cache.clear)
        for module, flags in ((quant_orders, {"quant_orders_enabled": True}), (quant_sizes, {"quant_sizes_enabled": True})):
            item = patch.object(module, "settings", dataclasses.replace(real_settings, **flags))
            item.start()
            self.addCleanup(item.stop)
        with self.Session() as db:
            for day in np.arange(np.datetime64("2026-07-01"), np.datetime64("2026-11-03")):
                stamp = str(day).replace("-", "")
                db.add(DailyBar(symbol=SELL_SYMBOL, trade_date=stamp, open=21.57, high=22.07, low=21.07, close=21.57, pre_close=21.57, source="tushare"))
                db.add(DailyBar(symbol=BUY_SYMBOL, trade_date=stamp, open=19.93, high=20.13, low=19.73, close=19.93, pre_close=19.93, source="tushare"))
            db.add(self._snapshot(BUY_SYMBOL, 7800))
            db.commit()

    def _advice(self) -> dict:
        with self.Session() as db:
            return quant_sizes.size_advice(db, OWNER, "2026-11-03", MORNING)

    def _advised(self, symbol: str = SELL_SYMBOL) -> int:
        return self._advice()[(ACCOUNT, symbol)].advised

    def _hold(self, symbol: str, **extra) -> dict:
        return {"account_id": ACCOUNT, "symbol": symbol, "action": "hold", **extra}

    def _plan(self, *items: dict) -> dict:
        return self._save(list(items))

    def _both(self, sell: dict) -> dict:
        """卖同力加兴业那一条。兴业的建议由测试算好，这里按建议存。"""
        buy_side = self._advised(BUY_SYMBOL)
        if buy_side > 0:
            other = self._sell(symbol=BUY_SYMBOL, trigger_price=20.33, quantity=buy_side)
        else:
            other = self._hold(BUY_SYMBOL)
        return self._plan(sell, other)

    def _item(self, symbol: str = SELL_SYMBOL) -> TradePlanItem:
        with self.Session() as db:
            return db.scalar(select(TradePlanItem).where(TradePlanItem.symbol == symbol))


class PlanCheckTests(_SizeCase):
    def test_the_advice_uses_only_stocks_with_bars(self) -> None:
        with self.Session() as db:
            db.add(self._snapshot(ETF, 5000))
            db.commit()
        advice = self._advice()
        self.assertNotIn((ACCOUNT, ETF), advice)
        invested = 7200 * 21.57 + 7800 * 19.93
        self.assertAlmostEqual(advice[(ACCOUNT, SELL_SYMBOL)].target, min(0.04 * invested / 2, 0.5 * invested / 21.57), delta=120)

    def test_following_the_advice_saves_and_records_it(self) -> None:
        advised = self._advised()
        self.assertGreater(advised, 0)
        result = self._both(self._sell(trigger_price=22.57, quantity=advised))
        self.assertTrue(result["saved"], result)
        item = self._item()
        self.assertEqual(item.advised_quantity, advised)
        self.assertIsNone(item.size_note)

    def test_changing_the_number_without_a_reason_is_refused(self) -> None:
        result = self._both(self._sell(trigger_price=22.57, quantity=3000))
        self.assertFalse(result["saved"])
        text = "\n".join(result["issues"])
        self.assertIn(f"代码建议卖 {self._advised():,} 股".replace(",", ""), text.replace(",", ""))
        self.assertIn("size_note", text)

    def test_changing_the_number_with_a_reason_saves(self) -> None:
        result = self._both(self._sell(trigger_price=22.57, quantity=3000, size_note="半年报前不想一次卖太多"))
        self.assertTrue(result["saved"], result)
        self.assertEqual(self._item().size_note, "半年报前不想一次卖太多")

    def test_holding_when_told_to_sell_needs_a_reason(self) -> None:
        self.assertFalse(self._both(self._hold(SELL_SYMBOL))["saved"])
        self.assertTrue(self._both(self._hold(SELL_SYMBOL, size_note="等公告"))["saved"])

    def test_leaving_the_stock_out_is_refused_with_a_way_forward(self) -> None:
        other = self._hold(BUY_SYMBOL) if self._advised(BUY_SYMBOL) <= 0 else self._sell(symbol=BUY_SYMBOL, trigger_price=20.33, quantity=self._advised(BUY_SYMBOL))
        result = self._plan(other)
        self.assertFalse(result["saved"])
        self.assertIn("存一条 action 为 hold", "\n".join(result["issues"]))

    def test_a_reason_on_a_matching_item_is_dropped(self) -> None:
        self._both(self._sell(trigger_price=22.57, quantity=self._advised(), size_note="多余的话"))
        self.assertIsNone(self._item().size_note)

    def test_the_repair_save_records_instead_of_refusing(self) -> None:
        with self.Session() as db:
            from backend.app.plans.service import save_trade_plan

            result = save_trade_plan(
                db,
                owner_user_id=OWNER,
                source="premarket",
                job_key="agent-1-x",
                job_run_id="job-1",
                run_id="run-1",
                digital_human_code="stock-pm",
                push_channel_key="feishu-daily",
                raw_items=[self._sell(trigger_price=22.57, quantity=3000), self._hold(BUY_SYMBOL)],
                now=MORNING,
                is_open=lambda _day: True,
                plan_repair=True,
            )
        self.assertTrue(result["saved"], result)
        self.assertEqual(self._item().size_note, quant_sizes.NO_REASON)

    def test_a_critical_event_lifts_the_check(self) -> None:
        with self.Session() as db:
            db.add(
                MarketEvent(
                    owner_user_id=OWNER, symbol=SELL_SYMBOL, stock_name="同力天启", source="announcement", title="收到立案告知书",
                    content_hash="h", event_type="立案调查", severity=CRITICAL, status="pushed", created_at=to_utc(MORNING),
                )
            )
            db.commit()
        self.assertNotIn((ACCOUNT, SELL_SYMBOL), self._advice())

    def test_other_problems_are_reported_first(self) -> None:
        result = self._plan(self._sell(trigger_price=1.0, quantity=1000))
        text = "\n".join(result["issues"])
        self.assertIn("卖价", text)
        self.assertNotIn("size_note", text)

    def test_plans_that_are_not_for_the_next_session_are_not_checked(self) -> None:
        result = self._save([self._sell(trigger_price=22.57, quantity=1000)], source="weekly")
        self.assertTrue(result["saved"], result)
        self.assertIsNone(self._item().advised_quantity)

    def test_the_switch_turns_it_off(self) -> None:
        with patch.object(quant_sizes, "settings", dataclasses.replace(real_settings, quant_sizes_enabled=False)):
            self.assertEqual(self._advice(), {})
            result = self._plan(self._sell(trigger_price=22.57, quantity=1000))
        self.assertTrue(result["saved"], result)

    def test_a_broken_calculation_never_blocks_the_plan(self) -> None:
        with patch.object(quant_sizes, "size_advice", side_effect=RuntimeError("boom")):
            self.assertTrue(self._plan(self._sell(trigger_price=22.57, quantity=1000))["saved"])

    def test_a_small_sellable_amount_caps_the_advice_on_the_day(self) -> None:
        with self.Session() as db:
            row = db.scalar(select(PositionHoldingSnapshot).where(PositionHoldingSnapshot.symbol == SELL_SYMBOL))
            row.available_quantity = 300
            db.commit()
        self.assertEqual(self._advised(), 300)  # MORNING 当天就是计划生效日


class PromptAndPageTests(_SizeCase):
    def test_the_prompt_lists_each_account(self) -> None:
        with self.Session() as db:
            text = quant_sizes.size_block(db, OWNER, "2026-11-03", MORNING)
        self.assertIn(f"账户 {ACCOUNT}（方正证券）", text)
        self.assertIn("持 7,200 股".replace(",", ""), text.replace(",", ""))
        self.assertIn("目标约", text)
        self.assertIn("size_note", text)
        self.assertIn("不动账户原有现金", text)

    def test_no_holdings_with_bars_means_no_block(self) -> None:
        with self.Session() as db:
            for bar in db.scalars(select(DailyBar)).all():
                db.delete(bar)
            db.commit()
            self.assertEqual(quant_sizes.size_block(db, OWNER, "2026-11-03", MORNING), "")

    def test_the_scheduler_joins_prices_and_sizes(self) -> None:
        from backend.app.scheduler import rhythm_scheduler

        with patch.object(rhythm_scheduler, "SessionLocal", self.Session):
            text = rhythm_scheduler._quant_order_block({"owner_user_id": OWNER}, "premarket", MORNING)
        self.assertIn("量化挂单价", text)
        self.assertIn("量化股数", text)
        self.assertLess(text.index("量化挂单价"), text.index("量化股数"))

    def test_the_page_shows_a_change_and_its_reason(self) -> None:
        self._both(self._sell(trigger_price=22.57, quantity=3000, size_note="半年报前不想一次卖太多"))
        with self.Session() as db:
            plan_id = db.scalar(select(TradePlanItem.plan_id).limit(1))
            page = render_plan_page(db, ReportShare(share_id="a" * 32, owner_user_id=OWNER, source_type="trade_plan", source_ref=plan_id, title="t", content_md="", expires_at=datetime(2026, 12, 5)))
        self.assertIn("代码建议卖", page)
        self.assertIn("半年报前不想一次卖太多", page)

    def test_advice_text(self) -> None:
        self.assertEqual(rules.describe_advice(900), "卖 900 股")
        self.assertEqual(rules.describe_advice(-500), "可以补 500 股")
        self.assertEqual(rules.describe_advice(0), "不动")


del _PlanCase, _SizeCase

if __name__ == "__main__":
    unittest.main()
