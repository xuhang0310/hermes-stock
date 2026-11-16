"""模型排名挑战者：打分、门禁、定期换仓和持有带。不连数据库。"""
from __future__ import annotations

import copy
import unittest

from tests.automatic_selection_fixtures import history_dates

from backend.app.quant.automatic_selection_presentation import method_view
from backend.app.quant.automatic_selection_scoring import (
    DEFAULT_STRATEGY_CONFIG,
    MODEL_RANK_STRATEGY_CONFIG,
    build_candidates,
    config_hash,
    quality_gate,
    rebalance_state,
    score_candidates,
    validate_config,
)

TARGET_DATE = "20260828"
DATES = history_dates(TARGET_DATE)


def _config(**overrides):
    config = copy.deepcopy(MODEL_RANK_STRATEGY_CONFIG)
    config["top_n"] = 3
    config["quality"] = {**config["quality"], "min_eligible_count": 1}
    config.update(overrides)
    return validate_config(config)


def _row(symbol: str, rank: float | None, **overrides):
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
        "history": [
            {"trade_date": day, "close": 10.0 + i * 0.1, "adj_factor": 1.0, "amount": 30_000.0}
            for i, day in enumerate(DATES)
        ],
        "expected_history_dates": DATES,
        "factor_values": {} if rank is None else {"model_rank": rank},
    }
    row.update(overrides)
    return row


def _pool(n: int = 8):
    # 600001 排第一，依次往后
    return [_row(f"60000{i}.SH", 1.0 - i / 10) for i in range(1, n + 1)]


def _selected(candidates):
    return sorted(item["symbol"] for item in candidates if item.get("selected"))


