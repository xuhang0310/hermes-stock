from __future__ import annotations

import math
import unittest
from unittest.mock import patch

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.core.models import DailyBar, Instrument
from backend.app.quant.factor_service import (
    benchmark_factors,
    factor_health,
    get_factor,
    list_factors,
    score_factor,
)
from backend.app.skills.registry import SkillRegistry


class FactorResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.symbols = [f"6000{index:02d}.SH" for index in range(1, 7)]
        dates = pd.bdate_range("2025-01-02", periods=100)
        with self.SessionFactory() as db:
            for index, symbol in enumerate(self.symbols):
                db.add(
                    Instrument(
                        symbol=symbol,
                        name=f"Test {index}",
                        market="CN",
                        type="stock",
                    )
                )
                for offset, trade_date in enumerate(dates):
                    trend = 0.0008 * offset * (index + 1)
                    cycle = 0.02 * math.sin((offset + index) / 7)
                    close = 10.0 + index + trend + cycle
                    db.add(
                        DailyBar(
                            symbol=symbol,
                            trade_date=trade_date.strftime("%Y%m%d"),
                            open=close * 0.998,
                            high=close * 1.012,
                            low=close * 0.988,
                            close=close,
                            pre_close=close * 0.999,
                            change_pct=0.1,
                            volume=100000 + offset * 100 + index * 1000,
                            amount=close * (100000 + offset * 100 + index * 1000) / 10,
                            source="tushare",
                        )
                    )
            db.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_registry_contains_all_vendored_factors(self) -> None:
        health = factor_health()
        self.assertEqual(health["loaded"], 462)
        self.assertEqual(health["failed"], 0)
        self.assertEqual(health["zoos"]["alpha101"], 101)
        self.assertEqual(health["zoos"]["gtja191"], 191)
        self.assertEqual(health["zoos"]["qlib158"], 154)

        result = list_factors(zoo="gtja191", limit=3)
        self.assertEqual(result["total"], 191)
        self.assertEqual(len(result["items"]), 3)
        self.assertIn("formula_latex", get_factor(result["items"][0]["id"]))

    def test_scores_factor_from_hermes_daily_bars(self) -> None:
        with patch("backend.app.quant.factor_service.SessionLocal", self.SessionFactory):
            result = score_factor(
                "qlib158_roc5",
                self.symbols,
                start_date="20250101",
                end_date="20251231",
            )
        self.assertEqual(len(result["scores"]), len(self.symbols))
        self.assertEqual(result["coverage"]["dates"], 100)
        self.assertEqual(result["scores"][0]["percentile"], 1.0)
        self.assertIn("不能直接", result["interpretation"])

    def test_benchmarks_factor_with_ic_ir(self) -> None:
        with patch("backend.app.quant.factor_service.SessionLocal", self.SessionFactory):
            result = benchmark_factors(
                ["qlib158_roc5", "qlib158_ma5"],
                self.symbols,
                start_date="20250101",
                end_date="20251231",
            )
        self.assertEqual(len(result["results"]), 2)
        self.assertFalse(result["skipped"])
        self.assertTrue(all(item["ic_count"] > 0 for item in result["results"]))
        self.assertTrue(all(item["category"] in {"alive", "reversed", "dead"} for item in result["results"]))

    def test_skill_registry_exposes_factor_tools_to_quant_role(self) -> None:
        registry = SkillRegistry()
        registry.discover()
        tool_names = {
            item["function"]["name"]
            for item in registry.get_tools_for("research", role_code="quant-analyst")
        }
        self.assertIn("list_factor_alphas", tool_names)
        self.assertIn("score_factor_universe", tool_names)
        self.assertTrue(registry.has_tool("benchmark_factor_alphas"))


if __name__ == "__main__":
    unittest.main()

