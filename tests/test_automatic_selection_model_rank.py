"""模型排名挑战者：打分、门禁、定期换仓和持有带。不连数据库。"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from tests.automatic_selection_fixtures import history_dates

from backend.app.quant.automatic_selection_presentation import (
    _rebalance_digest,
    explanation_evidence,
    method_view,
    operation_view,
)
from backend.app.quant.automatic_selection_scoring import (
    DEFAULT_STRATEGY_CONFIG,
    LAYERED_STRATEGY_CONFIG,
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


class LayeredChallengerTests(unittest.TestCase):
    def _layered(self, symbol: str, score: float | None, **overrides):
        return _row(symbol, None, factor_values={} if score is None else {"layered_swing": score}, **overrides)

    def test_config_only_swaps_the_feature(self) -> None:
        # 除了换成分层总分，选股池、换仓、持有带都和模型排名前 30 一样，前瞻评估才能直接比
        layered = validate_config(LAYERED_STRATEGY_CONFIG)
        model = validate_config(MODEL_RANK_STRATEGY_CONFIG)
        self.assertEqual(layered["feature_weights"], {"layered_swing": 1.0})
        self.assertEqual({k: v for k, v in layered.items() if k != "feature_weights"},
                         {k: v for k, v in model.items() if k != "feature_weights"})
        self.assertNotEqual(config_hash(layered), config_hash(model))

    def test_ranks_by_layered_score_and_excludes_unscored(self) -> None:
        config = validate_config({**LAYERED_STRATEGY_CONFIG, "top_n": 2, "quality": {**LAYERED_STRATEGY_CONFIG["quality"], "min_eligible_count": 1}})
        rows = [
            self._layered("600001.SH", 60.0),
            self._layered("600002.SH", 90.0, pe_ttm=-5.0),  # 亏损股不额外剔除，分层自己在基本面层处理
            self._layered("600003.SH", 75.0),
            self._layered("600004.SH", None),  # 被分层排雷或否决排除的没有分
        ]
        candidates = {item["symbol"]: item for item in build_candidates(rows, config, target_trade_date=TARGET_DATE)}
        self.assertEqual(candidates["600004.SH"]["exclusion_codes"], ["layered_score_missing"])
        self.assertTrue(candidates["600002.SH"]["eligible"])
        scored = list(candidates.values())
        score_candidates(scored, config)
        self.assertEqual(_selected(scored), ["600002.SH", "600003.SH"])

    def test_method_says_it_is_not_validated(self) -> None:
        view = method_view(validate_config(LAYERED_STRATEGY_CONFIG))
        self.assertEqual([item["key"] for item in view["dimensions"]], ["layered"])
        self.assertIn("没有经过历史回测", view["scoring"])
        self.assertFalse(any("亏损" in text for text in view["filters"]))
        self.assertTrue(any("分层打分没有排除" in text for text in view["filters"]))


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
    @patch("backend.app.quant.automatic_selection_presentation.repository.eligible_score_pool", return_value=[])
    def test_ai_method_describes_the_actual_strategy(self, _pool) -> None:
        model = explanation_evidence({"run_id": "unit-model-method", "config": validate_config(MODEL_RANK_STRATEGY_CONFIG)}, [])
        self.assertIn("模型排名", model["method"])
        self.assertIn("每 10 个交易日", model["method"])
        self.assertNotIn("剔除亏损", model["method"])
        self.assertNotIn("同一行业最多", model["method"])
        official = explanation_evidence({"run_id": "unit-official-method", "config": validate_config(DEFAULT_STRATEGY_CONFIG)}, [])
        self.assertIn("剔除亏损", official["method"])
        self.assertIn("同一行业最多 4 只", official["method"])
        self.assertNotIn("模型排名", official["method"])

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

    def _picks(self, states: list[str]) -> list[dict]:
        return [
            {
                "symbol": f"60000{index}.SH" if index != 2 else "688002.SH",
                "name": f"测试{index}",
                "industry": "银行",
                "close_price": 10.0 * index,
                "change_state": state,
            }
            for index, state in enumerate(states, start=1)
        ]

    def _model_run(self, rebalance: dict) -> dict:
        return {
            "run_id": "unit-operation",
            "config": validate_config(MODEL_RANK_STRATEGY_CONFIG),
            "sync_summary": {"rebalance": rebalance},
        }

    def test_hold_day_says_do_nothing(self) -> None:
        run = self._model_run({"due": False, "anchor_trade_date": "20260824", "days_since_anchor": 4, "every_days": 10})
        view = operation_view(run, self._picks(["retained", "retained", "retained"]), [])
        self.assertEqual(view["kind"], "hold")
        self.assertEqual(view["days_until_next"], 6)
        self.assertEqual(view["text"], "今天不用操作。名单是 8 月 24 日定的，还有 6 个交易日换仓。")
        self.assertEqual(view["added"], [])

        dropped = operation_view(run, self._picks(["retained", "retained"]), [{"symbol": "600009.SH", "name": "测试9"}])
        self.assertIn("测试9已不符合条件", dropped["text"])

        digest = _rebalance_digest(
            {"picks": self._picks(["retained"]), "trade_date": TARGET_DATE, "run": {"run_id": "r1"}, "track_brief": None},
            view,
        )
        self.assertEqual(digest["brief"], "8 月 28 日：今天不用操作。名单是 8 月 24 日定的，还有 6 个交易日换仓。")
        self.assertNotIn("调入", digest["content"])
        self.assertIn("不构成投资建议", digest["content"])

    def test_rebalance_day_lists_what_goes_in_and_out(self) -> None:
        run = self._model_run({"due": True, "anchor_trade_date": TARGET_DATE, "days_since_anchor": 0, "every_days": 10})
        picks = self._picks(["added", "retained", "added"])
        removed = [{"symbol": "600009.SH", "name": "测试9"}]
        view = operation_view(run, picks, removed)
        self.assertEqual(view["kind"], "rebalance")
        self.assertEqual(view["days_until_next"], 10)
        self.assertTrue(view["text"].startswith("今天换仓，调入 2 只，调出 1 只，保留 1 只"))
        self.assertEqual([item["symbol"] for item in view["added"]], ["600001.SH", "600003.SH"])
        # 科创板一手 200 股：10×100、20×200、30×100，等额按最贵的一手算
        self.assertEqual(view["basket"]["one_lot_total"], 8000.0)
        self.assertEqual(view["basket"]["per_stock"], 4000.0)
        self.assertEqual(view["basket"]["equal_total"], 12000.0)

        digest = _rebalance_digest(
            {"picks": picks, "trade_date": TARGET_DATE, "run": {"run_id": "r1"}, "track_brief": None}, view,
        )
        self.assertEqual(digest["inbox_title"], "选股换仓（8 月 28 日）：调入 2 只，调出 1 只")
        self.assertIn("1. **测试1（600001.SH）** · 银行 · 收盘 10.00 元", digest["content"])
        self.assertIn("**调出 1 只**：测试9（600009.SH）", digest["content"])
        self.assertIn("只挑其中几只没有依据", digest["content"])

        first = operation_view(run, self._picks(["added", "added"]), [])
        self.assertTrue(first["text"].startswith("今天出第一期名单，共 2 只"))

    def test_strategy_without_rebalance_is_marked_daily(self) -> None:
        run = {"run_id": "unit-daily", "config": validate_config(DEFAULT_STRATEGY_CONFIG), "sync_summary": {}}
        view = operation_view(run, self._picks(["added"]), [])
        self.assertEqual(view["kind"], "daily")
        missing_price = operation_view(run, [{**self._picks(["added"])[0], "close_price": None}], [])
        self.assertIsNone(missing_price["basket"])


if __name__ == "__main__":
    unittest.main()
