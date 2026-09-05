from __future__ import annotations

import copy
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from backend.app.core.models import (
    AutomaticSelectionCandidate,
    AutomaticSelectionEvaluation,
    AutomaticSelectionRun,
    AutomaticSelectionStockSnapshot,
    AutomaticSelectionStrategy,
    AutomaticSelectionStrategyEvent,
    TushareFactorCatalog,
)
from backend.app.quant import automatic_selection_repository as repository
from backend.app.quant import automatic_selection_scoring as scoring
from backend.app.quant import tushare_factor_service
from backend.app.quant.automatic_selection_scoring import (
    DEFAULT_STRATEGY_CONFIG,
    canonical_json,
    config_hash,
)
from tests.automatic_selection_mysql import (
    create_isolated_mysql_engine,
    reset_isolated_schema,
)

TARGET_DATE = "20260828"


class AutomaticSelectionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_isolated_mysql_engine()
        reset_isolated_schema(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.patch = patch.object(repository, "SessionLocal", self.SessionFactory)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        factor_patch = patch.object(tushare_factor_service, "SessionLocal", self.SessionFactory)
        factor_patch.start()
        self.addCleanup(factor_patch.stop)

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _challenger(self) -> dict:
        return repository.create_strategy(
            strategy_key="evidence-test",
            name="前瞻证据测试",
            config=copy.deepcopy(DEFAULT_STRATEGY_CONFIG),
            lifecycle_state="challenger",
            actor_user_id=8,
        )

    def _run(self, strategy: dict, run_id: str, **overrides) -> dict:
        payload = {
            "run_id": run_id,
            "strategy_id": strategy["id"],
            "strategy_key": strategy["strategy_key"],
            "strategy_version": strategy["version"],
            "strategy_name": strategy["name"],
            "target_trade_date": TARGET_DATE,
            "trigger_type": "test",
            "status": "completed",
            "stage": "ranked",
            "config_json": canonical_json(strategy["config"]),
            "config_hash": strategy["config_hash"],
            "data_fingerprint": run_id.ljust(64, "0"),
            "sync_summary_json": canonical_json({
                "scoring_implementation_version": scoring.SCORING_IMPLEMENTATION_VERSION,
            }),
            "quality_gate_json": canonical_json({"passed": True}),
            "universe_count": 3,
            "eligible_count": 2,
            "selected_count": 1,
            "started_at": datetime(2026, 8, 28, 16, 10),
            "created_at": datetime(2026, 8, 28, 16, 11),
            **overrides,
        }
        result, _ = repository.persist_run(
            run_payload=payload,
            candidates=[
                {"symbol": "600001.SH", "eligible": True, "selected": True, "rank": 1},
                {"symbol": "600002.SH", "eligible": True, "selected": False, "rank": 2},
                {"symbol": "600003.SH", "eligible": False, "selected": False},
            ],
            create_evaluations=True,
        )
        return result

    def _complete_five_day(self, run_id: str, **overrides) -> None:
        evaluation = repository.run_evaluations(run_id)[0]
        repository.update_evaluation_result(evaluation["id"], {
            "status": "completed",
            "entry_trade_date": "20260831",
            "exit_trade_date": "20260904",
            "selected_count": 1,
            "covered_count": 1,
            "coverage_ratio": 1.0,
            "benchmark_count": 2,
            "benchmark_covered_count": 2,
            "benchmark_coverage_ratio": 1.0,
            "basket_return": -0.01,
            "benchmark_return": 0.01,
            "excess_return": -0.02,
            **overrides,
        })

    def test_default_strategy_seed_is_idempotent(self) -> None:
        first = repository.ensure_default_strategy()
        second = repository.ensure_default_strategy()

        self.assertEqual(first["id"], second["id"])
        self.assertTrue(first["is_official"])
        self.assertEqual(first["config_hash"], config_hash(DEFAULT_STRATEGY_CONFIG))
        with self.SessionFactory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(AutomaticSelectionStrategy)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(AutomaticSelectionStrategyEvent)), 1)

    def test_new_versions_do_not_mutate_previous_config(self) -> None:
        first_config = copy.deepcopy(DEFAULT_STRATEGY_CONFIG)
        second_config = copy.deepcopy(DEFAULT_STRATEGY_CONFIG)
        second_config["top_n"] = 20
        first = repository.create_strategy(
            strategy_key="quality-growth",
            name="质量成长 v1",
            config=first_config,
            lifecycle_state="challenger",
            actor_user_id=7,
        )
        second = repository.create_strategy(
            strategy_key="quality-growth",
            name="质量成长 v2",
            config=second_config,
            lifecycle_state="shadow",
            actor_user_id=7,
            parent_strategy_id=first["id"],
        )

        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(first["config"]["top_n"], 30)
        self.assertEqual(second["config"]["top_n"], 20)
        with self.SessionFactory() as db:
            stored_first = db.get(AutomaticSelectionStrategy, first["id"])
            self.assertEqual(stored_first.config_hash, config_hash(first_config))

    def test_promotion_atomically_keeps_one_official_and_audits_both_sides(self) -> None:
        original = repository.ensure_default_strategy()
        challenger = repository.create_strategy(
            strategy_key="quality-growth",
            name="质量成长",
            config=copy.deepcopy(DEFAULT_STRATEGY_CONFIG),
            lifecycle_state="challenger",
            actor_user_id=8,
        )

        self._run(challenger, "promotion-evidence")
        self._complete_five_day("promotion-evidence")
        # The 20/60-day windows remain pending; the bootstrap gate is not a
        # profitability or statistical-significance claim.
        promoted = repository.promote_strategy(
            challenger["id"],
            actor_user_id=8,
            confirmation_note="已人工复核前瞻证据",
        )

        self.assertTrue(promoted["is_official"])
        with self.SessionFactory() as db:
            officials = db.scalars(
                select(AutomaticSelectionStrategy).where(AutomaticSelectionStrategy.official_slot == 1)
            ).all()
            self.assertEqual([row.id for row in officials], [challenger["id"]])
            demoted = db.get(AutomaticSelectionStrategy, original["id"])
            self.assertEqual(demoted.lifecycle_state, "challenger")
            events = db.scalars(
                select(AutomaticSelectionStrategyEvent).order_by(AutomaticSelectionStrategyEvent.id)
            ).all()
            self.assertEqual([event.event_type for event in events][-2:], ["demote", "promote"])

    def test_zero_evidence_promotion_leaves_official_and_audit_unchanged(self) -> None:
        original = repository.ensure_default_strategy()
        challenger = self._challenger()
        with self.SessionFactory() as db:
            before_events = db.scalar(select(func.count()).select_from(AutomaticSelectionStrategyEvent))
        with self.assertRaisesRegex(ValueError, "最低启动证据"):
            repository.promote_strategy(
                challenger["id"], actor_user_id=8, confirmation_note="已完成人工复核",
            )
        with self.SessionFactory() as db:
            self.assertEqual(db.get(AutomaticSelectionStrategy, original["id"]).official_slot, 1)
            self.assertIsNone(db.get(AutomaticSelectionStrategy, challenger["id"]).official_slot)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(AutomaticSelectionStrategyEvent)), before_events,
            )

    def test_promotion_rejects_incomplete_or_unreliable_evidence(self) -> None:
        cases = [
            ({}, {"status": "pending"}),
            ({}, {"status": "insufficient_data"}),
            ({}, {"coverage_ratio": 0.79}),
            ({}, {"benchmark_coverage_ratio": 0.79}),
            ({}, {"covered_count": 0}),
            ({}, {"benchmark_covered_count": 0}),
            ({}, {"entry_trade_date": None}),
            ({}, {"exit_trade_date": None}),
            ({}, {"selected_count": 2}),
            ({"status": "blocked"}, {}),
            ({"quality_gate_json": canonical_json({"passed": False})}, {}),
            ({"quality_gate_json": canonical_json({"passed": "true"})}, {}),
            ({"sync_summary_json": "{}"}, {}),
            ({"sync_summary_json": canonical_json({"scoring_implementation_version": "obsolete"})}, {}),
            ({"config_hash": "f" * 64}, {}),
        ]
        for index, (run_changes, evaluation_changes) in enumerate(cases):
            with self.subTest(index=index):
                challenger = self._challenger()
                run_id = f"invalid-evidence-{index}"
                self._run(challenger, run_id, **run_changes)
                self._complete_five_day(run_id, **evaluation_changes)
                with self.assertRaisesRegex(ValueError, "最低启动证据"):
                    repository.promote_strategy(
                        challenger["id"], actor_user_id=8, confirmation_note="已完成人工复核",
                    )

    def test_superseded_evidence_cannot_promote(self) -> None:
        challenger = self._challenger()
        self._run(challenger, "old-evidence")
        self._complete_five_day("old-evidence")
        self._run(challenger, "repair", supersedes_run_id="old-evidence", status="blocked")
        with self.assertRaisesRegex(ValueError, "最低启动证据"):
            repository.promote_strategy(
                challenger["id"], actor_user_id=8, confirmation_note="已完成人工复核",
            )

    def test_factor_creation_initializes_catalog_using_isolated_session(self) -> None:
        config = copy.deepcopy(DEFAULT_STRATEGY_CONFIG)
        config["tushare_factors"] = [{"id": "tushare:test_factor", "weight": 0.1}]

        def populate_catalog() -> None:
            with tushare_factor_service.SessionLocal() as db:
                db.add(TushareFactorCatalog(
                    factor_name="test_factor", asset_type="STK", factor_type="test",
                ))
                db.commit()

        with patch.object(tushare_factor_service, "sync_catalog", side_effect=populate_catalog) as sync:
            created = repository.create_strategy(
                strategy_key="catalog-test", name="目录初始化", config=config,
                lifecycle_state="shadow", actor_user_id=8,
            )
        sync.assert_called_once()
        self.assertEqual(created["config"]["tushare_factors"][0]["id"], "tushare:test_factor")

    def test_official_strategy_cannot_be_retired_directly(self) -> None:
        official = repository.ensure_default_strategy()
        with self.assertRaises(ValueError):
            repository.retire_strategy(official["id"], actor_user_id=1, note="直接退役")

    def test_stock_snapshot_is_insert_only_for_same_observation_day(self) -> None:
        first = [{
            "symbol": "600001.SH",
            "name": "首次名称",
            "market": "主板",
            "stock_type": "stock",
            "list_status": "L",
            "list_date": "20000101",
            "industry": "银行",
        }]
        changed = [{**first[0], "name": "被修改名称", "industry": "科技"}]
        added_later = {**first[0], "symbol": "600002.SH", "name": "事后新增"}

        self.assertEqual(repository.upsert_stock_snapshots(TARGET_DATE, first + first), 1)
        self.assertEqual(repository.upsert_stock_snapshots(TARGET_DATE, changed + [added_later]), 0)
        snapshots = repository.latest_stock_snapshots(TARGET_DATE)

        self.assertEqual(snapshots[0]["name"], "首次名称")
        self.assertEqual(snapshots[0]["industry"], "银行")
        with self.SessionFactory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(AutomaticSelectionStockSnapshot)), 1)

    def test_run_and_candidates_persist_once_for_identical_inputs(self) -> None:
        strategy = repository.ensure_default_strategy()
        payload = {
            "strategy_id": strategy["id"],
            "strategy_key": strategy["strategy_key"],
            "strategy_version": strategy["version"],
            "strategy_name": strategy["name"],
            "target_trade_date": TARGET_DATE,
            "trigger_type": "test",
            "job_run_id": None,
            "status": "completed",
            "stage": "ranked",
            "config_json": canonical_json(strategy["config"]),
            "config_hash": strategy["config_hash"],
            "data_fingerprint": "a" * 64,
            "supersedes_run_id": None,
            "sync_summary_json": canonical_json({"bars": 2}),
            "quality_gate_json": canonical_json({"passed": True}),
            "exclusion_summary_json": canonical_json({}),
            "universe_count": 2,
            "eligible_count": 2,
            "selected_count": 1,
            "selected_turnover": 1.0,
            "llm_status": "pending",
            "started_at": datetime(2026, 8, 28, 16, 10),
            "finished_at": datetime(2026, 8, 28, 16, 11),
        }
        candidates = [
            {
                "symbol": "600001.SH",
                "name": "测试一",
                "market": "主板",
                "list_status": "L",
                "list_date": "20000101",
                "industry": "银行",
                "eligible": True,
                "exclusion_codes": [],
                "factor_values": {"value_pe": -2.0},
                "z_scores": {"value_pe": 1.0},
                "composite_score": 1.0,
                "rank": 1,
                "percentile": 1.0,
                "selected": True,
                "research_weight": 1.0,
                "change_state": "added",
            },
            {
                "symbol": "600002.SH",
                "name": "测试二",
                "market": "主板",
                "list_status": "L",
                "list_date": "20000101",
                "industry": "科技",
                "eligible": True,
                "exclusion_codes": [],
                "factor_values": {"value_pe": -1.0},
                "z_scores": {"value_pe": -1.0},
                "composite_score": -1.0,
                "rank": 2,
                "percentile": 0.0,
                "selected": False,
                "change_state": "unchanged",
            },
        ]

        first, created_first = repository.persist_run(
            run_payload=payload,
            candidates=candidates,
            create_evaluations=True,
        )
        second, created_second = repository.persist_run(
            run_payload={**payload, "run_id": "duplicate-attempt"},
            candidates=candidates,
            create_evaluations=True,
        )

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first["run_id"], second["run_id"])
        detail = repository.run_detail(
            first["run_id"],
            candidate_scope="all",
            candidate_limit=1,
            candidate_offset=1,
        )
        self.assertEqual(len(detail["candidates"]), 1)
        self.assertEqual(detail["candidate_total"], 2)
        self.assertEqual(detail["candidate_limit"], 1)
        self.assertEqual(detail["candidate_offset"], 1)
        self.assertEqual([item["horizon"] for item in detail["evaluations"]], [5, 20, 60])
        self.assertEqual(
            [item["horizon"] for item in repository.run_evaluations(first["run_id"])],
            [5, 20, 60],
        )
        with self.SessionFactory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(AutomaticSelectionRun)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(AutomaticSelectionCandidate)), 2)
            self.assertEqual(db.scalar(select(func.count()).select_from(AutomaticSelectionEvaluation)), 3)

    def test_latest_runs_are_deterministic_and_replacements_win_timestamp_ties(self) -> None:
        strategy = repository.ensure_default_strategy()
        self._run(strategy, "a-first")
        self._run(strategy, "z-second")
        for _ in range(3):
            self.assertEqual(repository.latest_run_model(strategy["id"]).run_id, "z-second")
            self.assertEqual(repository.latest_run_for_target_model(strategy["id"], TARGET_DATE).run_id, "z-second")
            self.assertEqual(
                [run["run_id"] for run in repository.list_runs(strategy_id=strategy["id"])],
                ["z-second", "a-first"],
            )
        # A replacement may have a lexically smaller UUID within the same second.
        self._run(strategy, "a-repair", supersedes_run_id="z-second")
        self.assertEqual(repository.latest_run_model(strategy["id"]).run_id, "a-repair")
        self.assertEqual(repository.latest_run_for_target_model(strategy["id"], TARGET_DATE).run_id, "a-repair")

    def test_superseded_runs_are_not_prior_selection_or_active_evaluation(self) -> None:
        strategy = repository.ensure_default_strategy()
        self._run(strategy, "older-valid", target_trade_date="20260827")
        self._run(strategy, "old")
        self._run(strategy, "repair", supersedes_run_id="old", status="blocked")
        previous = repository.latest_run_model(
            strategy["id"], before_trade_date="20260831", completed_only=True,
        )
        self.assertEqual(previous.run_id, "older-valid")
        contexts = repository.pending_evaluation_contexts()
        self.assertEqual({row["run_id"] for row in contexts}, {"older-valid"})
        self.assertEqual({row.run_id for row in repository.evaluation_models()}, {"older-valid"})
        self.assertEqual(contexts[0]["selected_symbols"], ["600001.SH"])
        self.assertEqual(contexts[0]["benchmark_symbols"], ["600001.SH", "600002.SH"])
        # Historical evidence is still readable by an explicit run id.
        self.assertEqual(len(repository.run_evaluations("old")), 3)

    def test_both_queues_rotate_even_when_results_are_unchanged(self) -> None:
        strategy = repository.ensure_default_strategy()
        self._run(strategy, "pending")
        self._run(strategy, "retry")
        old_time = datetime(2026, 8, 1)
        with self.SessionFactory() as db:
            for row in db.scalars(select(AutomaticSelectionEvaluation)).all():
                row.status = "pending" if row.run_id == "pending" else "insufficient_data"
                row.updated_at = old_time
            db.commit()
        seen = set()
        for _ in range(3):
            contexts = repository.pending_evaluation_contexts(limit=2)
            self.assertEqual({row["run_id"] for row in contexts}, {"pending", "retry"})
            self.assertEqual(len(contexts), 2)
            for context in contexts:
                self.assertNotIn(context["evaluation_id"], seen)
                seen.add(context["evaluation_id"])
                status = "pending" if context["run_id"] == "pending" else "insufficient_data"
                repository.update_evaluation(context["evaluation_id"], {"status": status})
        self.assertEqual(len(seen), 6)

    def test_single_slot_batch_rotates_across_pending_and_retry(self) -> None:
        strategy = repository.ensure_default_strategy()
        self._run(strategy, "single-slot")
        with self.SessionFactory() as db:
            rows = db.scalars(select(AutomaticSelectionEvaluation).order_by(AutomaticSelectionEvaluation.id)).all()
            for index, row in enumerate(rows):
                row.status = "insufficient_data" if index == 1 else "pending"
                row.updated_at = datetime(2026, 8, 1)
            db.commit()
        seen = set()
        for _ in range(3):
            context = repository.pending_evaluation_contexts(limit=1)[0]
            self.assertNotIn(context["evaluation_id"], seen)
            seen.add(context["evaluation_id"])
            status = "insufficient_data" if context["horizon"] == 20 else "pending"
            repository.update_evaluation(context["evaluation_id"], {"status": status})
        self.assertEqual(len(seen), 3)

    def test_empty_retry_queue_lends_its_quota_to_pending(self) -> None:
        strategy = repository.ensure_default_strategy()
        self._run(strategy, "pending-only")
        self.assertEqual(len(repository.pending_evaluation_contexts(limit=3)), 3)

    def test_data_repair_creates_superseding_run(self) -> None:
        strategy = repository.ensure_default_strategy()
        base_payload = {
            "strategy_id": strategy["id"],
            "strategy_key": strategy["strategy_key"],
            "strategy_version": strategy["version"],
            "strategy_name": strategy["name"],
            "target_trade_date": TARGET_DATE,
            "trigger_type": "test",
            "status": "blocked",
            "stage": "quality_gate",
            "config_json": canonical_json(strategy["config"]),
            "config_hash": strategy["config_hash"],
            "sync_summary_json": "{}",
            "quality_gate_json": canonical_json({"passed": False}),
            "exclusion_summary_json": "{}",
            "universe_count": 0,
            "eligible_count": 0,
            "selected_count": 0,
            "llm_status": "skipped",
        }
        first, _ = repository.persist_run(
            run_payload={**base_payload, "data_fingerprint": "a" * 64},
            candidates=[],
            create_evaluations=False,
        )
        second, created = repository.persist_run(
            run_payload={
                **base_payload,
                "data_fingerprint": "b" * 64,
                "supersedes_run_id": first["run_id"],
            },
            candidates=[],
            create_evaluations=False,
        )

        self.assertTrue(created)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["supersedes_run_id"], first["run_id"])


if __name__ == "__main__":
    unittest.main()
