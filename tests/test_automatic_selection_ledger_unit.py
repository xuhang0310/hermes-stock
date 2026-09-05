from __future__ import annotations

import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateTable

from backend.app.core.models import (
    AutomaticSelectionCandidate, AutomaticSelectionEvaluation, AutomaticSelectionRun,
    AutomaticSelectionStockSnapshot, AutomaticSelectionStrategy, AutomaticSelectionStrategyEvent,
)
from backend.app.quant import automatic_selection_repository as repository
from backend.app.quant.automatic_selection_scoring import DEFAULT_STRATEGY_CONFIG, SCORING_IMPLEMENTATION_VERSION


class AutomaticSelectionLedgerUnitTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(Engine, "connect", side_effect=AssertionError("禁止连接应用数据库"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_new_tables_compile_for_mysql_with_explicit_collation_and_no_foreign_keys(self):
        for model in (AutomaticSelectionCandidate, AutomaticSelectionEvaluation, AutomaticSelectionRun,
                      AutomaticSelectionStockSnapshot, AutomaticSelectionStrategy, AutomaticSelectionStrategyEvent):
            with self.subTest(model=model.__name__):
                sql = str(CreateTable(model.__table__).compile(dialect=mysql.dialect()))
                self.assertIn("utf8mb4_unicode_ci", sql)
                self.assertFalse(model.__table__.foreign_keys)
        self.assertIn("input_evidence_json", AutomaticSelectionCandidate.__table__.columns)

    def test_promotion_query_requires_mature_covered_non_superseded_evidence(self):
        db = MagicMock()
        db.execute.return_value.mappings.return_value = [{
            "run_id": "evidence-1", "quality_gate_json": '{"passed":true}',
            "sync_summary_json": json.dumps({"scoring_implementation_version": SCORING_IMPLEMENTATION_VERSION}),
        }]
        target = SimpleNamespace(id=1, config_hash="a" * 64, config_json=json.dumps(DEFAULT_STRATEGY_CONFIG))
        self.assertEqual(repository._promotion_bootstrap_evidence(db, target), "evidence-1")
        sql = str(db.execute.call_args.args[0].compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
        for clause in ("horizon = 5", "status = 'completed'", "NOT (EXISTS", "supersedes_run_id",
                       "coverage_ratio BETWEEN 0.8 AND 1.0", "entry_trade_date >", "config_hash ="):
            self.assertIn(clause, sql)

    def test_promotion_rejects_missing_quality_and_old_scoring_version(self):
        target = SimpleNamespace(id=1, config_hash="a" * 64, config_json=json.dumps(DEFAULT_STRATEGY_CONFIG))
        for rows in ([], [{"run_id": "r", "quality_gate_json": '{"passed":false}', "sync_summary_json": "{}"}],
                     [{"run_id": "r", "quality_gate_json": '{"passed":true}', "sync_summary_json": '{"scoring_implementation_version":"old"}'}]):
            db = MagicMock()
            db.execute.return_value.mappings.return_value = rows
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "最低启动证据"):
                repository._promotion_bootstrap_evidence(db, target)

    def test_evaluation_batch_reserves_both_queues_and_projects_only_needed_candidates(self):
        db = MagicMock()
        db.__enter__.return_value = db
        pending = [self.context(i, "pending") for i in range(1, 5)]
        retries = [self.context(i, "retry") for i in range(5, 9)]
        results = []
        for rows in (pending, retries):
            result = MagicMock()
            result.mappings.return_value = rows
            results.append(result)
        candidates = MagicMock()
        candidates.all.return_value = [("pending", "600001.SH", True, True), ("retry", "600002.SH", False, True)]
        db.execute.side_effect = [*results, candidates]
        with patch.object(repository, "SessionLocal", return_value=db):
            contexts = repository.pending_evaluation_contexts(limit=4)
        self.assertEqual([row["evaluation_id"] for row in contexts], [1, 2, 5, 6])
        self.assertEqual(contexts[0]["selected_symbols"], ["600001.SH"])
        self.assertEqual(contexts[-1]["benchmark_symbols"], ["600002.SH"])
        sql = str(db.execute.call_args_list[-1].args[0].compile(dialect=mysql.dialect()))
        self.assertIn("selected IS true", sql)
        self.assertIn("eligible IS true", sql)
        self.assertNotIn("input_evidence_json", sql)

    def test_unchanged_evaluation_attempt_still_updates_rotation_time(self):
        db = MagicMock()
        db.__enter__.return_value = db
        row = AutomaticSelectionEvaluation(id=1, run_id="r", horizon=60, status="pending", updated_at=datetime(2020, 1, 1))
        db.get.return_value = row
        with patch.object(repository, "SessionLocal", return_value=db):
            repository.update_evaluation(1, {})
        self.assertGreater(row.updated_at, datetime(2020, 1, 1))
        db.commit.assert_called_once()

    @staticmethod
    def context(identifier, run_id):
        return {"evaluation_id": identifier, "run_id": run_id, "horizon": 5, "turnover": 0.0,
                "target_trade_date": "20260828", "config_json": "{}", "updated_at": datetime(2020, 1, 1)}


if __name__ == "__main__":
    unittest.main()
