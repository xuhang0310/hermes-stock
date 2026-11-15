"""选股回测的防作弊：只能按前一天的排名在次日开盘成交，买不进卖不掉的要算进去，判定标准不能松。不需要 LightGBM。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from backend.app.quant import research_selection as rs
from backend.app.quant.research_panel import Panel

FREE = rs.Costs(0.0, 0.0)


def _panel(close: np.ndarray, *, open_: np.ndarray | None = None, start: str = "2017-01-02", amount: float = 5e4, min_listed: int = 0) -> Panel:
    count, n = close.shape
    pre = np.vstack([close[0], close[:-1]])
    open_ = pre.copy() if open_ is None else open_
    arrays = {
        ("daily", "open"): open_,
        ("daily", "high"): np.maximum(open_, close) * 1.01,
        ("daily", "low"): np.minimum(open_, close) * 0.99,
        ("daily", "close"): close,
        ("daily", "pre_close"): pre,
        ("daily", "amount"): np.full((count, n), amount),
        ("daily_basic", "pe_ttm"): np.full((count, n), 20.0),
        ("daily_basic", "pb"): np.full((count, n), 2.0),
    }
    dates = [day.strftime("%Y%m%d") for day in pd.bdate_range(start, periods=count)]
    return Panel(dates, [f"{600000 + i}.SH" for i in range(n)], arrays, min_listed=min_listed)


def _trend(count: int, rates: list[float]) -> np.ndarray:
    return 10 * np.cumprod(1 + np.tile(np.array(rates), (count, 1)), axis=0)


def _scores(count: int, order: list[float]) -> np.ndarray:
    return np.tile(np.array(order, dtype=np.float64), (count, 1))


def _run(panel: Panel, scores: np.ndarray, **kwargs) -> dict:
    options = {"top_n": 1, "start": panel.dates[0], "end": panel.dates[-1], "hold_days": 5, "costs": FREE}
    return rs.simulate(panel, scores, **{**options, **kwargs})


class SimulationTests(unittest.TestCase):
    def test_the_first_trade_is_at_the_next_open_not_the_same_close(self) -> None:
        panel = _panel(_trend(30, [0.01, 0.0]))
        run = _run(panel, _scores(30, [1.0, 0.0]))
        self.assertEqual(run["nav"][0], 1.0)  # 第一天只排名，还没买
        close = panel.adj_close_filled[:, 0]
        self.assertAlmostEqual(run["nav"][1], close[1] / panel.adjusted("open")[1, 0])
        self.assertAlmostEqual(run["nav"][-1], close[-1] / panel.adjusted("open")[1, 0])

    def test_scores_from_the_future_are_never_read(self) -> None:
        panel = _panel(_trend(30, [0.01, -0.01]))
        scores = _scores(30, [1.0, 0.0])
        changed = scores.copy()
        changed[6:] = [0.0, 1.0]  # 第 6 天之后的排名变了
        np.testing.assert_allclose(_run(panel, scores)["nav"][:7], _run(panel, changed)["nav"][:7])

    def test_costs_are_charged_on_both_sides(self) -> None:
        panel = _panel(_trend(30, [0.0, 0.0]))
        scores = _scores(30, [1.0, 0.0])
        scores[5:] = [0.0, 1.0]  # 第二次换仓换成另一只
        run = _run(panel, scores, band=1.0, costs=rs.Costs(0.001, 0.002))
        self.assertAlmostEqual(run["nav"][-1], (1 / 1.001) * (1 - 0.002) / 1.001, places=9)
        self.assertGreater(run["sold"], 0)

    def test_a_stock_locked_at_the_upper_limit_is_skipped(self) -> None:
        close = _trend(30, [0.0, 0.0])
        close[1, 0] = close[0, 0] * 1.1
        close[2:, 0] = close[1, 0]
        open_ = np.vstack([close[0], close[:-1]])
        open_[1, 0] = close[1, 0]
        panel = _panel(close, open_=open_)
        panel._arrays[("daily", "high")][1, 0] = panel._arrays[("daily", "low")][1, 0] = close[1, 0]
        run = _run(panel, _scores(30, [1.0, 0.0]))
        self.assertAlmostEqual(run["nav"][-1], 1.0)  # 买的是第二名，第一名的涨停没吃到

    def test_a_stock_locked_at_the_lower_limit_cannot_be_sold(self) -> None:
        close = _trend(30, [0.0, 0.0])
        close[6:, 0] = close[5, 0] * 0.9
        open_ = np.vstack([close[0], close[:-1]])
        open_[6, 0] = close[6, 0]
        panel = _panel(close, open_=open_)
        panel._arrays[("daily", "high")][6, 0] = panel._arrays[("daily", "low")][6, 0] = close[6, 0]
        scores = _scores(30, [1.0, 0.0])
        scores[5:] = [0.0, 1.0]  # 第 6 天想卖，正好一字跌停
        run = _run(panel, scores, band=1.0)
        self.assertAlmostEqual(run["nav"][6], 0.9)
        self.assertAlmostEqual(run["nav"][-1], 0.9)  # 下一次换仓才卖掉，跌幅照吃

    def test_a_suspended_stock_cannot_be_bought_or_sold(self) -> None:
        close = _trend(30, [0.0, 0.01])
        close[1:4, 0] = np.nan
        panel = _panel(close)
        run = _run(panel, _scores(30, [1.0, 0.0]))
        self.assertGreater(run["nav"][-1], 1.0)  # 第一名停牌，买到的是第二名

    def test_holdings_inside_the_band_are_kept(self) -> None:
        panel = _panel(_trend(30, [0.0, 0.0, 0.0]))
        scores = _scores(30, [3.0, 2.0, 1.0])
        scores[5:] = [2.0, 3.0, 1.0]  # 原来的第一名掉到第二
        kept = _run(panel, scores, band=2.0, costs=rs.Costs(0.001, 0.001))
        swapped = _run(panel, scores, band=1.0, costs=rs.Costs(0.001, 0.001))
        self.assertEqual(kept["sold"], 0.0)
        self.assertGreater(swapped["sold"], 0.0)

    def test_a_holding_that_lost_its_score_is_sold(self) -> None:
        panel = _panel(_trend(30, [0.0, 0.0]))
        scores = _scores(30, [1.0, 0.0])
        scores[5:, 0] = np.nan  # 变成 ST 或掉出股票池
        self.assertGreater(_run(panel, scores, band=5.0)["sold"], 0.0)

    def test_money_is_split_equally_across_the_top_n(self) -> None:
        panel = _panel(_trend(30, [0.02, 0.0, 0.0]))
        run = _run(panel, _scores(30, [3.0, 2.0, 1.0]), top_n=2)
        winner = panel.adj_close_filled[-1, 0] / panel.adjusted("open")[1, 0]
        self.assertAlmostEqual(run["nav"][-1], 0.5 * winner + 0.5)


class BenchmarkAndMetricsTests(unittest.TestCase):
    def test_the_benchmark_is_the_equal_weight_average(self) -> None:
        panel = _panel(_trend(20, [0.01, -0.01]))
        nav = rs.benchmark_nav(panel, np.ones((20, 2), dtype=bool), panel.dates[0], panel.dates[-1])
        self.assertAlmostEqual(nav[-1], 1.0, places=9)

    def test_stocks_outside_the_pool_do_not_count(self) -> None:
        panel = _panel(_trend(20, [0.01, -0.01]))
        members = np.ones((20, 2), dtype=bool)
        members[:, 1] = False
        nav = rs.benchmark_nav(panel, members, panel.dates[0], panel.dates[-1])
        self.assertAlmostEqual(nav[-1], 1.01**19)

    def test_excess_and_years(self) -> None:
        dates = np.array([day.strftime("%Y%m%d") for day in pd.bdate_range("2017-12-01", periods=60)])
        nav = np.cumprod(np.full(60, 1.002))
        bench = np.cumprod(np.full(60, 1.001))
        result = rs.metrics(dates, nav, bench)
        self.assertGreater(result["excess"], 0)
        self.assertEqual(sorted(result["excess_by_year"]), ["2017", "2018"])
        self.assertTrue(all(value > 0 for value in result["excess_by_year"].values()))
        self.assertEqual(result["drawdown"], 0.0)

    def test_drawdown_is_measured_from_the_peak(self) -> None:
        self.assertAlmostEqual(rs._drawdown(np.array([1.0, 1.2, 0.9, 1.1])), 0.9 / 1.2 - 1)


class ScoreTests(unittest.TestCase):
    def test_the_pool_drops_thinly_traded_stocks(self) -> None:
        self.assertFalse(rs.pool(_panel(_trend(30, [0.0, 0.0]), amount=rs.MIN_AMOUNT - 1)).any())
        self.assertTrue(rs.pool(_panel(_trend(30, [0.0, 0.0]), amount=rs.MIN_AMOUNT + 1))[-1].all())

    def test_the_composite_prefers_low_values_of_reversed_features(self) -> None:
        features = {name: np.tile(np.array([1.0, 2.0, 3.0]), (4, 1)) for name in rs.FEATURE_SIGNS}
        scores = rs.composite_scores(features, np.array([2, 3]), (4, 3))
        self.assertTrue(np.isnan(scores[0]).all())
        self.assertGreater(scores[3, 0], scores[3, 2])

    def test_the_current_rule_rewards_recent_gains_as_it_does_online(self) -> None:
        rng = np.random.default_rng(3)
        noise = 1 + rng.normal(0, 0.01, (60, 40))
        noise[:, 0] += 0.01  # 第一只一直涨
        panel = _panel(10 * np.cumprod(noise, axis=0), amount=rs.CURRENT_MIN_AMOUNT * 2)
        scores = rs.current_rule_scores(panel, np.array([59]))
        self.assertGreater(scores[59, 0], np.nanmedian(scores[59]))
        self.assertTrue(np.isnan(scores[0]).all())

    def test_the_model_only_scores_years_after_its_training_data(self) -> None:
        count = 420
        rng = np.random.default_rng(5)
        panel = _panel(10 * np.cumprod(1 + rng.normal(0, 0.01, (count, 20)), axis=0), start="2017-06-01")
        features = {name: rng.normal(size=(count, 20)).astype(np.float32) for name in rs.rm.RANK_MODEL_FEATURES}
        label = rng.normal(size=(count, 20))
        seen: list[int] = []

        class Flat:
            def predict(self, x: np.ndarray) -> np.ndarray:
                return np.zeros(len(x))

        def fit(x: np.ndarray, y: np.ndarray, objective: str) -> Flat:
            seen.append(len(x))
            return Flat()

        scores = rs.model_scores(panel, features, label, first_year=2018, end="20181231", fit=fit)
        dates = np.array(panel.dates)
        self.assertEqual(len(seen), 1)
        self.assertTrue(np.isnan(scores[dates < "20180101"]).all())
        self.assertFalse(np.isnan(scores[(dates >= "20180101") & (dates <= "20181231")]).any())


def _item(**changes) -> dict:
    base = {"excess": 0.08, "information_ratio": 0.9, "years": 4, "years_ahead": 4, "worst_start_excess": 0.02, "stress_excess": 0.03, "drawdown": -0.3, "bench_drawdown": -0.3}
    return {**base, **changes}


class VerdictTests(unittest.TestCase):
    def test_dev_needs_every_condition(self) -> None:
        current = _item(excess=0.01)
        self.assertTrue(rs.dev_passes(_item(), current))
        self.assertFalse(rs.dev_passes(_item(excess=0.049), current))
        self.assertFalse(rs.dev_passes(_item(information_ratio=0.49), current))
        self.assertFalse(rs.dev_passes(_item(years_ahead=2), current))
        self.assertFalse(rs.dev_passes(_item(worst_start_excess=-0.001), current))
        self.assertFalse(rs.dev_passes(_item(), _item(excess=0.09)))

    def test_the_model_is_used_only_when_it_beats_the_plain_composite(self) -> None:
        results = {"model": {"10": _item(excess=0.07)}, "composite": {"10": _item(excess=0.08)}, "current": {"10": _item(excess=0.0)}}
        self.assertEqual(rs.dev_choice(results), {"10": "composite"})
        results["model"]["10"] = _item(excess=0.09)
        self.assertEqual(rs.dev_choice(results), {"10": "model"})
        results["model"]["10"] = results["composite"]["10"] = _item(excess=0.01)
        self.assertEqual(rs.dev_choice(results), {"10": None})

    def test_final_needs_every_condition(self) -> None:
        self.assertTrue(rs.final_passes(_item()))
        self.assertFalse(rs.final_passes(_item(excess=0.029)))
        self.assertFalse(rs.final_passes(_item(information_ratio=0.4)))
        self.assertFalse(rs.final_passes(_item(years=8, years_ahead=4)))
        self.assertFalse(rs.final_passes(_item(drawdown=-0.36)))
        self.assertFalse(rs.final_passes(_item(worst_start_excess=0.0)))
        self.assertFalse(rs.final_passes(_item(stress_excess=-0.01)))


class RunOnceTests(unittest.TestCase):
    def test_the_final_check_refuses_a_second_run(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            rs.final_report_path(out).write_text("{}")
            with self.assertRaises(SystemExit):
                rs.run_final(out)

    def test_the_final_check_needs_a_passing_dev_result(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            with self.assertRaises(SystemExit):
                rs.run_final(out)
            rs.dev_report_path(out).write_text(json.dumps({"chosen": {"10": None, "30": None}}))
            with self.assertRaises(SystemExit):
                rs.run_final(out)

    def test_no_picks_without_a_passing_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            with self.assertRaises(SystemExit):
                rs.run_picks(out)
            rs.final_report_path(out).write_text(json.dumps({"verdict": {"10": {"strategy": "model", "passed": False}}}))
            with self.assertRaises(SystemExit):
                rs.run_picks(out)


class EndToEndTests(unittest.TestCase):
    def test_a_real_edge_shows_up_and_noise_does_not(self) -> None:
        """价格里埋一个真信号：按它选要明显跑赢，按随机数选不行。整条链路（排名、成交、费用、基准、汇总）一起验。"""
        rng = np.random.default_rng(7)
        count, n = 300, 120
        signal = rng.normal(size=(count, n))
        persistent = pd.DataFrame(signal).ewm(alpha=0.2).mean().to_numpy()
        drift = np.vstack([np.zeros((1, n)), persistent[:-1]]) * 0.004  # 今天的信号决定明天起的涨跌
        close = 10 * np.cumprod(1 + drift + rng.normal(0, 0.01, (count, n)), axis=0)
        panel = _panel(close)
        start, end = panel.dates[0], panel.dates[-1]
        bench = rs.benchmark_nav(panel, rs.pool(panel), start, end)
        good = rs.evaluate(panel, persistent, bench, top_n=10, start=start, end=end)
        noise = rs.evaluate(panel, rng.normal(size=(count, n)), bench, top_n=10, start=start, end=end)
        self.assertGreater(good["excess"], 0.10)
        self.assertGreater(good["information_ratio"], 1.0)
        self.assertGreater(good["excess"], good["stress_excess"])  # 费用加倍，超额变少
        self.assertLess(abs(noise["excess"]), good["excess"] / 2)


if __name__ == "__main__":
    unittest.main()
