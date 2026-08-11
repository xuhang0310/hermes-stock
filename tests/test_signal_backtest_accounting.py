from __future__ import annotations

import math
import unittest
from unittest.mock import patch

import pandas as pd

from backend.app.quant.backtest_models import Position
from backend.app.quant.backtest_runner import run_signal_backtest
from backend.app.quant.engines.china_a import ChinaAEngine


class SignalBacktestAccountingTests(unittest.TestCase):
    def test_mark_to_market_keeps_invested_principal_in_equity(self) -> None:
        engine = ChinaAEngine({"initial_cash": 1_000_000})
        engine.capital = 0.0
        engine.positions["601609.SH"] = Position(
            symbol="601609.SH",
            direction=1,
            entry_price=6.22,
            entry_time=pd.Timestamp("2024-10-11"),
            size=160_800,
        )
        prices = pd.DataFrame(
            {"601609.SH": [6.28]},
            index=[pd.Timestamp("2024-11-05")],
        )

        equity = engine._calc_equity(
            prices,
            prices.index[0],
            0,
            prices.values,
            {"601609.SH": 0},
        )

        self.assertAlmostEqual(equity, 160_800 * 6.28, places=6)

    def test_signal_report_uses_decimal_net_returns_and_real_benchmark(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=160)
        closes = [10 + 0.018 * index + 0.8 * math.sin(index / 4) for index in range(len(dates))]
        frame = pd.DataFrame(
            {
                "open": [value * 0.998 for value in closes],
                "high": [value * 1.012 for value in closes],
                "low": [value * 0.988 for value in closes],
                "close": closes,
                "volume": [1_000_000 + index * 1_000 for index in range(len(dates))],
            },
            index=dates,
        )

        with (
            patch(
                "backend.app.quant.backtest_runner._load_stock_data",
                return_value={"601609.SH": frame},
            ),
            patch(
                "backend.app.quant.backtest_runner._save_backtest_result",
                side_effect=lambda result, **_: result,
            ),
        ):
            result = run_signal_backtest(
                "ma_cross",
                ["601609.SH"],
                "20240102",
                "20240812",
                strategy_params={"short_period": 3, "long_period": 8},
            )

        self.assertGreater(len(result["trades"]), 0)
        for trade in result["trades"]:
            self.assertGreater(trade["fees"], 0)
            self.assertAlmostEqual(
                trade["pnl"],
                trade["gross_pnl"] - trade["fees"],
                delta=0.02,
            )
            self.assertLess(abs(trade["return_pct"]), 1)

        metrics = result["metrics"]
        expected_benchmark = closes[-1] / closes[0] - 1
        self.assertAlmostEqual(metrics["benchmark_return"], expected_benchmark, places=8)
        self.assertAlmostEqual(
            metrics["excess_return"],
            metrics["total_return"] - metrics["benchmark_return"],
            places=8,
        )
        self.assertGreaterEqual(metrics["max_drawdown"], -1)
        self.assertLess(metrics["annual_volatility"], 2)
        self.assertEqual(result["benchmark_label"], "标的买入持有")


if __name__ == "__main__":
    unittest.main()
