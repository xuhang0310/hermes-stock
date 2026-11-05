"""买入模型的选价逻辑和价值统计，用小矩阵验证，不训练真实模型。"""
from __future__ import annotations

import unittest
import unittest.mock

import numpy as np

from backend.app.quant import research_buy_model as model
from backend.app.quant.order_engine import BUY_COST

GRID = np.array([0.5, 1.0, 2.0], dtype=np.float32)


class ChooseTests(unittest.TestCase):
    def test_picks_the_price_with_the_highest_value(self) -> None:
        # 近的价容易成交但走势差，远的价走势好但难成交，取乘积最大的
        probability = np.array([[0.6, 0.3, 0.1]])
        edge = np.array([[0.002, 0.02, 0.03]])
        self.assertEqual(model.choose(probability, edge, GRID).tolist(), [1.0])

    def test_no_order_when_every_value_is_negative(self) -> None:
        self.assertTrue(np.isnan(model.choose(np.array([[0.5, 0.4, 0.3]]), np.array([[-0.01, -0.02, BUY_COST]]), GRID)[0]))

    def test_the_cost_comes_off_before_comparing(self) -> None:
        self.assertTrue(np.isnan(model.choose(np.array([[0.9, 0.9, 0.9]]), np.array([[BUY_COST / 2] * 3]), GRID)[0]))

    def test_a_minimum_edge_removes_thin_orders(self) -> None:
        probability, edge = np.array([[0.9, 0.05, 0.01]]), np.array([[0.002, 0.01, 0.02]])
        self.assertEqual(model.choose(probability, edge, GRID).tolist(), [0.5])
        self.assertEqual(model.choose(probability, edge, GRID, 0.005).tolist(), [1.0])

    def test_a_direct_value_model_skips_the_multiplication(self) -> None:
        self.assertEqual(model.choose(None, np.array([[0.001, 0.003, 0.002]]), GRID).tolist(), [1.0])
        self.assertTrue(np.isnan(model.choose(None, np.array([[-0.1, -0.2, 0.0]]), GRID)[0]))

    def test_each_row_is_independent(self) -> None:
        probability = np.array([[0.5, 0.5, 0.5], [0.5, 0.5, 0.5]])
        edge = np.array([[0.01, 0.02, 0.03], [-0.01, -0.01, -0.01]])
        chosen = model.choose(probability, edge, GRID)
        self.assertEqual(chosen[0], 2.0)
        self.assertTrue(np.isnan(chosen[1]))


class _FakePanel:
    """2 只股票、14 天。第 0 天第二只明天跌得深。"""

    def __init__(self) -> None:
        self.dates = [f"201501{day:02d}" for day in range(1, 15)]
        self.codes = ["a", "b"]
        shape = (14, 2)
        self.traded = np.ones(shape, dtype=bool)
        self.one_price = np.zeros(shape, dtype=bool)
        self.universe = np.ones(shape, dtype=bool)
        self.open = np.full(shape, 10.0)
        self.low = np.full(shape, 9.95)
        self.low[1, 1] = 9.0

    def adjusted(self, name: str) -> np.ndarray:
        return {"open": self.open, "low": self.low}[name]


def _state() -> dict[str, np.ndarray]:
    shape = (14, 2)
    close = np.full(shape, 10.0)
    close[11, 1] = 11.0  # 第 0 天挂的单，第 1 天成交，10 天后收盘 11
    return {"close": close, "atr": np.full(shape, 0.5), "market": np.ones(14), "eligible": np.ones(shape, dtype=bool), "cell": np.zeros(shape, dtype=np.int8)}


class EvaluateTests(unittest.TestCase):
    def _run(self, matrix: np.ndarray):
        panel, state = _FakePanel(), _state()
        with unittest.mock.patch.object(model, "_usable", lambda _panel, _state, t: state["eligible"][t]):
            return model.evaluate_matrix(panel, state, matrix, "20150101", "20150103").loc["2015"]

    def test_a_fill_counts_the_move_after_the_fill_price(self) -> None:
        matrix = np.full((14, 2), np.nan)
        matrix[0, 1] = 1.0  # 10 - 0.5 = 9.5，第 1 天最低 9，成交价 9.5
        row = self._run(matrix)
        self.assertEqual(row["挂单"], 1)
        self.assertEqual(row["成交"], 1)
        self.assertAlmostEqual(row["价值"], 11.0 / 9.5 - 1 - BUY_COST)

    def test_no_order_is_worth_zero_and_still_counts_as_a_stock_day(self) -> None:
        row = self._run(np.full((14, 2), np.nan))
        self.assertEqual((row["挂单"], row["成交"], row["价值"]), (0, 0, 0))
        self.assertGreater(row["股票日"], 0)
        self.assertEqual(row["价值%"], 0)

    def test_an_untouched_price_earns_nothing(self) -> None:
        matrix = np.full((14, 2), np.nan)
        matrix[0, 0] = 1.0  # 这只第 1 天最低 9.95，碰不到 9.5
        row = self._run(matrix)
        self.assertEqual((row["挂单"], row["成交"], row["价值"]), (1, 0, 0))

    def test_the_same_stocks_are_also_priced_at_the_baseline(self) -> None:
        matrix = np.full((14, 2), np.nan)
        matrix[0, 1] = 1.0
        row = self._run(matrix)
        self.assertAlmostEqual(row["同股-1%价值"], 11.0 / 9.9 - 1 - BUY_COST)


class GuardTests(unittest.TestCase):
    def test_features_are_all_available_online(self) -> None:
        self.assertNotIn("limit_hits_20", model.ALL_FEATURES)
        self.assertEqual(len(set(model.ALL_FEATURES)), 14)

    def test_the_final_run_needs_explicit_permission(self) -> None:
        with unittest.mock.patch("sys.argv", ["x", "final", "--out", "/tmp/none", "--variant", "full14"]), self.assertRaises(SystemExit):
            model.main()


if __name__ == "__main__":
    unittest.main()
