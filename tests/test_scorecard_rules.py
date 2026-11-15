"""成绩单的判定规则：什么算对、什么算错、多少样本才下结论。只用标准库。"""
from __future__ import annotations

import unittest

from backend.app.scorecard import rules


class DirectionTests(unittest.TestCase):
    def test_buying_and_holding_bet_on_the_stock_beating_the_market(self) -> None:
        for action in ("buy", "add", "hold"):
            self.assertEqual(rules.direction(action), rules.UP)

    def test_selling_bets_on_the_stock_lagging(self) -> None:
        for action in ("sell", "reduce", "stop_loss"):
            self.assertEqual(rules.direction(action), rules.DOWN)

    def test_cancel_is_not_a_view(self) -> None:
        self.assertIsNone(rules.direction("cancel"))
        self.assertIsNone(rules.direction(None))


class VerdictTests(unittest.TestCase):
    def test_a_buy_is_right_when_the_stock_beats_the_market(self) -> None:
        self.assertEqual(rules.verdict(rules.UP, 0.03), rules.RIGHT)
        self.assertEqual(rules.verdict(rules.UP, -0.03), rules.WRONG)

    def test_a_sell_is_right_when_the_stock_lags(self) -> None:
        self.assertEqual(rules.verdict(rules.DOWN, -0.03), rules.RIGHT)
        self.assertEqual(rules.verdict(rules.DOWN, 0.03), rules.WRONG)

    def test_small_gaps_are_neither_right_nor_wrong(self) -> None:
        self.assertEqual(rules.verdict(rules.UP, 0.009), rules.FLAT)
        self.assertEqual(rules.verdict(rules.DOWN, 0.009), rules.FLAT)
        self.assertEqual(rules.verdict(rules.UP, rules.DEAD_BAND), rules.RIGHT)

    def test_edge_is_what_following_the_advice_was_worth(self) -> None:
        self.assertAlmostEqual(rules.edge(rules.UP, 0.04), 0.04)
        self.assertAlmostEqual(rules.edge(rules.DOWN, 0.04), -0.04)


class ReturnTests(unittest.TestCase):
    def test_the_daily_move_uses_the_previous_close(self) -> None:
        self.assertAlmostEqual(rules.daily_return(11.0, 10.0), 0.1)

    def test_an_ex_rights_day_is_not_a_crash(self) -> None:
        # 10 送 10：昨天收 20，今天的昨收已经调成 10，今天收 10.2
        self.assertAlmostEqual(rules.daily_return(10.2, 10.0), 0.02)

    def test_bad_rows_are_dropped(self) -> None:
        self.assertIsNone(rules.daily_return(None, 10.0))
        self.assertIsNone(rules.daily_return(10.0, 0.0))
        self.assertIsNone(rules.daily_return(0.0, 10.0))

    def test_a_first_day_ipo_jump_is_capped(self) -> None:
        self.assertEqual(rules.daily_return(30.0, 10.0), rules.RETURN_CLIP)

    def test_returns_compound(self) -> None:
        self.assertAlmostEqual(rules.compound([0.1, -0.1]), -0.01)
        self.assertEqual(rules.compound([]), 0.0)


class ModelViewTests(unittest.TestCase):
    def test_a_high_rank_agrees_with_buying_and_disagrees_with_selling(self) -> None:
        self.assertEqual(rules.model_view(rules.UP, 0.9), "agree")
        self.assertEqual(rules.model_view(rules.DOWN, 0.9), "disagree")

    def test_a_low_rank_agrees_with_selling(self) -> None:
        self.assertEqual(rules.model_view(rules.DOWN, 0.1), "agree")
        self.assertEqual(rules.model_view(rules.UP, 0.1), "disagree")

    def test_the_middle_and_missing_ranks_take_no_side(self) -> None:
        self.assertEqual(rules.model_view(rules.UP, 0.5), "neutral")
        self.assertEqual(rules.model_view(rules.UP, None), "unknown")


class DedupeTests(unittest.TestCase):
    def test_the_last_plan_of_the_day_wins(self) -> None:
        base = {"owner": 1, "advice_date": "20261110", "symbol": "600000.SH", "direction": rules.DOWN}
        kept = rules.latest_per_day([{**base, "order": 1, "tag": "早盘"}, {**base, "order": 2, "tag": "午盘"}])
        self.assertEqual([item["tag"] for item in kept], ["午盘"])

    def test_other_days_stocks_directions_and_users_are_kept_apart(self) -> None:
        base = {"owner": 1, "advice_date": "20261110", "symbol": "600000.SH", "direction": rules.DOWN, "order": 1}
        items = [base, {**base, "advice_date": "20261111"}, {**base, "symbol": "000001.SZ"}, {**base, "direction": rules.UP}, {**base, "owner": 2}]
        self.assertEqual(len(rules.latest_per_day(items)), 5)


def _row(result: str, side: str = rules.UP, excess: float = 0.03) -> dict:
    return {"verdict": result, "direction": side, "excess": excess}


class SummaryTests(unittest.TestCase):
    def test_counts_and_rate(self) -> None:
        rows = [_row(rules.RIGHT)] * 6 + [_row(rules.WRONG, excess=-0.03)] * 2 + [_row(rules.FLAT, excess=0.0)] * 2
        result = rules.summarize(rows)
        self.assertEqual((result["total"], result["right"], result["wrong"], result["flat"]), (10, 6, 2, 2))
        self.assertAlmostEqual(result["right_rate"], 0.75)  # 差不多的不进分母
        self.assertAlmostEqual(result["avg_edge"], (6 * 0.03 - 2 * 0.03) / 10)

    def test_pending_and_missing_rows_do_not_count(self) -> None:
        result = rules.summarize([_row(rules.RIGHT), _row(None), _row(rules.NO_DATA)])
        self.assertEqual(result["total"], 1)

    def test_a_sell_that_avoided_a_drop_has_a_positive_edge(self) -> None:
        result = rules.summarize([_row(rules.RIGHT, rules.DOWN, -0.05)])
        self.assertAlmostEqual(result["avg_edge"], 0.05)

    def test_no_conclusion_below_the_sample_floor(self) -> None:
        self.assertFalse(rules.summarize([_row(rules.RIGHT)] * (rules.MIN_SAMPLES - 1))["enough"])
        self.assertTrue(rules.summarize([_row(rules.RIGHT)] * rules.MIN_SAMPLES)["enough"])

    def test_an_empty_group_has_no_rate(self) -> None:
        result = rules.summarize([])
        self.assertIsNone(result["right_rate"])
        self.assertIsNone(result["avg_edge"])


if __name__ == "__main__":
    unittest.main()
