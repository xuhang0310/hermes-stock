from __future__ import annotations

import copy
import math
import unittest

from tests.automatic_selection_fixtures import history_dates
from unittest.mock import patch

import numpy as np

from backend.app.quant import automatic_selection_scoring as scoring
from backend.app.quant.automatic_selection_scoring import (
    DEFAULT_STRATEGY_CONFIG,
    apply_membership_changes,
    build_candidates,
    config_hash,
    fingerprint_payload,
    quality_gate,
    score_candidates,
    validate_config,
)

TARGET_DATE = "20260828"


def _config(**overrides):
    config = copy.deepcopy(DEFAULT_STRATEGY_CONFIG)
    config["top_n"] = 3
    config["quality"] = {
        "min_bar_coverage": 0.0,
        "min_basic_coverage": 0.0,
        "min_industry_coverage": 0.8,
        "min_factor_coverage": 1.0,
        "min_history_coverage": 0.85,
        "min_eligible_count": 1,
        "min_evaluation_coverage": 0.5,
    }
    config["constraints"] = {
        "max_per_industry": 0,
        "max_per_market_cap_bucket": 0,
    }
    for key, value in overrides.items():
        config[key] = value
    return config


def _history(*, start: float = 10.0, step: float = 0.1, amount: float = 100_000.0):
    return [
        {
            "trade_date": trade_date,
            "close": start + index * step,
            "adj_factor": 1.0,
            "amount": amount,
        }
        for index, trade_date in enumerate(history_dates(TARGET_DATE))
    ]


def _row(symbol: str, **overrides):
    row = {
        "symbol": symbol,
        "name": f"测试{symbol}",
        "market": "主板",
        "list_status": "L",
        "list_date": "20100101",
        "industry": "银行",
        "close_price": 12.0,
        "pct_chg": 1.0,
        "pe_ttm": 10.0,
        "pb": 1.0,
        "total_mv": 1000.0,
        "circ_mv": 800.0,
        "history": _history(),
        "expected_history_dates": history_dates(TARGET_DATE),
        "factor_values": {},
    }
    row.update(overrides)
    return row


