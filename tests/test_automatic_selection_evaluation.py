from __future__ import annotations

import unittest

from backend.app.quant.automatic_selection_evaluation import (
    calculate_turnover,
    evaluate_forward_window,
)

TARGET_DATE = "20260828"
DATES = ["20260831", "20260901", "20260902", "20260903", "20260904"]


def _bars(symbol: str, *, entry: float, closes: list[float]):
    return [
        {
            "symbol": symbol,
            "trade_date": trade_date,
            "open": entry if index == 0 else closes[index - 1],
            "close": close,
        }
        for index, (trade_date, close) in enumerate(zip(DATES, closes))
    ]


class AutomaticSelectionEvaluationTests(unittest.TestCase):
    def test_window_stays_pending_until_horizon_matures(self) -> None:
        result = evaluate_forward_window(
            target_trade_date=TARGET_DATE,
            horizon=5,
            selected_symbols=["600001.SH"],
            benchmark_symbols=["600001.SH"],
            bars=[],
            trading_dates=DATES[:4],
            min_coverage=0.8,
            commission_rate=0.0,
            slippage_rate=0.0,
            turnover=1.0,
        )

        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["entry_trade_date"], DATES[0])
        self.assertIsNone(result["exit_trade_date"])
        self.assertIn("尚未达到", result["error"])

    def test_uses_next_trade_day_open_and_fifth_close_with_costs(self) -> None:
        bars = [
            *_bars("600001.SH", entry=10.0, closes=[10.0, 10.5, 11.0, 11.5, 12.0]),
            *_bars("600002.SH", entry=20.0, closes=[20.0, 20.0, 20.0, 20.0, 20.0]),
        ]

        result = evaluate_forward_window(
            target_trade_date=TARGET_DATE,
            horizon=5,
            selected_symbols=["600001.SH"],
            benchmark_symbols=["600002.SH"],
            bars=bars,
            trading_dates=DATES,
            min_coverage=1.0,
            commission_rate=0.001,
            slippage_rate=0.001,
            turnover=0.5,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["entry_trade_date"], DATES[0])
        self.assertEqual(result["exit_trade_date"], DATES[-1])
        expected_selected = (1 - 0.002) * (12.0 / 10.0) * (1 - 0.002) - 1
        expected_benchmark = (1 - 0.002) * (1 - 0.002) - 1
        self.assertAlmostEqual(result["basket_return"], expected_selected)
        self.assertAlmostEqual(result["benchmark_return"], expected_benchmark)
        self.assertAlmostEqual(result["excess_return"], expected_selected - expected_benchmark)
        self.assertEqual(len(result["daily_series"]), 5)
        self.assertEqual(result["per_symbol"][0]["entry_price"], 10.0)
        self.assertEqual(result["per_symbol"][0]["exit_price"], 12.0)

    def test_missing_symbol_data_is_disclosed_and_can_block_metrics(self) -> None:
        bars = _bars("600001.SH", entry=10.0, closes=[10.0, 10.1, 10.2, 10.3, 10.4])

        result = evaluate_forward_window(
            target_trade_date=TARGET_DATE,
            horizon=5,
            selected_symbols=["600001.SH", "600002.SH"],
            benchmark_symbols=["600001.SH", "600002.SH"],
            bars=bars,
            trading_dates=DATES,
            min_coverage=0.8,
            commission_rate=0.0,
            slippage_rate=0.0,
            turnover=1.0,
        )

        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["coverage_ratio"], 0.5)
        self.assertEqual(result["benchmark_coverage_ratio"], 0.5)
        self.assertIsNone(result["basket_return"])
        missing = next(item for item in result["per_symbol"] if item["symbol"] == "600002.SH")
        self.assertFalse(missing["covered"])
        self.assertEqual(missing["missing_trade_dates"], DATES)

    def test_missing_intermediate_bar_is_not_silently_ignored(self) -> None:
        bars = _bars("600001.SH", entry=10.0, closes=[10.0, 10.1, 10.2, 10.3, 10.4])
        bars = [item for item in bars if item["trade_date"] != DATES[2]]

        result = evaluate_forward_window(
            target_trade_date=TARGET_DATE,
            horizon=5,
            selected_symbols=["600001.SH"],
            benchmark_symbols=["600001.SH"],
            bars=bars,
            trading_dates=DATES,
            min_coverage=1.0,
            commission_rate=0.0,
            slippage_rate=0.0,
            turnover=0.0,
        )

        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["per_symbol"][0]["missing_trade_dates"], [DATES[2]])

    def test_turnover_compares_adjacent_top_n_memberships(self) -> None:
        self.assertEqual(calculate_turnover([], []), 0.0)
        self.assertEqual(calculate_turnover(["A", "B"], []), 1.0)
        self.assertEqual(calculate_turnover(["A", "B"], ["A", "C"]), 0.5)
        self.assertEqual(calculate_turnover(["A", "B"], ["A", "B"]), 0.0)

    def test_rejects_unsupported_horizon_or_invalid_cost(self) -> None:
        common = {
            "target_trade_date": TARGET_DATE,
            "selected_symbols": ["A"],
            "benchmark_symbols": ["A"],
            "bars": [],
            "trading_dates": DATES,
            "min_coverage": 1.0,
            "commission_rate": 0.0,
            "slippage_rate": 0.0,
            "turnover": 0.0,
        }
        with self.assertRaises(ValueError):
            evaluate_forward_window(horizon=10, **common)
        common["commission_rate"] = -0.1
        with self.assertRaises(ValueError):
            evaluate_forward_window(horizon=5, **common)


if __name__ == "__main__":
    unittest.main()