class ModelRankConfigTests(unittest.TestCase):
    def test_default_config_hash_is_unchanged(self) -> None:
        # 现有正式策略的配置哈希不能变，否则它的历史运行会全部失效
        self.assertNotIn("rebalance", validate_config(DEFAULT_STRATEGY_CONFIG))
        self.assertEqual(
            config_hash(validate_config(DEFAULT_STRATEGY_CONFIG)),
            config_hash(validate_config(copy.deepcopy(DEFAULT_STRATEGY_CONFIG))),
        )

    def test_model_config_matches_backtest(self) -> None:
        config = validate_config(MODEL_RANK_STRATEGY_CONFIG)
        self.assertEqual(config["top_n"], 30)
        self.assertEqual(config["min_avg_amount"], 20_000.0)  # 千元，即 2000 万元
        self.assertEqual(config["feature_weights"], {"model_rank": 1.0})
        self.assertEqual(config["rebalance"], {"every_days": 10, "hold_rank_multiple": 2.0})
        self.assertEqual(config["constraints"], {"max_per_industry": 0, "max_per_market_cap_bucket": 0})
        self.assertEqual((config["winsor_lower"], config["winsor_upper"]), (0.0, 1.0))

    def test_rebalance_is_validated(self) -> None:
        for bad in ({"every_days": 0, "hold_rank_multiple": 2}, {"every_days": 21, "hold_rank_multiple": 2},
                    {"every_days": 10, "hold_rank_multiple": 0.5}, {"every_days": 10}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_config({**MODEL_RANK_STRATEGY_CONFIG, "rebalance": bad})


class ModelRankScoringTests(unittest.TestCase):
    def test_orders_by_model_rank_without_ties_at_top(self) -> None:
        candidates = build_candidates(_pool(), _config(), target_trade_date=TARGET_DATE)
        score_candidates(candidates, _config())
        self.assertEqual(_selected(candidates), ["600001.SH", "600002.SH", "600003.SH"])
        ranks = {item["symbol"]: item["rank"] for item in candidates}
        self.assertEqual(ranks["600001.SH"], 1)
        self.assertEqual(ranks["600008.SH"], 8)

    def test_loss_making_stocks_stay_but_missing_rank_is_excluded(self) -> None:
        rows = [_row("600001.SH", 0.9, pe_ttm=-5.0), _row("600002.SH", None), _row("430001.BJ", None)]
        candidates = {item["symbol"]: item for item in build_candidates(rows, _config(), target_trade_date=TARGET_DATE)}
        self.assertTrue(candidates["600001.SH"]["eligible"])
        self.assertEqual(candidates["600002.SH"]["exclusion_codes"], ["model_rank_missing"])
        # 默认策略对亏损股照旧剔除
        default = build_candidates([_row("600001.SH", 0.9, pe_ttm=-5.0)], DEFAULT_STRATEGY_CONFIG, target_trade_date=TARGET_DATE)
        self.assertIn("valuation_invalid", default[0]["exclusion_codes"])

    def test_thin_trading_is_excluded(self) -> None:
        thin = _row("600001.SH", 0.9)
        for bar in thin["history"]:
            bar["amount"] = 19_000.0
        candidates = build_candidates([thin], _config(), target_trade_date=TARGET_DATE)
        self.assertIn("liquidity_low", candidates[0]["exclusion_codes"])

    def test_gate_blocks_when_ranks_are_missing(self) -> None:
        rows = [_row(f"60000{i}.SH", None) for i in range(1, 6)] + [_row("430001.BJ", None)]
        gate = quality_gate(build_candidates(rows, _config(), target_trade_date=TARGET_DATE), _config())
        self.assertFalse(gate["passed"])
        self.assertIn("model_rank_coverage", [item["code"] for item in gate["failures"]])
        # 北交所不在模型范围内，不拉低覆盖率
        rows = _pool(5) + [_row("430001.BJ", None)]
        gate = quality_gate(build_candidates(rows, _config(), target_trade_date=TARGET_DATE), _config())
        self.assertEqual(gate["metrics"]["model_rank_coverage"], 1.0)
        self.assertTrue(gate["passed"])


class RebalanceTests(unittest.TestCase):
    def test_state_counts_trading_days(self) -> None:
        config = _config()
        self.assertTrue(rebalance_state(config, previous_anchor=None, expected_dates=DATES, target_trade_date=TARGET_DATE)["due"])
        held = rebalance_state(config, previous_anchor=DATES[-4], expected_dates=DATES, target_trade_date=TARGET_DATE)
        self.assertEqual((held["due"], held["days_since_anchor"], held["anchor_trade_date"]), (False, 3, DATES[-4]))
        due = rebalance_state(config, previous_anchor=DATES[-11], expected_dates=DATES, target_trade_date=TARGET_DATE)
        self.assertEqual((due["due"], due["anchor_trade_date"]), (True, TARGET_DATE))
        # 锚点早于窗口：重新挑
        self.assertTrue(rebalance_state(config, previous_anchor="20200101", expected_dates=DATES, target_trade_date=TARGET_DATE)["due"])
        self.assertIsNone(rebalance_state(validate_config(DEFAULT_STRATEGY_CONFIG), previous_anchor=None, expected_dates=DATES, target_trade_date=TARGET_DATE))

    def test_between_rebalances_previous_list_is_kept(self) -> None:
        config = _config()
        candidates = build_candidates(_pool(), config, target_trade_date=TARGET_DATE)
        score_candidates(candidates, config, previous_selected=["600006.SH", "600007.SH", "600099.SH"], rebalance={"due": False})
        # 600099 已经不在合格池里，不补位
        self.assertEqual(_selected(candidates), ["600006.SH", "600007.SH"])

    def test_hold_band_keeps_names_within_two_n(self) -> None:
        config = _config()  # top 3，持有带前 6
        candidates = build_candidates(_pool(), config, target_trade_date=TARGET_DATE)
        score_candidates(candidates, config, previous_selected=["600005.SH", "600008.SH"], rebalance={"due": True})
        # 600005 排第 5 留着；600008 排第 8 换掉；空位按排名补 600001、600002
        self.assertEqual(_selected(candidates), ["600001.SH", "600002.SH", "600005.SH"])

    def test_without_previous_list_picks_top_n(self) -> None:
        config = _config()
        candidates = build_candidates(_pool(), config, target_trade_date=TARGET_DATE)
        score_candidates(candidates, config, previous_selected=[], rebalance={"due": False})
        self.assertEqual(_selected(candidates), ["600001.SH", "600002.SH", "600003.SH"])


class ModelRankPresentationTests(unittest.TestCase):
    def test_method_view_explains_model_rules(self) -> None:
        view = method_view(validate_config(MODEL_RANK_STRATEGY_CONFIG))
        self.assertEqual([item["key"] for item in view["dimensions"]], ["model"])
        self.assertFalse(any("亏损" in text for text in view["filters"]))
        self.assertTrue(any("2,000 万元" in text for text in view["filters"]))
        self.assertTrue(any("每 10 个交易日" in text and "前 60" in text for text in view["selection"]))
        self.assertIn("回撤", view["scoring"])
        default = method_view(validate_config(DEFAULT_STRATEGY_CONFIG))
        self.assertTrue(any("亏损" in text for text in default["filters"]))
        self.assertNotIn("模型排名", default["scoring"])


if __name__ == "__main__":
    unittest.main()