class AutomaticSelectionScoringTests(unittest.TestCase):
    def test_build_candidates_records_distinct_exclusion_reasons(self) -> None:
        rows = [
            _row("600001.SH", name="*ST 测试"),
            _row("600002.SH", list_status="P"),
            _row("600003.SH", list_date="20260801"),
            _row("600004.SH", close_price=None),
            _row("600005.SH", pe_ttm=None),
            _row("600006.SH", pe_ttm=-2.0),
            _row("600007.SH", history=_history(amount=1_000.0)),
            _row("600008.SH", history=_history()[:10]),
            _row("600009.SH", daily_basic_present=False),
        ]

        candidates = build_candidates(rows, _config(), target_trade_date=TARGET_DATE)
        codes = {item["symbol"]: set(item["exclusion_codes"]) for item in candidates}

        self.assertIn("st", codes["600001.SH"])
        self.assertIn("not_listed", codes["600002.SH"])
        self.assertIn("listing_too_short", codes["600003.SH"])
        self.assertIn("target_bar_missing", codes["600004.SH"])
        self.assertIn("valuation_invalid", codes["600005.SH"])
        self.assertIn("valuation_invalid", codes["600006.SH"])
        self.assertIn("liquidity_low", codes["600007.SH"])
        self.assertIn("history_insufficient", codes["600008.SH"])
        self.assertIn("daily_basic_missing", codes["600009.SH"])
        self.assertTrue(all(not item["eligible"] for item in candidates))

    def test_required_tushare_factor_is_strict_and_enters_score(self) -> None:
        config = _config(
            feature_weights={"value_pe": 1.0},
            tushare_factors=[{"id": "tushare:roe_ttm", "weight": 2.0}],
        )
        rows = [
            _row("600001.SH", factor_values={"tushare:roe_ttm": 0.1}),
            _row("600002.SH", factor_values={}),
        ]

        candidates = build_candidates(rows, config, target_trade_date=TARGET_DATE)
        self.assertTrue(candidates[0]["eligible"])
        self.assertFalse(candidates[1]["eligible"])
        self.assertIn("required_factor_missing", candidates[1]["exclusion_codes"])
        score_candidates(candidates, config)
        self.assertIn("tushare:roe_ttm", candidates[0]["z_scores"])

    def test_scoring_direction_and_tie_break_are_deterministic(self) -> None:
        config = _config(
            top_n=4,
            feature_weights={"value_pe": 1.0},
        )
        rows = [
            _row("600003.SH", pe_ttm=20.0),
            _row("600002.SH", pe_ttm=10.0),
            _row("600001.SH", pe_ttm=10.0),
            _row("600004.SH", pe_ttm=30.0),
        ]

        first = score_candidates(build_candidates(rows, config, target_trade_date=TARGET_DATE), config)
        second = score_candidates(build_candidates(reversed(rows), config, target_trade_date=TARGET_DATE), config)
        first_ranks = {item["symbol"]: item["rank"] for item in first}
        second_ranks = {item["symbol"]: item["rank"] for item in second}

        self.assertEqual(first_ranks, second_ranks)
        self.assertEqual(first_ranks["600001.SH"], 1)
        self.assertEqual(first_ranks["600002.SH"], 2)
        self.assertLess(first_ranks["600003.SH"], first_ranks["600004.SH"])
        self.assertTrue(all(math.isfinite(float(item["composite_score"])) for item in first))

    def test_industry_and_market_cap_constraints_limit_selection(self) -> None:
        config = _config(
            top_n=4,
            feature_weights={"value_pe": 1.0},
            constraints={"max_per_industry": 1, "max_per_market_cap_bucket": 1},
        )
        rows = [
            _row("600001.SH", pe_ttm=5.0, industry="银行", total_mv=100.0),
            _row("600002.SH", pe_ttm=6.0, industry="银行", total_mv=110.0),
            _row("600003.SH", pe_ttm=7.0, industry="医药", total_mv=500.0),
            _row("600004.SH", pe_ttm=8.0, industry="科技", total_mv=900.0),
            _row("600005.SH", pe_ttm=9.0, industry="消费", total_mv=1000.0),
            _row("600006.SH", pe_ttm=10.0, industry=None, total_mv=600.0),
        ]

        candidates = score_candidates(build_candidates(rows, config, target_trade_date=TARGET_DATE), config)
        selected = [item for item in candidates if item["selected"]]
        selected_industries = [item["industry"] for item in selected]
        selected_buckets = [item["market_cap_bucket"] for item in selected]

        self.assertEqual(len(selected_industries), len(set(selected_industries)))
        self.assertEqual(len(selected_buckets), len(set(selected_buckets)))
        missing = next(item for item in candidates if item["symbol"] == "600006.SH")
        self.assertIn("industry_missing", missing["constraint_codes"])
        self.assertTrue(all(abs(item["research_weight"] - 1 / len(selected)) < 1e-12 for item in selected))

    def test_quality_gate_reports_coverage_and_minimum_pool(self) -> None:
        config = _config(
            quality={
                "min_bar_coverage": 0.9,
                "min_basic_coverage": 0.8,
                "min_industry_coverage": 0.8,
                "min_factor_coverage": 0.8,
                "min_eligible_count": 2,
                "min_evaluation_coverage": 0.8,
            },
            constraints={"max_per_industry": 1, "max_per_market_cap_bucket": 0},
        )
        rows = [
            _row("600001.SH"),
            _row("600002.SH", close_price=None, pe_ttm=None, industry=None, daily_basic_present=False),
        ]

        gate = quality_gate(build_candidates(rows, config, target_trade_date=TARGET_DATE), config)
        failure_codes = {item["code"] for item in gate["failures"]}

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["metrics"]["bar_coverage"], 0.5)
        self.assertIn("bar_coverage", failure_codes)
        self.assertIn("basic_coverage", failure_codes)
        self.assertIn("industry_coverage", failure_codes)
        self.assertIn("eligible_count", failure_codes)

    def test_unavailable_pe_does_not_reduce_real_basic_coverage(self) -> None:
        config = _config()
        config["quality"]["min_basic_coverage"] = 0.85
        rows = [
            _row(
                f"{600000 + index}.SH",
                daily_basic_present=index < 99,
                pe_ttm=None if index < 30 else 10.0,
                total_mv=1000.0 if index < 99 else None,
            )
            for index in range(100)
        ]
        candidates = build_candidates(rows, config, target_trade_date=TARGET_DATE)
        gate = quality_gate(candidates, config)

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["metrics"]["basic_count"], 99)
        self.assertEqual(gate["metrics"]["basic_coverage"], 0.99)
        self.assertEqual(gate["metrics"]["eligible_count"], 69)
        score_candidates(candidates, config)
        for candidate in candidates[:30]:
            self.assertTrue(candidate["basic_data_complete"])
            self.assertTrue(candidate["input_evidence"]["daily_basic_present"])
            self.assertEqual(candidate["exclusion_codes"], ["valuation_invalid"])
            self.assertFalse(candidate["eligible"])
            self.assertFalse(candidate["selected"])
            self.assertIsNone(candidate["rank"])

    def test_actual_missing_basic_records_still_block_even_with_filled_values(self) -> None:
        config = _config()
        config["quality"]["min_basic_coverage"] = 0.85
        rows = [
            _row(f"{600000 + index}.SH", daily_basic_present=index < 84)
            for index in range(100)
        ]
        candidates = build_candidates(rows, config, target_trade_date=TARGET_DATE)
        gate = quality_gate(candidates, config)

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["metrics"]["basic_coverage"], 0.84)
        self.assertIn("basic_coverage", {failure["code"] for failure in gate["failures"]})
        for candidate in candidates[84:]:
            self.assertFalse(candidate["input_evidence"]["daily_basic_present"])
            self.assertFalse(candidate["basic_data_complete"])
            self.assertFalse(candidate["eligible"])
            self.assertEqual(candidate["exclusion_codes"], ["daily_basic_missing"])

    def test_basic_record_requires_positive_finite_market_cap(self) -> None:
        for market_cap in (None, 0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(market_cap=market_cap):
                rows = [
                    _row("600001.SH", daily_basic_present=True),
                    _row("600002.SH", daily_basic_present=True, total_mv=market_cap),
                ]
                candidates = build_candidates(rows, _config(), target_trade_date=TARGET_DATE)
                invalid = candidates[1]
                self.assertTrue(invalid["input_evidence"]["daily_basic_present"])
                self.assertFalse(invalid["basic_data_complete"])
                self.assertFalse(invalid["eligible"])
                self.assertEqual(invalid["exclusion_codes"], ["daily_basic_missing"])
                self.assertEqual(quality_gate(candidates, _config())["metrics"]["basic_coverage"], 0.5)

    def test_null_or_nonpositive_valuation_stays_excluded_with_complete_basic_data(self) -> None:
        for field in ("pe_ttm", "pb"):
            for value in (None, 0.0, -1.0, float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    candidates = build_candidates(
                        [_row("600001.SH", daily_basic_present=True, **{field: value})],
                        _config(),
                        target_trade_date=TARGET_DATE,
                    )
                    candidate = candidates[0]
                    self.assertTrue(candidate["basic_data_complete"])
                    self.assertFalse(candidate["eligible"])
                    self.assertEqual(candidate["exclusion_codes"], ["valuation_invalid"])
                    self.assertEqual(quality_gate(candidates, _config())["metrics"]["basic_coverage"], 1.0)

    def test_legacy_inputs_infer_basic_record_only_from_valid_market_cap(self) -> None:
        rows = [
            _row("600001.SH", pe_ttm=None),
            _row("600002.SH", total_mv=None),
            _row("600003.SH", total_mv=0.0),
        ]
        candidates = build_candidates(rows, _config(), target_trade_date=TARGET_DATE)
        self.assertEqual([item["basic_data_complete"] for item in candidates], [True, False, False])
        self.assertEqual(
            [item["input_evidence"]["daily_basic_present"] for item in candidates],
            [True, False, False],
        )
        self.assertEqual(candidates[0]["exclusion_codes"], ["valuation_invalid"])

    def test_basic_presence_evidence_and_v4_scoring_change_run_identity(self) -> None:
        candidates = build_candidates([_row("600001.SH")], _config(), target_trade_date=TARGET_DATE)
        original = fingerprint_payload(candidates, _config(), TARGET_DATE)
        changed = copy.deepcopy(candidates)
        changed[0]["input_evidence"]["daily_basic_present"] = False
        self.assertNotEqual(original, fingerprint_payload(changed, _config(), TARGET_DATE))
        self.assertEqual(scoring.SCORING_IMPLEMENTATION_VERSION, "forward-selection-v5")
        with patch.object(scoring, "SCORING_IMPLEMENTATION_VERSION", "forward-selection-v3"):
            self.assertNotEqual(original, fingerprint_payload(candidates, _config(), TARGET_DATE))

    def test_config_and_data_fingerprints_are_stable_and_sensitive(self) -> None:
        config = _config()
        candidates = build_candidates(
            [_row("600002.SH"), _row("600001.SH")],
            config,
            target_trade_date=TARGET_DATE,
        )
        reordered = list(reversed(candidates))

        self.assertEqual(config_hash(config), config_hash(copy.deepcopy(config)))
        self.assertEqual(
            fingerprint_payload(candidates, config, TARGET_DATE),
            fingerprint_payload(reordered, copy.deepcopy(config), TARGET_DATE),
        )
        changed = copy.deepcopy(candidates)
        changed[0]["close_price"] = 99.0
        self.assertNotEqual(
            fingerprint_payload(candidates, config, TARGET_DATE),
            fingerprint_payload(changed, config, TARGET_DATE),
        )

    def test_membership_change_counts_added_retained_and_removed(self) -> None:
        candidates = [
            {"symbol": "600001.SH", "selected": True, "change_state": "unchanged"},
            {"symbol": "600002.SH", "selected": True, "change_state": "unchanged"},
            {"symbol": "600003.SH", "selected": False, "change_state": "unchanged"},
        ]

        counts = apply_membership_changes(candidates, ["600001.SH", "600003.SH", "600004.SH"])

        self.assertEqual(counts, {"added": 1, "retained": 1, "removed": 2})
        states = {item["symbol"]: item["change_state"] for item in candidates}
        self.assertEqual(states["600001.SH"], "retained")
        self.assertEqual(states["600002.SH"], "added")
        self.assertEqual(states["600003.SH"], "removed")

    def test_twenty_day_features_use_twenty_returns_and_last_twenty_amounts(self) -> None:
        history = _history()
        history[0]["amount"] = 9_999_999.0
        candidate = build_candidates([_row("600001.SH", history=history)], _config(), target_trade_date=TARGET_DATE)[0]
        closes = np.asarray([item["close"] for item in history])
        self.assertTrue(candidate["eligible"])
        self.assertAlmostEqual(candidate["momentum_20"], closes[-1] / closes[0] - 1)
        self.assertAlmostEqual(candidate["volatility_20"], np.std(np.diff(closes) / closes[:-1], ddof=1))
        self.assertEqual(candidate["avg_amount_20"], 100_000.0)

    def test_ex_rights_gap_is_not_scored_as_a_price_drop(self) -> None:
        # 第 11 个交易日 10 送 10：不复权收盘价腰斩，复权因子同步翻倍，真实走势仍是平滑上涨。
        history = _history()
        for item in history[10:]:
            item["close"] /= 2
            item["adj_factor"] = 2.0
        candidate = build_candidates([_row("600001.SH", history=history)], _config(), target_trade_date=TARGET_DATE)[0]
        smooth = np.asarray([item["close"] for item in _history()])
        self.assertTrue(candidate["eligible"])
        self.assertAlmostEqual(candidate["momentum_20"], smooth[-1] / smooth[0] - 1)
        self.assertAlmostEqual(candidate["volatility_20"], np.std(np.diff(smooth) / smooth[:-1], ddof=1))
        self.assertGreater(candidate["momentum_20"], 0)

    def test_missing_adjustment_factor_excludes_instead_of_using_raw_prices(self) -> None:
        for missing in (None, 0.0, float("nan")):
            with self.subTest(missing=missing):
                history = _history()
                history[5]["adj_factor"] = missing
                candidate = build_candidates([_row("600001.SH", history=history)], _config(), target_trade_date=TARGET_DATE)[0]
                self.assertFalse(candidate["eligible"])
                self.assertFalse(candidate["history_complete"])
                self.assertEqual(candidate["exclusion_codes"], ["adj_factor_missing"])
                self.assertIsNone(candidate["momentum_20"])
        rows = [_row("600001.SH"), _row("600002.SH", history=[{**item, "adj_factor": None} for item in _history()])]
        gate = quality_gate(build_candidates(rows, _config(), target_trade_date=TARGET_DATE), _config())
        self.assertEqual(gate["metrics"]["history_coverage"], 0.5)

    def test_adjustment_factor_changes_run_identity(self) -> None:
        candidates = build_candidates([_row("600001.SH")], _config(), target_trade_date=TARGET_DATE)
        changed = copy.deepcopy(candidates)
        changed[0]["input_evidence"]["history"][3]["adj_factor"] = 1.5
        self.assertNotEqual(
            fingerprint_payload(candidates, _config(), TARGET_DATE),
            fingerprint_payload(changed, _config(), TARGET_DATE),
        )

    def test_missing_exchange_session_is_not_replaced_with_an_older_bar(self) -> None:
        history = _history()
        history[10]["trade_date"] = "20260730"
        candidate = build_candidates([_row("600001.SH", history=history)], _config(), target_trade_date=TARGET_DATE)[0]
        self.assertFalse(candidate["history_complete"])
        self.assertIn("history_insufficient", candidate["exclusion_codes"])
        self.assertIsNone(candidate["momentum_20"])

    def test_history_coverage_blocks_biased_partial_universe(self) -> None:
        rows = [_row("600001.SH"), _row("600002.SH", history=_history()[:10])]
        gate = quality_gate(build_candidates(rows, _config(), target_trade_date=TARGET_DATE), _config())
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["metrics"]["history_coverage"], 0.5)
        self.assertIn("history_coverage", {item["code"] for item in gate["failures"]})

    def test_missing_calendar_cannot_claim_a_complete_history(self) -> None:
        candidate = build_candidates([_row("600001.SH", expected_history_dates=[])], _config(), target_trade_date=TARGET_DATE)[0]
        self.assertFalse(candidate["eligible"])

    def test_scoring_version_and_raw_history_change_run_identity(self) -> None:
        candidates = build_candidates([_row("600001.SH")], _config(), target_trade_date=TARGET_DATE)
        original = fingerprint_payload(candidates, _config(), TARGET_DATE)
        with patch.object(scoring, "SCORING_IMPLEMENTATION_VERSION", "changed"):
            self.assertNotEqual(original, fingerprint_payload(candidates, _config(), TARGET_DATE))
        changed = copy.deepcopy(candidates)
        changed[0]["input_evidence"]["history"][0]["amount"] += 1
        self.assertNotEqual(original, fingerprint_payload(changed, _config(), TARGET_DATE))

    def test_validation_rejects_unknown_or_zero_weight_configuration(self) -> None:
        with self.assertRaises(ValueError):
            validate_config(_config(feature_weights={"unknown": 1.0}))
        with self.assertRaises(ValueError):
            validate_config(_config(feature_weights={"value_pe": 0.0}))


if __name__ == "__main__":
    unittest.main()
