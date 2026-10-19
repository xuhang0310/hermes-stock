from __future__ import annotations

import copy
import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from backend.app.core.models import (
    AutomaticSelectionRun,
    DailyBar,
    Instrument,
    MarketTradingCalendar,
    PaperOrder,
    StockAdjFactor,
    StockDailyBasic,
)
from backend.app.paper import paper_service
from backend.app.market import market_sync
from backend.app.quant import tushare_factor_service
from tests.automatic_selection_fixtures import history_dates
from backend.app.quant import automatic_selection_repository as repository
from backend.app.quant import automatic_selection_service as service
from backend.app.quant.automatic_selection_scoring import DEFAULT_STRATEGY_CONFIG
from tests.automatic_selection_mysql import (
    create_isolated_mysql_engine,
    reset_isolated_schema,
)

TARGET_DATE = "20260828"
NEXT_DATE = "20260831"


def _config(*, top_n: int = 2):
    config = copy.deepcopy(DEFAULT_STRATEGY_CONFIG)
    config["top_n"] = top_n
    config["feature_weights"] = {"value_pe": 1.0}
    config["constraints"] = {"max_per_industry": 0, "max_per_market_cap_bucket": 0}
    config["quality"] = {
        "min_bar_coverage": 1.0,
        "min_basic_coverage": 1.0,
        "min_industry_coverage": 0.0,
        "min_factor_coverage": 1.0,
        "min_eligible_count": 1,
        "min_evaluation_coverage": 0.8,
    }
    return config


def _history(start: float = 10.0, *, target_trade_date: str = TARGET_DATE):
    return [
        {"trade_date": trade_date, "close": start + index * 0.1, "adj_factor": 1.0, "amount": 100_000.0}
        for index, trade_date in enumerate(history_dates(target_trade_date))
    ]


def _row(symbol: str, *, pe_ttm: float = 10.0, close_price: float = 12.0, factors=None, target_trade_date: str = TARGET_DATE):
    return {
        "symbol": symbol,
        "name": f"测试{symbol}",
        "market": "主板",
        "list_status": "L",
        "list_date": "20100101",
        "industry": "银行",
        "close_price": close_price,
        "pct_chg": 1.0,
        "pe_ttm": pe_ttm,
        "pb": 1.0,
        "total_mv": 1000.0 + pe_ttm,
        "circ_mv": 800.0,
        "history": _history(target_trade_date=target_trade_date),
        "expected_history_dates": history_dates(target_trade_date),
        "factor_values": factors or {},
    }


class AutomaticSelectionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_isolated_mysql_engine()
        reset_isolated_schema(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.repo_patch = patch.object(repository, "SessionLocal", self.SessionFactory)
        self.service_db_patch = patch.object(service, "SessionLocal", self.SessionFactory)
        self.repo_patch.start()
        self.service_db_patch.start()
        self.addCleanup(self.repo_patch.stop)
        self.addCleanup(self.service_db_patch.stop)
        for module in (market_sync, tushare_factor_service):
            db_patch = patch.object(module, "SessionLocal", self.SessionFactory)
            db_patch.start()
            self.addCleanup(db_patch.stop)
        catalog_patch = patch.object(tushare_factor_service, "ensure_catalog")
        catalog_patch.start()
        self.addCleanup(catalog_patch.stop)
        names_patch = patch.object(tushare_factor_service, "known_factor_names", return_value={"roe_ttm"})
        names_patch.start()
        self.addCleanup(names_patch.stop)
        self.strategy = repository.create_strategy(
            strategy_key="test-strategy",
            name="测试策略",
            config=_config(),
            lifecycle_state="challenger",
            actor_user_id=1,
        )

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_quality_gate_blocks_run_without_silent_factor_drop(self) -> None:
        factor_config = _config()
        factor_config["tushare_factors"] = [{"id": "tushare:roe_ttm", "weight": 1.0}]
        strategy = repository.create_strategy(
            strategy_key="factor-strategy",
            name="严格因子策略",
            config=factor_config,
            lifecycle_state="shadow",
            actor_user_id=1,
        )

        result = service.run_strategy_from_rows(
            strategy,
            [_row("600001.SH")],
            target_trade_date=TARGET_DATE,
            trigger_type="test",
            generate_explanation=False,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["selected_count"], 0)
        self.assertIn("factor_coverage:tushare:roe_ttm", {
            item["code"] for item in result["quality_gate"]["failures"]
        })
        detail = repository.run_detail(result["run_id"], candidate_scope="all")
        self.assertIn("required_factor_missing", detail["candidates"][0]["exclusion_codes"])
        self.assertEqual(detail["evaluations"], [])

    def test_full_evidence_and_membership_changes_include_missing_previous_stock(self) -> None:
        first = service.run_strategy_from_rows(
            self.strategy,
            [_row("600001.SH", pe_ttm=5.0), _row("600002.SH", pe_ttm=6.0)],
            target_trade_date=TARGET_DATE,
            trigger_type="test",
            generate_explanation=False,
        )
        second = service.run_strategy_from_rows(
            self.strategy,
            [_row("600001.SH", pe_ttm=5.0, target_trade_date=NEXT_DATE), _row("600003.SH", pe_ttm=7.0, target_trade_date=NEXT_DATE)],
            target_trade_date=NEXT_DATE,
            trigger_type="test",
            generate_explanation=False,
        )
        detail = repository.run_detail(second["run_id"], candidate_scope="all")
        by_symbol = {item["symbol"]: item for item in detail["candidates"]}

        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["universe_count"], 2)
        self.assertEqual(by_symbol["600001.SH"]["change_state"], "retained")
        self.assertEqual(by_symbol["600003.SH"]["change_state"], "added")
        self.assertEqual(by_symbol["600002.SH"]["change_state"], "removed")
        self.assertEqual(by_symbol["600002.SH"]["exclusion_codes"], ["not_in_current_catalog"])
        self.assertEqual(second["sync_summary"]["membership_changes"], {
            "added": 1,
            "retained": 1,
            "removed": 1,
        })
        self.assertEqual(len(detail["evaluations"]), 3)

    def test_identical_inputs_are_idempotent(self) -> None:
        rows = [_row("600001.SH"), _row("600002.SH", pe_ttm=12.0)]
        first = service.run_strategy_from_rows(
            self.strategy,
            rows,
            target_trade_date=TARGET_DATE,
            trigger_type="test",
            generate_explanation=False,
        )
        second = service.run_strategy_from_rows(
            self.strategy,
            list(reversed(rows)),
            target_trade_date=TARGET_DATE,
            trigger_type="test",
            generate_explanation=False,
        )

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["run_id"], second["run_id"])
        with self.SessionFactory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(AutomaticSelectionRun)), 1)

    def test_changed_input_supersedes_same_day_run(self) -> None:
        first = service.run_strategy_from_rows(
            self.strategy,
            [_row("600001.SH", close_price=10.0)],
            target_trade_date=TARGET_DATE,
            trigger_type="test",
            generate_explanation=False,
        )
        second = service.run_strategy_from_rows(
            self.strategy,
            [_row("600001.SH", close_price=10.5)],
            target_trade_date=TARGET_DATE,
            trigger_type="test",
            generate_explanation=False,
        )

        self.assertTrue(second["created"])
        self.assertNotEqual(first["data_fingerprint"], second["data_fingerprint"])
        self.assertEqual(second["supersedes_run_id"], first["run_id"])

    def test_llm_failure_does_not_change_completed_ranking_or_place_orders(self) -> None:
        with (
            patch.object(service, "active_llm_config", side_effect=RuntimeError("LLM 不可用")),
            patch.object(paper_service, "place_order") as place_order,
        ):
            result = service.run_strategy_from_rows(
                self.strategy,
                [_row("600001.SH"), _row("600002.SH", pe_ttm=12.0)],
                target_trade_date=TARGET_DATE,
                trigger_type="test",
                generate_explanation=True,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["llm_status"], "failed")
        self.assertEqual(result["selected_count"], 2)
        self.assertIn("LLM 不可用", result["llm_error"])
        place_order.assert_not_called()
        with self.SessionFactory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(PaperOrder)), 0)

    def test_llm_receives_only_post_persistence_evidence(self) -> None:
        response = {"choices": [{"message": {"content": "仅解释已落库证据，不构成投资建议。"}}]}
        with (
            patch.object(service, "active_llm_config", return_value={
                "api_key": "test",
                "base_url": "https://example.invalid",
                "model": "test-model",
                "display_name": "测试模型",
                "reasoning_effort": "off",
            }),
            patch.object(service, "reasoning_params", return_value={
                "enable_thinking": None,
                "reasoning_effort": "none",
            }),
            patch.object(service, "chat_completion", return_value=response) as completion,
        ):
            result = service.run_strategy_from_rows(
                self.strategy,
                [_row("600001.SH")],
                target_trade_date=TARGET_DATE,
                trigger_type="test",
                generate_explanation=True,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["llm_status"], "completed")
        self.assertEqual(result["llm_model"], "测试模型")
        self.assertIn("不构成投资建议", result["llm_explanation"])
        self.assertEqual(completion.call_args.kwargs["log_module"], "automatic_selection")
        with self.SessionFactory() as db:
            self.assertIsNotNone(db.get(AutomaticSelectionRun, result["run_id"]))

    def test_strategy_failure_is_persisted_without_evaluations(self) -> None:
        with patch.object(service, "score_candidates", side_effect=RuntimeError("评分失败")):
            summary = service.run_strategies_from_rows(
                [self.strategy],
                [_row("600001.SH")],
                target_trade_date=TARGET_DATE,
                trigger_type="test",
                generate_explanation=False,
            )

        self.assertEqual(summary["failed_count"], 1)
        failed = summary["runs"][0]
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["created"])
        self.assertIn("评分失败", failed["error"])
        detail = repository.run_detail(failed["run_id"], candidate_scope="all")
        self.assertEqual(detail["stage"], "failed")
        self.assertEqual(detail["quality_gate"]["metrics"], {})
        self.assertIn("min_eligible_count", detail["quality_gate"]["thresholds"])
        self.assertEqual(detail["evaluations"], [])

    def test_nullable_factor_values_are_ignored_without_crashing(self) -> None:
        panel = pd.DataFrame(
            {
                "600001.SH": [pd.NA],
                "600002.SH": [float("nan")],
                "600003.SH": ["0.2"],
            },
            index=pd.to_datetime([TARGET_DATE]),
        )
        with patch.object(service, "load_factor_panel", return_value=panel):
            values = service._load_target_factor_values(["tushare:roe_ttm"], TARGET_DATE)

        self.assertEqual(values, {"tushare:roe_ttm": {"600003.SH": 0.2}})

    def test_market_rows_are_assembled_from_one_target_snapshot(self) -> None:
        symbol = "600001.SH"
        dates = history_dates(TARGET_DATE)
        with self.SessionFactory() as db:
            db.add(Instrument(symbol=symbol, name="测试一", market="CN", type="stock"))
            db.flush()  # 无 relationship 时 UoW 不保证先插父表，MySQL 会拒绝 daily_bars 外键
            for index, trade_date in enumerate(dates):
                db.add(
                    DailyBar(
                        symbol=symbol,
                        trade_date=trade_date,
                        open=10.0 + index,
                        close=10.5 + index,
                        change_pct=1.5,
                        amount=100_000.0,
                        source="tushare",
                    )
                )
                if index != 3:
                    db.add(StockAdjFactor(symbol=symbol, trade_date=trade_date, adj_factor=1.0 + index / 100))
            db.add(
                StockDailyBasic(
                    symbol=symbol,
                    trade_date=TARGET_DATE,
                    pe_ttm=9.0,
                    pb=1.2,
                    total_mv=5000.0,
                    circ_mv=4000.0,
                )
            )
            db.commit()

        rows = service.load_candidate_input_rows(
            [{
                "symbol": symbol,
                "name": "测试一",
                "market": "主板",
                "list_status": "L",
                "list_date": "20100101",
                "industry": "银行",
            }],
            target_trade_date=TARGET_DATE,
            history_days=20,
            expected_history_dates=dates,
            factor_values={"tushare:roe_ttm": {symbol: 0.2}},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["history"]), 21)
        self.assertEqual(rows[0]["close_price"], 30.5)
        self.assertEqual(rows[0]["pe_ttm"], 9.0)
        self.assertEqual(rows[0]["factor_values"], {"tushare:roe_ttm": 0.2})
        factors = [item["adj_factor"] for item in rows[0]["history"]]
        self.assertIsNone(factors[3])
        self.assertAlmostEqual(factors[-1], 1.2)

    def test_due_evaluation_updates_five_day_window_and_leaves_longer_windows_pending(self) -> None:
        run = service.run_strategy_from_rows(
            self.strategy,
            [_row("600001.SH")],
            target_trade_date=TARGET_DATE,
            trigger_type="test",
            generate_explanation=False,
        )
        future_dates = ["20260831", "20260901", "20260902", "20260903", "20260904"]
        with self.SessionFactory() as db:
            db.add(Instrument(symbol="600001.SH", name="测试一", market="CN", type="stock"))
            db.flush()
            for day, is_open in [(TARGET_DATE, True), ("20260829", False), ("20260830", False)]:
                db.add(MarketTradingCalendar(exchange="SSE", cal_date=day, is_open=is_open))
            for index, trade_date in enumerate(future_dates):
                db.add(MarketTradingCalendar(exchange="SSE", cal_date=trade_date, is_open=True))
                db.add(
                    DailyBar(
                        symbol="600001.SH",
                        trade_date=trade_date,
                        open=10.0 + index,
                        close=10.5 + index,
                        amount=100_000.0,
                        source="tushare",
                    )
                )
                db.add(StockAdjFactor(symbol="600001.SH", trade_date=trade_date, adj_factor=1.0))
            db.commit()

        class EvaluationDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 9, 4)

        with patch.object(market_sync, "date", EvaluationDate):
            summary = service.update_due_evaluations()
        detail = repository.run_detail(run["run_id"], candidate_scope="top")
        statuses = {item["horizon"]: item["status"] for item in detail["evaluations"]}

        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["pending"], 2)
        self.assertEqual(statuses, {5: "completed", 20: "pending", 60: "pending"})
        five_day = next(item for item in detail["evaluations"] if item["horizon"] == 5)
        self.assertEqual(five_day["entry_trade_date"], future_dates[0])
        self.assertEqual(five_day["exit_trade_date"], future_dates[-1])
        self.assertIsNotNone(five_day["basket_return"])

    def test_cycle_reads_local_cache_and_persists_snapshot_and_ranking(self) -> None:
        symbol = "600001.SH"
        dates = history_dates(TARGET_DATE)
        with self.SessionFactory() as db:
            db.add(Instrument(symbol=symbol, name="测试一", market="CN", type="stock"))
            db.flush()
            for index, trade_date in enumerate(dates):
                db.add(
                    DailyBar(
                        symbol=symbol,
                        trade_date=trade_date,
                        open=10.0 + index,
                        close=10.5 + index,
                        change_pct=1.0,
                        amount=100_000.0,
                        source="tushare",
                    )
                )
                db.add(StockAdjFactor(symbol=symbol, trade_date=trade_date, adj_factor=1.0))
            db.add(
                StockDailyBasic(
                    symbol=symbol,
                    trade_date=TARGET_DATE,
                    pe_ttm=10.0,
                    pb=1.0,
                    total_mv=1000.0,
                    circ_mv=800.0,
                )
            )
            db.commit()
        strategy_model = repository.get_strategy_model(self.strategy["id"])
        catalog = [{
            "symbol": symbol,
            "name": "测试一",
            "market": "主板",
            "stock_type": "stock",
            "list_status": "L",
            "list_date": "20100101",
            "industry": "银行",
            "source": "tushare.stock_basic",
        }]
        with (
            patch.object(repository, "ensure_default_strategy", return_value=self.strategy),
            patch.object(repository, "active_strategy_models", return_value=[strategy_model]),
            patch.object(service, "sync_market") as market_sync,
            patch.object(service, "local_selection_cache_status", return_value={
                "ready": True, "expected_trade_date": TARGET_DATE,
                "latest_trade_date": TARGET_DATE, "expected_history_dates": dates,
            }),
            patch.object(service, "_completed_ingestion_evidence", return_value={"job_run_id": "ingested"}),
            patch.object(service, "load_local_stock_catalog_snapshot", return_value=catalog),
            patch.object(service, "update_due_evaluations", return_value={
                "updated": 0,
                "completed": 0,
                "insufficient_data": 0,
                "pending": 0,
            }),
            patch.object(service, "_generate_explanation"),
        ):
            result = service.run_automatic_selection_cycle(trigger_type="test", job_run_id="job-1")

        market_sync.assert_not_called()
        self.assertEqual(result["target_trade_date"], TARGET_DATE)
        self.assertEqual(result["completed_count"], 1)
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(repository.latest_stock_snapshots(TARGET_DATE)[0]["symbol"], symbol)
        stored = repository.list_runs(limit=10)
        self.assertEqual(stored[0]["job_run_id"], "job-1")


if __name__ == "__main__":
    unittest.main()
