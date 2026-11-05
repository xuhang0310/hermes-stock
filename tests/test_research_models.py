"""预测模型的防作弊：训练集不能读到测试年的价格，成交样本算得对。不需要 LightGBM。"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from backend.app.quant import research_models as rm
from backend.app.quant.order_table_build import market_state
from backend.app.quant.research_panel import Panel

DAYS = [day.strftime("%Y%m%d") for day in pd.bdate_range("2012-01-02", "2018-12-31")]


def _panel(count: int = 200, n: int = 30, seed: int = 11) -> Panel:
    rng = np.random.default_rng(seed)
    close = 20 * np.cumprod(1 + rng.normal(0, 0.02, (count, n)), axis=0)
    pre = np.vstack([close[0], close[:-1]])
    open_ = pre * (1 + rng.normal(0, 0.003, (count, n)))
    high = np.maximum(open_, close) * (1 + rng.uniform(0.001, 0.02, (count, n)))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.001, 0.02, (count, n)))
    dates = [day.strftime("%Y%m%d") for day in pd.bdate_range("2017-01-02", periods=count)]
    arrays = {
        ("daily", "open"): open_,
        ("daily", "high"): high,
        ("daily", "low"): low,
        ("daily", "close"): close,
        ("daily", "pre_close"): pre,
        ("daily", "amount"): rng.uniform(1e4, 1e5, (count, n)),
    }
    return Panel(dates, [f"{600000 + i}.SH" for i in range(n)], arrays)


class WalkForwardTests(unittest.TestCase):
    def test_each_year_is_predicted_from_earlier_data_only(self) -> None:
        splits = list(rm.walk_forward(DAYS))
        self.assertEqual([split.year for split in splits], [2015, 2016, 2017, 2018])
        for split in splits:
            self.assertLess(split.train[-1], split.test[0])

    def test_training_labels_cannot_reach_the_test_year(self) -> None:
        for split in rm.walk_forward(DAYS):
            last_label_day = split.train[-1] + rm.HORIZON + 1
            self.assertLess(last_label_day, split.test[0])

    def test_validation_years_are_never_used_by_default(self) -> None:
        days = DAYS + [day.strftime("%Y%m%d") for day in pd.bdate_range("2019-01-01", "2020-12-31")]
        splits = list(rm.walk_forward(days))
        self.assertEqual(splits[-1].year, 2018)
        array = np.array(days)
        for split in splits:
            self.assertLessEqual(array[split.test[-1]], "20181231")
            self.assertLessEqual(array[split.train[-1]], "20181231")

    def test_training_starts_at_the_development_start(self) -> None:
        self.assertGreaterEqual(DAYS[next(rm.walk_forward(DAYS)).train[0]], rm.DEV_START)


class RankTests(unittest.TestCase):
    def test_features_become_daily_ranks_and_keep_gaps(self) -> None:
        values = np.array([[1.0, 2.0, np.nan], [30.0, 10.0, 20.0]])
        ranked = rm.rank_features(values, np.array([0, 1]))
        np.testing.assert_allclose(ranked[1], [1.0, 1 / 3, 2 / 3], rtol=1e-6)
        np.testing.assert_allclose(ranked[0][:2], [0.5, 1.0], rtol=1e-6)
        self.assertTrue(np.isnan(ranked[0, 2]))

    def test_assemble_stacks_features_last(self) -> None:
        values = {"a": np.zeros((3, 2)), "b": np.ones((3, 2))}
        self.assertEqual(rm.assemble(values, ["a", "b"], np.array([0, 2])).shape, (2, 2, 2))


class TouchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.panel = _panel()
        self.state = market_state(self.panel)
        self.rows = np.arange(130, 170)
        self.x = np.zeros((len(self.rows), len(self.panel.codes), 1), dtype=np.float32)

    def test_the_touch_flag_matches_the_high(self) -> None:
        samples = rm.touch_samples(self.panel, self.state, self.x, self.rows, per_stock_day=1)
        self.assertEqual(samples["x"].shape[1], 2)
        self.assertTrue(set(np.unique(samples["touched"])) <= {0, 1})
        self.assertTrue(np.isin(np.round(samples["x"][:, 1], 2), np.round(rm.SELL_GRID, 2)).all())

    def test_farther_prices_are_touched_less_often(self) -> None:
        samples = rm.touch_samples(self.panel, self.state, self.x, self.rows, per_stock_day=4)
        k, touched = samples["x"][:, 1], samples["touched"]
        self.assertGreater(touched[k <= 0.5].mean(), touched[k >= 2.0].mean())

    def test_only_touched_keeps_filled_samples(self) -> None:
        samples = rm.touch_samples(self.panel, self.state, self.x, self.rows, only_touched=True)
        self.assertTrue((samples["touched"] == 1).all())

    def test_the_same_seed_gives_the_same_samples(self) -> None:
        one = rm.touch_samples(self.panel, self.state, self.x, self.rows, seed=5)
        two = rm.touch_samples(self.panel, self.state, self.x, self.rows, seed=5)
        np.testing.assert_array_equal(one["x"], two["x"])

    def test_samples_near_the_end_are_skipped(self) -> None:
        rows = np.arange(len(self.panel.dates) - rm.HORIZON - 1, len(self.panel.dates))
        x = np.zeros((len(rows), len(self.panel.codes), 1), dtype=np.float32)
        with self.assertRaises(ValueError):  # 没有可用样本
            rm.touch_samples(self.panel, self.state, x, rows)


class CalibrationTests(unittest.TestCase):
    def test_a_well_calibrated_forecast_has_small_gaps(self) -> None:
        rng = np.random.default_rng(1)
        probability = rng.uniform(0, 1, 200_000)
        actual = (rng.uniform(0, 1, 200_000) < probability).astype(int)
        self.assertLess(rm.calibration(probability, actual)["差"].abs().max(), 0.02)

    def test_a_biased_forecast_shows_up(self) -> None:
        rng = np.random.default_rng(1)
        probability = rng.uniform(0, 0.5, 100_000)
        actual = (rng.uniform(0, 1, 100_000) < probability * 0.5).astype(int)
        self.assertGreater(rm.calibration(probability, actual)["差"].abs().max(), 0.1)


if __name__ == "__main__":
    unittest.main()
