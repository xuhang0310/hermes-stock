"""量化挂单价 v1：选价、不挂、止损、校验接线。内存 SQLite，不连 MySQL。"""
from __future__ import annotations

import dataclasses
import unittest
from datetime import datetime
from unittest.mock import patch

import numpy as np
from sqlalchemy import select

from backend.app.core.models import DailyBar, MarketEvent, Position, StockModelSignal, WatchlistItem
from backend.app.core.settings import settings as real_settings
from backend.app.events.rules import CRITICAL
from backend.app.plans import quant_orders, quant_sizes
from backend.app.plans.quant_orders import QUANT_SOURCES
from backend.app.plans.service import to_utc
from backend.app.quant import order_engine
from backend.app.quant.order_engine import (
    DOWN,
    MIN_NET_EDGE,
    RANGE,
    SELL_COST,
    STOP_ATR,
    UP,
    Level,
    OrderLevels,
    describe_orders,
    load_table,
    net_edge,
    plan_orders,
    price_problem,
    side_value,
    trend_of,
    vol_bucket,
)
from backend.app.quant.order_table_build import CELLS, best_multiples, build_table, evaluate_policy, market_state
from backend.app.quant.research_panel import Panel
from backend.app.quant.tech_signal import MIN_BARS, Bar
from tests.test_trade_plans import ACCOUNT, BUY_SYMBOL, MORNING, OWNER, SELL_SYMBOL, _PlanCase


def _table(sell: dict[str, tuple[float, float]], stop: float = -0.002) -> dict:
    """所有分组用同一份数字，省得每个测试都写九遍。"""
    return {
        "sell": {cell: {k: {"fill": fill, "edge": edge, "raw": 0, "n": 1000} for k, (fill, edge) in sell.items()} for cell in CELLS},
        "buy": {cell: {"1.0": {"fill": 0.1, "edge": 0.01, "raw": 0, "n": 1000}} for cell in CELLS},
        "stop": {cell: {"3.0": {"fill": 0.01, "edge": stop, "raw": 0, "n": 1000}} for cell in CELLS},
    }


def _flat_bars(count: int = 80, price: float = 20.0, spread: float = 0.5) -> list[Bar]:
    return [
        Bar(trade_date=f"2026{i:04d}", high=price + spread, low=price - spread, close=price, pre_close=price)
        for i in range(count)
    ]


class ChoiceTests(unittest.TestCase):
    def test_the_highest_value_price_wins(self) -> None:
        table = _table({"0.25": (0.6, -0.003), "1.0": (0.1, -0.03), "2.0": (0.01, -0.05)})
        levels = plan_orders(_flat_bars(), table)
        self.assertEqual(levels.sell.atr_multiple, 1.0)
        self.assertAlmostEqual(levels.sell.value, 0.1 * (0.03 - SELL_COST))
        self.assertAlmostEqual(levels.sell.price, 21.0, places=2)

    def test_a_thin_edge_is_ignored_even_with_a_high_fill_rate(self) -> None:
        # 2012 至今的真实表里，低波动下跌股贴着昨收挂的价值最高，但成交一次只多赚 0.03%
        table = _table({"0.25": (0.61, -0.0016), "2.0": (0.02, -0.01)})
        self.assertLess(net_edge("sell", -0.0016), MIN_NET_EDGE)
        self.assertEqual(plan_orders(_flat_bars(), table).sell.atr_multiple, 2.0)

    def test_no_sell_order_when_nothing_is_worth_it(self) -> None:
        self.assertIsNone(plan_orders(_flat_bars(), _table({"1.0": (0.1, 0.01), "2.0": (0.01, -0.0015)})).sell)

    def test_prices_outside_the_daily_limit_are_not_options(self) -> None:
        table = _table({"1.0": (0.1, -0.01), "3.0": (0.5, -0.05)})
        levels = plan_orders(_flat_bars(spread=1.0), table, limit_ratio=0.1)  # 3 倍 ATR 超过 +10%
        self.assertEqual(levels.sell.atr_multiple, 1.0)

    def test_sell_prices_round_up_and_stops_round_down_to_a_cent(self) -> None:
        bars = _flat_bars(price=20.0, spread=0.3333)
        levels = plan_orders(bars, _table({"1.0": (0.1, -0.01)}))
        self.assertEqual(levels.sell.price, 20.67)
        self.assertEqual(plan_orders(_flat_bars(price=20.0, spread=0.1), _table({"1.0": (0.1, -0.01)})).stop.price, 19.4)

    def test_the_stop_floor_is_three_atr_and_never_below_limit_down(self) -> None:
        levels = plan_orders(_flat_bars(spread=0.25), _table({"1.0": (0.1, -0.01)}))
        self.assertEqual(levels.stop.atr_multiple, STOP_ATR)
        self.assertAlmostEqual(levels.stop.price, 18.5, places=2)
        wide = plan_orders(_flat_bars(spread=2.0), _table({"1.0": (0.1, -0.01)}), limit_ratio=0.1)
        self.assertEqual(wide.stop.price, 18.0)

    def test_too_few_bars_gives_nothing(self) -> None:
        self.assertIsNone(plan_orders(_flat_bars(MIN_BARS - 1), _table({"1.0": (0.1, -0.01)})))

    def test_the_same_bars_always_give_the_same_prices(self) -> None:
        table = _table({"1.0": (0.1, -0.01)})
        self.assertEqual(plan_orders(_flat_bars(), table), plan_orders(_flat_bars(), table))

    def test_the_groups_are_read_from_the_bars(self) -> None:
        self.assertEqual(trend_of(11, 10, 9), UP)
        self.assertEqual(trend_of(9, 10, 11), DOWN)
        self.assertEqual(trend_of(11, 10, 11), RANGE)
        self.assertEqual([vol_bucket(v) for v in (0.01, 0.03, 0.05)], [0, 1, 2])

    def test_values_add_costs_the_right_way(self) -> None:
        self.assertAlmostEqual(side_value("sell", 0.5, -0.01), 0.5 * (0.01 - 0.0013))
        self.assertAlmostEqual(side_value("buy", 0.5, 0.01), 0.5 * (0.01 - 0.0008))


class RealTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = load_table()
        self.assertIsNotNone(self.table, "order_table.json 要随代码提交")

    def test_every_group_has_sell_and_stop_numbers(self) -> None:
        for cell in CELLS:
            self.assertTrue(self.table["sell"][cell], cell)
            self.assertIn(str(STOP_ATR), self.table["stop"][cell], cell)

    def test_fill_rates_fall_as_the_price_moves_away(self) -> None:
        for cell in CELLS:
            fills = [stats["fill"] for _, stats in sorted(self.table["sell"][cell].items(), key=lambda kv: float(kv[0]))]
            self.assertEqual(fills, sorted(fills, reverse=True), cell)

    def test_the_samples_are_large(self) -> None:
        self.assertGreater(min(s["n"] for cell in CELLS for s in self.table["sell"][cell].values()), 200)


class ProblemTests(unittest.TestCase):
    LEVELS = OrderLevels(
        close=21.79,
        atr=1.03,
        trend=DOWN,
        vol_bucket=2,
        sell=Level(23.34, 1.5, 0.031, -0.0043, 0.0001),
        buy=None,
        stop=Level(19.73, 2.0, 0.01, 0.0, 0.0),
    )

    def test_the_right_prices_pass(self) -> None:
        self.assertIsNone(price_problem(self.LEVELS, "sell", 23.34))
        self.assertIsNone(price_problem(self.LEVELS, "reduce", 23.34))
        self.assertIsNone(price_problem(self.LEVELS, "stop_loss", 19.73))

    def test_a_different_price_is_rejected_with_the_right_one(self) -> None:
        problem = price_problem(self.LEVELS, "reduce", 22.18)
        self.assertIn("23.34", problem)
        self.assertIn("22.18", problem)

    def test_a_stop_can_be_wider_but_not_tighter(self) -> None:
        self.assertIn("不能高于 19.73", price_problem(self.LEVELS, "stop_loss", 20.59))
        self.assertIsNone(price_problem(self.LEVELS, "stop_loss", 18.0))

    def test_no_sell_order_when_the_table_says_so(self) -> None:
        levels = dataclasses.replace(self.LEVELS, sell=None)
        self.assertIn("不要挂卖单", price_problem(levels, "sell", 23.34))

    def test_buy_prices_are_not_checked_in_this_version(self) -> None:
        self.assertIsNone(price_problem(self.LEVELS, "buy", 1.0))
        self.assertIsNone(price_problem(self.LEVELS, "add", 1.0))

    def test_the_description_names_the_basis(self) -> None:
        text = describe_orders("同力天启", SELL_SYMBOL, self.LEVELS)
        self.assertIn("同力天启（605286）", text)
        self.assertIn("下降趋势", text)
        self.assertIn("23.34", text)
        self.assertIn("1.5 倍 ATR", text)
        self.assertIn("成交概率 3%", text)
        self.assertNotIn("买单", text)

    def test_a_conditional_sell_is_checked_as_a_stop(self) -> None:
        self.assertIn("止损不能高于", quant_orders.item_problem(self.LEVELS, "sell", "fall_to", 20.59))
        self.assertIn("卖价", quant_orders.item_problem(self.LEVELS, "sell", "limit", 22.18))
        self.assertIn("卖价", quant_orders.item_problem(self.LEVELS, "reduce", "rise_to", 22.18))
        self.assertIsNone(quant_orders.item_problem(self.LEVELS, "buy", "limit", 19.0))
        self.assertIsNone(quant_orders.item_problem(None, "sell", "limit", 19.0))


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        rng = np.random.default_rng(3)
        days, n = 260, 40
        close = 20 * np.cumprod(1 + rng.normal(0, 0.02, (days, n)), axis=0)
        pre = np.vstack([close[0], close[:-1]])
        open_ = pre * (1 + rng.normal(0, 0.003, (days, n)))
        high = np.maximum(open_, close) * (1 + rng.uniform(0.001, 0.02, (days, n)))
        low = np.minimum(open_, close) * (1 - rng.uniform(0.001, 0.02, (days, n)))
        import pandas as pd

        self.dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("2017-01-02", periods=days)]
        self.arrays = {
            ("daily", "open"): open_,
            ("daily", "high"): high,
            ("daily", "low"): low,
            ("daily", "close"): close,
            ("daily", "pre_close"): pre,
            ("daily", "amount"): rng.uniform(1e4, 1e5, (days, n)),
        }
        self.codes = [f"{600000 + i}.SH" for i in range(n)]
        self.panel = Panel(self.dates, self.codes, self.arrays)
        self.state = market_state(self.panel)

    def test_fill_rates_are_real_probabilities_and_fall_with_distance(self) -> None:
        table = build_table(self.panel, self.state, "20170101", "20171231")
        rows = [cell for cell in table["sell"].values() if len(cell) >= 3]
        self.assertTrue(rows)
        for cell in rows:
            fills = [s["fill"] for _, s in sorted(cell.items(), key=lambda kv: float(kv[0]))]
            self.assertTrue(all(0 <= f <= 1 for f in fills))
            self.assertEqual(fills, sorted(fills, reverse=True))

    def test_a_table_cannot_see_past_its_range(self) -> None:
        cut = 200
        arrays = {key: np.asarray(value)[:cut] for key, value in self.arrays.items()}
        short = Panel(self.dates[:cut], self.codes, arrays)
        end = self.dates[cut - 1 - 11]
        first = build_table(short, market_state(short), "20170101", end)
        second = build_table(self.panel, self.state, "20170101", end)
        self.assertEqual(first["sell"], second["sell"])

    def test_the_policy_only_places_where_there_is_a_choice(self) -> None:
        none_chosen = {i: None for i in range(len(CELLS))}
        frame = evaluate_policy(self.panel, self.state, "sell", none_chosen, "20170101", "20171231")
        self.assertEqual(frame["挂单"].sum(), 0)
        self.assertEqual(frame["每次挂单价值%"].sum(), 0)

    def test_thin_edges_are_not_chosen(self) -> None:
        table = _table({"0.25": (0.6, -0.0016)})
        self.assertEqual(set(best_multiples(table, "sell").values()), {None})


