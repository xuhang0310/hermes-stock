from __future__ import annotations

import math
import unittest
from unittest.mock import patch

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
from backend.app.skills.registry import SkillRegistry
from backend.app.skills.strategy_backtest import run_backtest_tool


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

    def test_common_backtest_phrases_resolve_professional_skill(self) -> None:
        registry = SkillRegistry()
        for question in (
            "给 000001.SZ 做一次回归测试",
            "用历史数据测试这个 MACD 策略",
            "跑一下过去两年的收益测试",
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
        stock_names = {
            item["function"]["name"]
            for item in registry.get_tools_for("stock")
        }
        self.assertIn("run_macd_backtest", stock_names)

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


if __name__ == "__main__":
    unittest.main()
