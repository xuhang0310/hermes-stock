from __future__ import annotations

import math
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.quant.backtest_engine import (
    MacdBacktestConfig,
    get_backtest,
    run_macd_backtest,
    summary_markdown,
)
from backend.app.quant.portfolio_backtest import (
    PortfolioBacktestConfig,
    equal_risk_contribution_weights,
    run_portfolio_backtest,
)
from backend.app.agent_runtime.agentscope_engine import (
    _apply_builtin_backtest_route,
    _builtin_backtest_route,
)
from backend.app.skills.registry import SkillRegistry
from backend.app.skills.strategy_backtest import (
    run_backtest_tool,
    run_portfolio_backtest_tool,
)


class StrategyBacktestTests(unittest.TestCase):
    def setUp(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=320)
        closes = [
            10 + 0.008 * index + 0.75 * math.sin(index / 11) + 0.2 * math.sin(index / 3)
            for index in range(len(dates))
        ]
        self.frame = pd.DataFrame(
            {
                "trade_date": dates,
                "open": [value * 0.998 for value in closes],
                "high": [value * 1.015 for value in closes],
                "low": [value * 0.985 for value in closes],
                "close": closes,
                "volume": [1_000_000 + index * 1000 for index in range(len(dates))],
                "amount": [value * 1_000_000 / 10 for value in closes],
            }
        )
        self.config = MacdBacktestConfig(
            symbol="000001.SZ",
            start_date="20240101",
            end_date="20251231",
        )

    def test_macd_backtest_produces_consistent_report_contract(self) -> None:
        result = run_macd_backtest(self.config, self.frame)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["price_series"]), 320)
        self.assertEqual(len(result["equity_curve"]), 320)
        self.assertGreater(result["metrics"]["trade_count"], 2)
        self.assertEqual(len(result["trade_markers"]), result["metrics"]["trade_count"] * 2)
        self.assertTrue(all(trade["quantity"] % 100 == 0 for trade in result["trades"]))
        self.assertTrue(all(trade["entry_date"] <= trade["exit_date"] for trade in result["trades"]))
        self.assertEqual(len(result["data_fingerprint"]), 64)

    def test_short_sample_is_never_rated_healthy(self) -> None:
        short = self.frame.iloc[:80].copy()
        result = run_macd_backtest(self.config, short)
        self.assertEqual(result["confidence"], "insufficient")
        self.assertEqual(result["rating"], "样本不足")
        self.assertTrue(any("样本量不足" in item for item in result["warnings"]))

    def test_summary_links_to_single_run(self) -> None:
        result = run_macd_backtest(self.config, self.frame)
        markdown = summary_markdown("run-123", result)
        self.assertIn("/backtests/run-123", markdown)
        self.assertIn("MACD(12,26,9)", markdown)

    def test_persisted_result_is_owner_scoped(self) -> None:
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        result = run_macd_backtest(self.config, self.frame)
        result["run_id"] = "owned-run"
        result["report_url"] = "/backtests/owned-run"
        from backend.app.core.models import BacktestRun
        import json

        with factory() as db:
            db.add(
                BacktestRun(
                    run_id="owned-run",
                    owner_user_id=7,
                    status="completed",
                    symbol=self.config.symbol,
                    strategy_code="macd_cross",
                    timeframe="1D",
                    start_date=result["period"]["start_date"],
                    end_date=result["period"]["end_date"],
                    confidence=result["confidence"],
                    config_json=json.dumps(result["config"]),
                    metrics_json=json.dumps(result["metrics"]),
                    result_json=json.dumps(result),
                    summary_markdown="summary",
                )
            )
            db.commit()
            self.assertEqual(get_backtest("owned-run", owner_user_id=7, db=db)["run_id"], "owned-run")
            with self.assertRaises(KeyError):
                get_backtest("owned-run", owner_user_id=8, db=db)
        engine.dispose()

    def test_quant_role_receives_backtest_tool(self) -> None:
        registry = SkillRegistry()
        registry.discover()
        names = {
            item["function"]["name"]
            for item in registry.get_tools_for("research", role_code="quant-analyst")
        }
        self.assertIn("run_macd_backtest", names)
        self.assertIn("run_portfolio_backtest", names)

    def test_common_backtest_phrases_resolve_professional_skill(self) -> None:
        registry = SkillRegistry()
        for question in (
            "给 000001.SZ 做一次回归测试",
            "用历史数据测试这个 MACD 策略",
            "跑一下过去两年的收益测试",
            "用 000001.SZ、600519.SH、000858.SZ 构建风险平价组合并与等权比较",
        ):
            selected = registry.resolve_manifests(question, "research", "stock-pm")
            self.assertIn("strategy-backtest", {item.name for item in selected}, question)

    def test_backtest_tool_is_available_to_all_research_roles_and_stock_mode(self) -> None:
        registry = SkillRegistry()
        registry.discover()
        for role_code in ("stock-pm", "risk-officer", "research-analyst", "quant-analyst"):
            names = {
                item["function"]["name"]
                for item in registry.get_tools_for("research", role_code=role_code)
            }
            self.assertIn("run_macd_backtest", names, role_code)
            self.assertIn("run_portfolio_backtest", names, role_code)
        stock_names = {
            item["function"]["name"]
            for item in registry.get_tools_for("stock")
        }
        self.assertIn("run_macd_backtest", stock_names)
        self.assertIn("run_portfolio_backtest", stock_names)

    def test_agent_backtest_result_keeps_owner_and_parent_run(self) -> None:
        created = {
            "run_id": "report-run",
            "report_url": "/backtests/report-run",
            "summary_markdown": "summary",
            "strategy": {"name": "MACD"},
            "symbol": "000001.SZ",
            "period": {},
            "metrics": {},
            "confidence": "low",
            "rating": "低置信度",
            "warnings": [],
        }
        with patch("backend.app.skills.strategy_backtest.create_backtest", return_value=created) as mocked:
            result = run_backtest_tool(
                {"symbol": "000001.SZ"},
                owner_user_id=9,
                agent_run_id="agent-run",
            )
        mocked.assert_called_once_with(
            {"symbol": "000001.SZ"},
            owner_user_id=9,
            agent_run_id="agent-run",
        )
        self.assertEqual(result["run_id"], "report-run")

    def test_equal_risk_contribution_solver_equalizes_risk_budget(self) -> None:
        covariance = np.array(
            [
                [0.04, 0.006, 0.004],
                [0.006, 0.09, 0.008],
                [0.004, 0.008, 0.16],
            ]
        )
        weights = equal_risk_contribution_weights(covariance)
        contributions = weights * (covariance @ weights) / (weights @ covariance @ weights)
        self.assertAlmostEqual(float(weights.sum()), 1.0, places=8)
        self.assertTrue(np.all(weights > 0))
        self.assertTrue(np.allclose(contributions, np.full(3, 1 / 3), atol=1e-6))

    def test_portfolio_backtest_compares_risk_parity_with_equal_weight(self) -> None:
        dates = pd.bdate_range("2023-07-03", "2024-12-31")
        rng = np.random.default_rng(17)
        returns = rng.normal(
            loc=[0.00035, 0.00015, 0.00025],
            scale=[0.009, 0.018, 0.013],
            size=(len(dates), 3),
        )
        symbols = ("000001.SZ", "600519.SH", "000858.SZ")
        prices = pd.DataFrame(
            100 * np.cumprod(1 + returns, axis=0),
            index=dates,
            columns=symbols,
        )
        config = PortfolioBacktestConfig(
            symbols=symbols,
            start_date="20240101",
            end_date="20241231",
            rebalance_frequency="monthly",
        )
        result = run_portfolio_backtest(config, prices)
        self.assertEqual(result["report_type"], "portfolio")
        self.assertEqual(result["symbols"], list(symbols))
        self.assertEqual(len(result["rebalance_history"]), 12)
        self.assertEqual(result["metrics"]["rebalance_count"], 12)
        self.assertIn("benchmark_metrics", result)
        self.assertEqual(len(result["asset_curves"]), 3)
        contribution_pnl = sum(
            row["pnl_contribution"] for row in result["attribution"]["asset_stats"]
        )
        expected_net = contribution_pnl - result["metrics"]["total_cost"]
        actual_net = result["metrics"]["final_equity"] - config.initial_cash
        self.assertAlmostEqual(expected_net, actual_net, delta=5.0)

    def test_builtin_portfolio_route_removes_workspace_python(self) -> None:
        question = (
            "用000001.SZ、600519.SH、000858.SZ构建风险平价组合，"
            "回测2024全年，与等权基准对比"
        )
        self.assertEqual(_builtin_backtest_route(question), "run_portfolio_backtest")
        schemas = [
            {"function": {"name": "run_portfolio_backtest"}},
            {"function": {"name": "run_macd_backtest"}},
            {"function": {"name": "ws_run_python"}},
            {"function": {"name": "ws_write_file"}},
        ]
        prompt, routed = _apply_builtin_backtest_route("base", schemas, question)
        names = {item["function"]["name"] for item in routed}
        self.assertEqual(names, {"run_portfolio_backtest"})
        self.assertIn("禁止编写、修改或执行临时 Python", prompt)

    def test_agent_portfolio_result_keeps_owner_and_parent_run(self) -> None:
        created = {
            "run_id": "portfolio-report",
            "report_url": "/backtests/portfolio-report",
            "summary_markdown": "summary",
            "strategy": {"name": "风险平价组合"},
            "symbol": "组合(3)",
            "symbols": ["000001.SZ", "600519.SH", "000858.SZ"],
            "period": {},
            "metrics": {},
            "benchmark_metrics": {},
            "confidence": "medium",
            "rating": "中等置信度",
            "warnings": [],
        }
        args = {
            "symbols": ["000001.SZ", "600519.SH", "000858.SZ"],
            "start_date": "20240101",
            "end_date": "20241231",
        }
        with patch("backend.app.skills.strategy_backtest.create_backtest", return_value=created) as mocked:
            result = run_portfolio_backtest_tool(
                args,
                owner_user_id=9,
                agent_run_id="agent-run",
            )
        mocked.assert_called_once_with(
            {**args, "strategy": "risk_parity"},
            owner_user_id=9,
            agent_run_id="agent-run",
        )
        self.assertEqual(result["run_id"], "portfolio-report")


if __name__ == "__main__":
    unittest.main()