class PlanWiringTests(_PlanCase):
    """用 test_trade_plans 的账户和持仓。同力天启给一段平稳的 K 线，统计表用固定数字。"""

    TABLE = _table({"1.0": (0.1, -0.01)})

    def setUp(self) -> None:
        super().setUp()
        quant_orders._table_cache.clear()
        quant_orders._table_cache["table"] = self.TABLE
        self.addCleanup(quant_orders._table_cache.clear)
        self.settings = patch.object(quant_orders, "settings", dataclasses.replace(real_settings, quant_orders_enabled=True))
        self.settings.start()
        self.addCleanup(self.settings.stop)
        # 这里只测价格，股数建议在 test_quant_sizes 里测
        sizes = patch.object(quant_sizes, "settings", dataclasses.replace(real_settings, quant_sizes_enabled=False))
        sizes.start()
        self.addCleanup(sizes.stop)
        with self.Session() as db:
            for index, day in enumerate(np.arange(np.datetime64("2026-07-01"), np.datetime64("2026-11-03"))):
                stamp = str(day).replace("-", "")
                db.add(DailyBar(symbol=SELL_SYMBOL, trade_date=stamp, open=21.57, high=22.07, low=21.07, close=21.57, pre_close=21.57, source="tushare"))
            db.commit()

    def _codes(self, result: dict) -> str:
        return "\n".join(result.get("issues") or [])

    def test_the_plan_only_saves_with_the_engine_price(self) -> None:
        rejected = self._save([self._sell(trigger_price=21.0, quantity=2400)])
        self.assertFalse(rejected["saved"])
        self.assertIn("卖价要用系统算的 22.57", self._codes(rejected))
        accepted = self._save([self._sell(trigger_price=22.57, quantity=2400)])
        self.assertTrue(accepted["saved"], accepted)

    def test_the_stop_price_is_checked_too(self) -> None:
        rejected = self._save([self._sell(action="stop_loss", trigger_type="fall_to", trigger_price=20.0)])
        self.assertIn("止损不能高于 19.41", self._codes(rejected))
        self.assertTrue(self._save([self._sell(action="stop_loss", trigger_type="fall_to", trigger_price=19.41)])["saved"])

    def test_no_sell_order_when_the_table_finds_no_edge(self) -> None:
        quant_orders._table_cache["table"] = _table({"1.0": (0.1, 0.01)})
        self.assertIn("不要挂卖单", self._codes(self._save([self._sell(trigger_price=22.57)])))

    def test_hold_items_are_untouched(self) -> None:
        self.assertTrue(self._save([{"account_id": ACCOUNT, "symbol": SELL_SYMBOL, "action": "hold"}])["saved"])

    def test_buy_prices_stay_with_the_agent(self) -> None:
        result = self._save([{"account_id": ACCOUNT, "symbol": BUY_SYMBOL, "action": "buy", "trigger_type": "limit", "trigger_price": 19.5, "quantity": 100}])
        self.assertNotIn("系统算的", self._codes(result))

    def test_other_plan_kinds_are_not_checked(self) -> None:
        self.assertNotIn("weekly", QUANT_SOURCES)
        self.assertNotIn("selection", QUANT_SOURCES)
        with self.Session() as db:
            self.assertEqual(quant_orders.enforceable_levels(db, OWNER, "weekly", "2026-11-03", {SELL_SYMBOL}, MORNING), {})

    def test_the_switch_turns_it_off(self) -> None:
        with patch.object(quant_orders, "settings", dataclasses.replace(real_settings, quant_orders_enabled=False)):
            self.assertTrue(self._save([self._sell(trigger_price=21.0)])["saved"])

    def test_a_stock_without_bars_is_not_blocked(self) -> None:
        with self.Session() as db:
            for bar in db.scalars(select(DailyBar)).all():
                db.delete(bar)
            db.commit()
        self.assertTrue(self._save([self._sell(trigger_price=21.0)])["saved"])

    def test_an_old_bar_is_not_used_to_enforce_a_price(self) -> None:
        with self.Session() as db:
            latest = db.scalar(select(DailyBar).where(DailyBar.symbol == SELL_SYMBOL, DailyBar.trade_date == "20261102"))
            db.delete(latest)
            db.commit()
        self.assertTrue(self._save([self._sell(trigger_price=21.0)])["saved"])

    def test_a_broken_engine_never_blocks_the_plan(self) -> None:
        with patch.object(quant_orders, "levels_for", side_effect=RuntimeError("boom")):
            self.assertTrue(self._save([self._sell(trigger_price=21.0)])["saved"])

    def test_critical_news_frees_the_price(self) -> None:
        with self.Session() as db:
            db.add(
                MarketEvent(
                    owner_user_id=OWNER, symbol=SELL_SYMBOL, stock_name="同力天启", source="announcement", title="收到立案告知书",
                    content_hash="h", event_type="立案调查", severity=CRITICAL, status="pushed", created_at=to_utc(datetime(2026, 11, 2, 20, 0)),
                )
            )
            db.commit()
        self.assertTrue(self._save([self._sell(trigger_price=21.0)])["saved"])

    def test_the_prompt_lists_prices_for_holdings(self) -> None:
        with self.Session() as db:
            db.add(Position(owner_user_id=OWNER, account_id=ACCOUNT, symbol=SELL_SYMBOL, position_pct=0, quantity=7200, status="open"))
            db.add(WatchlistItem(owner_user_id=OWNER, symbol="000001.SZ", group_id=1, status="active"))
            db.commit()
            text = quant_orders.plan_block(db, OWNER, "2026-11-03", MORNING)
        self.assertIn("同力天启（605286）", text)
        self.assertIn("22.57", text)
        self.assertIn("写别的价存不进去", text)
        self.assertIn("止损可以不设", text)
        self.assertIn("买价这一版还没有量化结果", text)

    def _signal(self, k: float | None, day: str = "20261102") -> None:
        with self.Session() as db:
            db.add(StockModelSignal(trade_date=day, symbol=SELL_SYMBOL, model_rank=0.5, buy_k=k))
            db.commit()

    def _add(self, price: float) -> dict:
        return {"account_id": ACCOUNT, "symbol": SELL_SYMBOL, "action": "add", "trigger_price": price, "quantity": 100, "funding_note": "用现金"}

    def test_a_buy_only_saves_with_the_model_price(self) -> None:
        self._signal(1.0)
        rejected = self._save([self._add(21.0)])
        self.assertFalse(rejected["saved"])
        self.assertIn("买价要用系统算的 20.57", self._codes(rejected))
        self.assertTrue(self._save([self._add(20.57)])["saved"])

    def test_a_buy_is_refused_when_the_model_says_no(self) -> None:
        self._signal(None)
        rejected = self._save([self._add(20.57)])
        self.assertFalse(rejected["saved"])
        self.assertIn("没有优势", self._codes(rejected))

    def test_a_buy_without_a_signal_is_not_blocked(self) -> None:
        self.assertTrue(self._save([self._add(21.0)])["saved"])

    def test_an_old_signal_is_not_used(self) -> None:
        self._signal(1.0, day="20261030")
        self.assertTrue(self._save([self._add(21.0)])["saved"])

    def test_sells_are_not_affected_by_the_buy_model(self) -> None:
        self._signal(None)
        self.assertIsNone(quant_orders.buy_problem({SELL_SYMBOL: None}, SELL_SYMBOL, "sell", 22.57))
        self.assertIsNone(quant_orders.buy_problem({SELL_SYMBOL: None}, SELL_SYMBOL, "stop_loss", 18.0))

    def test_a_broken_signal_lookup_never_blocks_the_plan(self) -> None:
        self._signal(1.0)
        with patch.object(quant_orders, "model_buys", side_effect=RuntimeError("boom")):
            self.assertTrue(self._save([self._add(21.0)])["saved"])

    def test_the_prompt_lists_buy_prices_once_there_are_signals(self) -> None:
        self._signal(1.0)
        with self.Session() as db:
            db.add(Position(owner_user_id=OWNER, account_id=ACCOUNT, symbol=SELL_SYMBOL, position_pct=0, quantity=7200, status="open"))
            db.commit()
            text = quant_orders.plan_block(db, OWNER, "2026-11-03", MORNING)
        self.assertIn("买单 20.57 元", text)
        self.assertIn("买单同样只能用上面的价", text)
        self.assertNotIn("买价这一版还没有量化结果", text)

    def test_the_prompt_says_when_the_model_says_no(self) -> None:
        self._signal(None)
        with self.Session() as db:
            db.add(Position(owner_user_id=OWNER, account_id=ACCOUNT, symbol=SELL_SYMBOL, position_pct=0, quantity=7200, status="open"))
            db.commit()
            self.assertIn("买单：买入模型判断没有优势，不挂", quant_orders.plan_block(db, OWNER, "2026-11-03", MORNING))

    def test_the_scheduler_adds_the_block_only_to_plan_jobs(self) -> None:
        from backend.app.scheduler import rhythm_scheduler

        with patch.object(rhythm_scheduler, "SessionLocal", self.Session), patch.object(quant_orders, "plan_block", return_value="量化挂单价") as block:
            snap = {"owner_user_id": OWNER}
            self.assertEqual(rhythm_scheduler._quant_order_block(snap, "premarket", MORNING), "量化挂单价")
            self.assertEqual(block.call_args.args[1:3], (OWNER, "2026-11-03"))
            self.assertEqual(rhythm_scheduler._quant_order_block(snap, "selection", MORNING), "")
            self.assertEqual(rhythm_scheduler._quant_order_block(snap, None, MORNING), "")
            self.assertEqual(rhythm_scheduler._quant_order_block({"owner_user_id": None}, "premarket", MORNING), "")


del _PlanCase

if __name__ == "__main__":
    unittest.main()
