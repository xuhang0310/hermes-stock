from __future__ import annotations

import copy
import json
import unittest
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy.engine import Engine

from backend.app.quant import automatic_selection_repository as repository
from backend.app.quant import automatic_selection_service as service
from backend.app.quant.automatic_selection_scoring import (
    DEFAULT_STRATEGY_CONFIG, SCORING_IMPLEMENTATION_VERSION, config_hash, validate_config,
)
from tests.automatic_selection_fixtures import history_dates

TARGET = "20260828"
CATALOG = [{
    "symbol": "600001.SH", "name": "冻结名称", "market": "主板", "industry": "银行",
    "list_status": "L", "list_date": "20100101", "stock_type": "stock",
}]


def strategy():
    config = copy.deepcopy(DEFAULT_STRATEGY_CONFIG)
    config["feature_weights"] = {"value_pe": 1.0}
    config["constraints"] = {"max_per_industry": 0, "max_per_market_cap_bucket": 0}
    config["quality"]["min_eligible_count"] = 1
    config = validate_config(config)
    return {"id": 1, "strategy_key": "test-strategy", "name": "测试", "version": 1,
            "config": config, "config_hash": config_hash(config)}


class AutomaticSelectionOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch.object(Engine, "connect", side_effect=AssertionError("禁止连接数据库"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def mock(self, obj, name, **kwargs):
        replacement = patch.object(obj, name, **kwargs)
        value = replacement.start()
        self.addCleanup(replacement.stop)
        return value

    def prepare_cycle(self):
        self.locked = False
        self.events = []

        @contextmanager
        def lock():
            self.assertFalse(self.locked)
            self.locked = True
            self.events.append("lock")
            try:
                yield
            finally:
                self.locked = False
                self.events.append("unlock")

        self.mock(service, "_selection_lock", side_effect=lock)
        self.mock(repository, "ensure_default_strategy")
        self.mock(repository, "active_strategy_models", return_value=[strategy()])
        self.mock(repository, "strategy_to_dict", side_effect=lambda value: value)
        self.mock(service, "update_due_evaluations", side_effect=lambda: self.outside_lock("evaluate"))
        self.cache = self.mock(service, "local_selection_cache_status", side_effect=lambda: self.inside_lock({
            "ready": True, "expected_trade_date": TARGET, "latest_trade_date": TARGET,
            "expected_history_dates": history_dates(TARGET),
        }))
        self.ingestion = self.mock(service, "_completed_ingestion_evidence", side_effect=lambda target: self.inside_lock({"job_run_id": "data-1"}))
        self.snapshots = self.mock(repository, "latest_stock_snapshots", side_effect=lambda target: self.inside_lock(CATALOG))
        self.catalog = self.mock(service, "load_local_stock_catalog_snapshot", return_value=CATALOG)
        self.freeze = self.mock(repository, "upsert_stock_snapshots", return_value=1)
        self.factors = self.mock(service, "_load_target_factor_values", side_effect=lambda *args: self.inside_lock({}))
        self.rows = self.mock(service, "load_candidate_input_rows", side_effect=lambda *args, **kwargs: self.inside_lock([]))
        self.rank = self.mock(service, "run_strategies_from_rows", side_effect=lambda *args, **kwargs: self.inside_lock({"runs": [], "completed_count": 1}))
        self.explain = self.mock(service, "_explain_completed_runs", side_effect=lambda result: self.outside_lock("explain"))
        self.fail = self.mock(service, "_persist_failed_strategy_run", side_effect=lambda item, **kwargs: {
            "status": "failed", "error": str(kwargs["error"]), "target_trade_date": kwargs["target_trade_date"],
        })
        self.remote = self.mock(service, "sync_market", side_effect=AssertionError("排名不得外呼"))

    def inside_lock(self, value):
        self.assertTrue(self.locked)
        return value

    def outside_lock(self, label):
        self.assertFalse(self.locked)
        self.events.append(label)
        return {"updated": 0}

    def test_all_input_reads_and_ranking_are_locked_but_evaluation_and_llm_are_not(self):
        self.prepare_cycle()
        result = service.run_automatic_selection_cycle(trigger_type="manual", job_run_id="job-1")
        self.assertEqual(result["completed_count"], 1)
        self.assertEqual(self.events, ["evaluate", "lock", "unlock", "explain"])
        self.remote.assert_not_called()
        self.catalog.assert_not_called()
        self.freeze.assert_not_called()
        self.rows.assert_called_once_with(CATALOG, target_trade_date=TARGET, history_days=20,
                                          expected_history_dates=history_dates(TARGET), factor_values={})
        self.assertTrue(self.rank.call_args.kwargs["defer_explanation"])

    def test_first_observation_is_frozen_then_reread_before_ranking(self):
        self.prepare_cycle()
        self.snapshots.side_effect = [[], CATALOG]
        mutable = [{**CATALOG[0], "name": "可变目录"}]
        self.catalog.return_value = mutable
        service.run_automatic_selection_cycle()
        self.freeze.assert_called_once_with(TARGET, mutable)
        self.assertEqual(self.rows.call_args.args[0], CATALOG)

    def test_stale_cache_cannot_rank_the_previous_day(self):
        self.prepare_cycle()
        self.cache.side_effect = None
        self.cache.return_value = {"ready": False, "expected_trade_date": TARGET,
                                   "latest_trade_date": "20260827", "errors": ["最新行情不完整"]}
        result = service.run_automatic_selection_cycle()
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["runs"][0]["target_trade_date"], TARGET)
        self.rank.assert_not_called()
        self.catalog.assert_not_called()

    def test_unknown_target_is_not_invented_from_today_or_stale_watermark(self):
        self.prepare_cycle()
        self.cache.side_effect = None
        self.cache.return_value = {"ready": False, "latest_trade_date": "20260827"}
        result = service.run_automatic_selection_cycle()
        self.assertIsNone(result["target_trade_date"])
        self.assertEqual(result["runs"][0]["target_trade_date"], "")

    def test_empty_catalog_persists_failure_instead_of_losing_domain_evidence(self):
        self.prepare_cycle()
        self.snapshots.side_effect = None
        self.snapshots.return_value = []
        self.catalog.return_value = []
        result = service.run_automatic_selection_cycle(job_run_id="job-empty")
        self.assertEqual(result["failed_count"], 1)
        self.assertIn("目录为空", result["runs"][0]["error"])
        self.assertEqual(self.fail.call_args.kwargs["job_run_id"], "job-empty")
        self.rank.assert_not_called()

    def test_cache_query_failure_is_persisted(self):
        self.prepare_cycle()
        self.cache.side_effect = RuntimeError("日历读取失败")
        result = service.run_automatic_selection_cycle()
        self.assertIn("日历读取失败", result["runs"][0]["error"])
        self.rank.assert_not_called()

    def test_missing_ingestion_marker_prevents_ranking_even_if_counts_look_ready(self):
        self.prepare_cycle()
        self.ingestion.side_effect = RuntimeError("尚无成功的数据入库记录")
        result = service.run_automatic_selection_cycle()
        self.assertEqual(result["failed_count"], 1)
        self.rank.assert_not_called()

    def test_ingestion_and_selection_use_the_same_database_lock(self):
        factory = self.mock(service, "_database_named_lock", return_value=MagicMock())
        with service._selection_lock():
            pass
        with service._ingestion_lock():
            pass
        self.assertEqual(factory.call_args_list[0].args[0], factory.call_args_list[1].args[0])

    def test_market_soft_error_cannot_be_hidden_by_old_ready_cache(self):
        self.mock(service, "_ingestion_lock", return_value=MagicMock())
        self.mock(repository, "ensure_default_strategy")
        self.mock(repository, "active_strategy_models", return_value=[])
        sync = self.mock(service, "sync_market", return_value={"errors": ["daily 部分失败"]})
        self.mock(service, "local_selection_cache_status", return_value={"ready": True, "expected_trade_date": TARGET})
        result = service.sync_automatic_selection_data()
        self.assertFalse(result["ready"])
        self.assertIn("daily 部分失败", result["errors"])
        sync.assert_called_once_with(days=60)

    def test_factor_error_is_disclosed_without_blocking_other_factor_free_strategies(self):
        self.mock(service, "_ingestion_lock", return_value=MagicMock())
        self.mock(repository, "ensure_default_strategy")
        self.mock(repository, "active_strategy_models", return_value=[])
        self.mock(service, "sync_market", return_value={"errors": []})
        self.mock(service, "local_selection_cache_status", return_value={"ready": True, "expected_trade_date": TARGET})
        self.mock(service, "_sync_required_factors", return_value={"errors": ["某因子缺失"]})
        result = service.sync_automatic_selection_data()
        self.assertTrue(result["ready"])
        self.assertIn("某因子缺失", result["errors"])

    def test_marker_requires_latest_success_ready_and_matching_trade_date(self):
        db = MagicMock()
        self.mock(service, "SessionLocal", return_value=db)
        db.__enter__.return_value = db
        row = SimpleNamespace(run_id="data-1", status="success", output_content=json.dumps({"ready": True, "target_trade_date": TARGET}))
        db.scalar.return_value = row
        self.assertEqual(service._completed_ingestion_evidence(TARGET)["job_run_id"], "data-1")
        for status, output in [("failed", row.output_content), ("running", row.output_content),
                               ("success", '{"ready":false}'), ("success", "[]"),
                               ("success", '{"ready":true,"target_trade_date":"20260827"}')]:
            row.status, row.output_content = status, output
            with self.subTest(status=status, output=output), self.assertRaises(RuntimeError):
                service._completed_ingestion_evidence(TARGET)

    def test_immature_evaluation_is_rotated_without_loading_price_rows(self):
        self.mock(repository, "pending_evaluation_contexts", return_value=[{
            "evaluation_id": 11, "target_trade_date": TARGET, "horizon": 60,
        }])
        self.mock(service, "load_local_open_trade_dates", return_value=[])
        update = self.mock(repository, "update_evaluation")
        result = service.update_due_evaluations()
        self.assertEqual(result["pending"], 1)
        update.assert_called_once_with(11, {})

    def test_calendar_gap_defers_evaluation_without_stopping_other_work(self):
        self.mock(repository, "pending_evaluation_contexts", return_value=[{
            "evaluation_id": 12, "target_trade_date": TARGET, "horizon": 5,
        }])
        self.mock(service, "load_local_open_trade_dates", side_effect=RuntimeError("日历不连续"))
        update = self.mock(repository, "update_evaluation")
        result = service.update_due_evaluations()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["completed"], 0)
        update.assert_called_once_with(12, {"error": "日历不连续"})

    def test_explanation_failure_only_updates_explanation_state_and_never_places_orders(self):
        from backend.app.paper import paper_service
        self.mock(repository, "run_detail", return_value={"run_id": "ranked", "candidates": [], "quality_gate": {"passed": True}})
        self.mock(service, "_generate_explanation", side_effect=RuntimeError("模型不可用"))
        update = self.mock(repository, "update_run_explanation")
        persist = self.mock(repository, "persist_run")
        order = self.mock(paper_service, "place_order")
        service._generate_persisted_explanation("ranked")
        update.assert_called_once_with("ranked", status="failed", error="模型不可用")
        persist.assert_not_called()
        order.assert_not_called()

    def test_input_history_round_trips_through_candidate_ledger(self):
        evidence = {"expected_history_dates": history_dates(TARGET), "history": [{"trade_date": TARGET, "close": 10.0, "amount": 50000.0}]}
        row = repository._candidate_model("run-1", {"symbol": "600001.SH", "input_evidence": evidence})
        self.assertEqual(repository.candidate_to_dict(row)["input_evidence"], evidence)

    def test_reverted_inputs_create_a_new_active_run_then_remain_idempotent(self):
        from backend.app.core.models import AutomaticSelectionRun

        stored = []
        self.mock(repository, "latest_run_model", return_value=None)
        self.mock(repository, "latest_run_for_target_model", side_effect=lambda *args: stored[-1] if stored else None)
        self.mock(repository, "find_run_by_inputs", side_effect=lambda sid, day, chash, fingerprint: next(
            (repository.run_to_dict(row) for row in stored if row.data_fingerprint == fingerprint), None,
        ))

        def persist(**kwargs):
            row = AutomaticSelectionRun(run_id=f"run-{len(stored) + 1}", **kwargs["run_payload"])
            stored.append(row)
            return repository.run_to_dict(row), True

        self.mock(repository, "persist_run", side_effect=persist)
        row = {**CATALOG[0], "close_price": 12.0, "pe_ttm": 10.0, "pb": 1.0, "total_mv": 1000.0,
               "history": [{"trade_date": day, "close": 10.0 + i / 10, "amount": 100_000.0}
                           for i, day in enumerate(history_dates(TARGET))],
               "expected_history_dates": history_dates(TARGET)}
        args = dict(target_trade_date=TARGET, trigger_type="test", generate_explanation=False)
        first = service.run_strategy_from_rows(strategy(), [row], **args)
        second = service.run_strategy_from_rows(strategy(), [{**row, "pe_ttm": 11.0}], **args)
        third = service.run_strategy_from_rows(strategy(), [row], **args)
        retry = service.run_strategy_from_rows(strategy(), [row], **args)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(third["supersedes_run_id"], second["run_id"])
        self.assertNotEqual(third["run_id"], first["run_id"])
        self.assertEqual(third["run_id"], retry["run_id"])
        self.assertFalse(retry["created"])
        self.assertEqual(len(stored), 3)

    def test_failed_run_fingerprint_includes_scoring_version(self):
        self.mock(repository, "latest_run_for_target_model", return_value=None)
        persist = self.mock(repository, "persist_run", side_effect=lambda **kwargs: (kwargs["run_payload"], True))
        args = dict(target_trade_date=TARGET, trigger_type="test", job_run_id=None,
                    sync_summary={}, error=RuntimeError("失败"))
        first = service._persist_failed_strategy_run(strategy(), **args)
        with patch.object(service, "SCORING_IMPLEMENTATION_VERSION", "next-version"):
            second = service._persist_failed_strategy_run(strategy(), **args)
        self.assertNotEqual(first["data_fingerprint"], second["data_fingerprint"])
        self.assertEqual(json.loads(first["sync_summary_json"])["scoring_implementation_version"], SCORING_IMPLEMENTATION_VERSION)
        self.assertFalse(persist.call_args.kwargs["create_evaluations"])


if __name__ == "__main__":
    unittest.main()
