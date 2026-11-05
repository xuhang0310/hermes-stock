"""特征库：不能用到未来数据、拆股不能造假信号、开发区间读不到验证区间的价格。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from backend.app.quant import research_features as rf
from backend.app.quant.research_data import _write_table
from backend.app.quant.research_panel import FIELDS, Panel, build_cache, st_matrix

CODES = ["000001.SZ", "000002.SZ", "300001.SZ", "600000.SH", "600001.SH", "688001.SH"]


def _dates(count: int, start: str = "2017-01-02") -> list[str]:
    return [day.strftime("%Y%m%d") for day in pd.bdate_range(start, periods=count)]


def _panel(count: int = 260, seed: int = 7, dates: list[str] | None = None) -> Panel:
    rng = np.random.default_rng(seed)
    n = len(CODES)
    close = 20 * np.cumprod(1 + rng.normal(0, 0.02, (count, n)), axis=0)
    pre = np.vstack([close[0], close[:-1]])
    open_ = pre * (1 + rng.normal(0, 0.005, (count, n)))
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, (count, n)))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, (count, n)))
    arrays: dict[tuple[str, str], np.ndarray] = {
        ("daily", "open"): open_,
        ("daily", "high"): high,
        ("daily", "low"): low,
        ("daily", "close"): close,
        ("daily", "pre_close"): pre,
        ("daily", "vol"): rng.uniform(1e3, 1e4, (count, n)),
        ("daily", "amount"): rng.uniform(1e4, 1e5, (count, n)),
        ("daily_basic", "turnover_rate_f"): rng.uniform(0.5, 5, (count, n)),
        ("daily_basic", "volume_ratio"): rng.uniform(0.5, 2, (count, n)),
        ("daily_basic", "pe_ttm"): rng.uniform(5, 50, (count, n)),
        ("daily_basic", "pb"): rng.uniform(0.5, 5, (count, n)),
        ("daily_basic", "total_mv"): rng.uniform(1e5, 1e6, (count, n)),
        ("daily_basic", "circ_mv"): rng.uniform(1e5, 1e6, (count, n)),
        ("moneyflow", "buy_lg_amount"): rng.uniform(1e3, 1e4, (count, n)),
        ("moneyflow", "sell_lg_amount"): rng.uniform(1e3, 1e4, (count, n)),
        ("moneyflow", "buy_elg_amount"): rng.uniform(1e3, 1e4, (count, n)),
        ("moneyflow", "sell_elg_amount"): rng.uniform(1e3, 1e4, (count, n)),
        ("moneyflow", "net_mf_amount"): rng.normal(0, 1e3, (count, n)),
        ("stk_limit", "up_limit"): pre * 1.1,
        ("stk_limit", "down_limit"): pre * 0.9,
        ("cyq_perf", "cost_5pct"): close * 0.8,
        ("cyq_perf", "cost_50pct"): close * 0.95,
        ("cyq_perf", "cost_95pct"): close * 1.2,
        ("cyq_perf", "weight_avg"): close * 0.96,
        ("cyq_perf", "winner_rate"): rng.uniform(0, 100, (count, n)),
    }
    return Panel(dates or _dates(count), CODES, arrays)


def _truncate(panel: Panel, rows: int) -> Panel:
    arrays = {key: np.asarray(value)[:rows] for key, value in panel._arrays.items()}
    return Panel(panel.dates[:rows], panel.codes, arrays)


class NoLookaheadTests(unittest.TestCase):
    def test_no_feature_changes_when_later_bars_are_added(self) -> None:
        full = _panel(260)
        cut = 200
        short = rf.compute_all(_truncate(full, cut))
        long = rf.compute_all(full)
        for feature in rf.FEATURES:
            a, b = short[feature.name][cut - 1], long[feature.name][cut - 1]
            mask = ~np.isnan(a)
            self.assertTrue(mask.any(), feature.name)
            np.testing.assert_allclose(a[mask], b[mask], rtol=1e-9, err_msg=feature.name)
            np.testing.assert_array_equal(np.isnan(a), np.isnan(b), err_msg=feature.name)

    def test_every_feature_has_a_group_and_a_unique_name(self) -> None:
        names = [feature.name for feature in rf.FEATURES]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 45)


class AdjustmentTests(unittest.TestCase):
    def test_a_split_is_not_a_crash(self) -> None:
        panel = _panel(260)
        arrays = dict(panel._arrays)
        for name in ("open", "high", "low", "close", "pre_close"):
            array = np.array(arrays[("daily", name)])
            start = 200 if name == "pre_close" else 200
            array[start:, 0] /= 2
            arrays[("daily", name)] = array
        split = Panel(panel.dates, panel.codes, arrays)
        np.testing.assert_allclose(split.adj_close_filled[:, 0], panel.adj_close_filled[:, 0], rtol=1e-9)
        a = rf.compute_all(split)["ret_20"][250, 0]
        b = rf.compute_all(panel)["ret_20"][250, 0]
        self.assertAlmostEqual(a, b, places=8)

    def test_a_suspension_does_not_create_a_return(self) -> None:
        panel = _panel(260)
        arrays = dict(panel._arrays)
        for name in FIELDS["daily"]:
            array = np.array(arrays[("daily", name)])
            array[100:105, 1] = np.nan
            arrays[("daily", name)] = array
        gap = Panel(panel.dates, panel.codes, arrays)
        self.assertTrue(np.isnan(gap.adjusted("close")[102, 1]))
        self.assertEqual(gap.adj_close_filled[102, 1], gap.adj_close_filled[99, 1])
        self.assertFalse(gap.traded[102, 1])
        values = rf.compute_all(gap)["ret_1"]
        self.assertTrue(np.isnan(values[102, 1]))


class LabelTests(unittest.TestCase):
    def test_the_label_buys_at_the_next_open_and_sells_at_the_nth_close(self) -> None:
        panel = _panel(100)
        label = rf.forward_return(panel, 10, last_exit_date="99999999")
        expected = panel.adj_close_filled[60, 2] / panel.adjusted("open")[51, 2] - 1
        self.assertAlmostEqual(label[50, 2], expected)

    def test_the_label_cannot_read_past_the_last_exit_date(self) -> None:
        panel = _panel(100)
        last = panel.dates[70]
        label = rf.forward_return(panel, 10, last_exit_date=last)
        self.assertFalse(np.isnan(label[60]).all())
        self.assertTrue(np.isnan(label[61]).all())

    def test_a_stock_locked_at_limit_up_next_day_cannot_be_bought(self) -> None:
        panel = _panel(100)
        arrays = dict(panel._arrays)
        close = np.array(arrays[("daily", "close")])
        pre = np.array(arrays[("daily", "pre_close")])
        close[51, 3] = pre[51, 3] * 1.1
        for name in ("open", "high", "low"):
            array = np.array(arrays[("daily", name)])
            array[51, 3] = close[51, 3]
            arrays[("daily", name)] = array
        arrays[("daily", "close")] = close
        label = rf.forward_return(Panel(panel.dates, panel.codes, arrays), 10, last_exit_date="99999999")
        self.assertTrue(np.isnan(label[50, 3]))
        self.assertFalse(np.isnan(label[50, 2]))

    def test_the_validation_period_is_locked_by_default(self) -> None:
        with self.assertRaises(ValueError):
            rf.check_range(None, "20190102", allow_validation_data=False)
        rf.check_range(None, "20181231", allow_validation_data=False)
        rf.check_range(None, "20190102", allow_validation_data=True)


class IcTests(unittest.TestCase):
    def setUp(self) -> None:
        self.panel = _panel(260, dates=_dates(260, "2016-01-04"))
        self.label = rf.forward_return(self.panel, 10, last_exit_date="99999999")
        self.rows = np.arange(130, 240)

    def test_a_perfect_feature_scores_one_and_its_opposite_minus_one(self) -> None:
        self.assertAlmostEqual(rf.feature_ic(self.panel, self.label, self.label, self.rows).mean(), 1.0, places=6)
        self.assertAlmostEqual(rf.feature_ic(self.panel, -self.label, self.label, self.rows).mean(), -1.0, places=6)

    def test_new_listings_are_outside_the_universe(self) -> None:
        self.assertFalse(self.panel.universe[50].any())
        self.assertTrue(self.panel.universe[130].all())

    def test_st_stocks_are_outside_the_universe(self) -> None:
        st = np.zeros(self.panel.universe.shape, dtype=bool)
        st[:, 0] = True
        panel = Panel(self.panel.dates, self.panel.codes, self.panel._arrays, st=st)
        self.assertFalse(panel.universe[200, 0])
        self.assertTrue(panel.universe[200, 1])


class SelectionTests(unittest.TestCase):
    GOOD = {"ic": -0.03, "icir": -0.5, "years_agree": 5, "years": 7}

    def test_all_three_conditions_are_needed(self) -> None:
        self.assertTrue(rf.passes(self.GOOD))
        self.assertFalse(rf.passes({**self.GOOD, "ic": -0.019}))
        self.assertFalse(rf.passes({**self.GOOD, "icir": -0.29}))
        self.assertFalse(rf.passes({**self.GOOD, "years_agree": 4}))
        self.assertFalse(rf.passes({**self.GOOD, "years": 0, "years_agree": 0}))

    def test_icir_skips_overlapping_days(self) -> None:
        index = [f"2017{i:04d}" for i in range(100)]
        ic = pd.Series(np.tile([0.1] + [0.0] * 9, 10), index=index)
        stats = rf.summarize_ic(ic, step=10)
        self.assertAlmostEqual(stats["ic"], 0.01)
        self.assertEqual(stats["icir"], 0.0)

    def test_year_agreement_follows_the_overall_sign(self) -> None:
        index = ["20170101", "20170102", "20180101", "20180102", "20190101", "20190102"]
        stats = rf.summarize_ic(pd.Series([-0.05, -0.05, -0.05, -0.05, 0.01, 0.01], index=index), step=1)
        self.assertEqual((stats["years_agree"], stats["years"]), (2, 3))

    def test_duplicates_keep_the_one_with_the_higher_icir(self) -> None:
        corr = {("b", "a"): 0.9, ("c", "a"): 0.1, ("c", "b"): 0.1}
        kept, dropped = rf.drop_correlated(["a", "b", "c"], lambda x, y: corr.get((x, y), 0.0))
        self.assertEqual(kept, ["a", "c"])
        self.assertEqual(dropped, {"b": "a"})

    def test_a_negative_correlation_counts_as_duplicate_too(self) -> None:
        kept, _ = rf.drop_correlated(["a", "b"], lambda x, y: -0.95)
        self.assertEqual(kept, ["a"])


class CacheTests(unittest.TestCase):
    def test_day_files_become_a_panel(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            fields = "ts_code," + ",".join(FIELDS["daily"])
            _write_table(out / "daily" / "20180102.tsv.gz", fields, [["000001.SZ", 1, 2, 0.5, 1.5, 1.4, 100, 1000], ["830001.BJ", 1, 1, 1, 1, 1, 1, 1]])
            _write_table(out / "daily" / "20180103.tsv.gz", fields, [["000001.SZ", 1.5, 2, 1, 1.6, 1.5, 100, 1000], ["600000.SH", 3, 3, 3, 3, 3, 3, 3]])
            _write_table(out / "stk_limit" / "20180103.tsv.gz", "ts_code,up_limit,down_limit", [["600000.SH", 3.3, 2.7]])
            _write_table(out / "namechange.tsv.gz", "ts_code,name,start_date,end_date", [["600000.SH", "ST浦发", "20180103", ""], ["000001.SZ", "平安", "20100101", ""]])
            panel = Panel.open(out)
            self.assertEqual(panel.codes, ["000001.SZ", "600000.SH"])
            self.assertEqual(panel.dates, ["20180102", "20180103"])
            self.assertAlmostEqual(float(panel.field("daily", "close")[1, 0]), 1.6, places=5)
            self.assertAlmostEqual(float(panel.field("stk_limit", "up_limit")[1, 1]), 3.3, places=5)
            self.assertTrue(np.isnan(panel.field("stk_limit", "up_limit")[0, 1]))
            self.assertEqual(panel.st.tolist(), [[False, False], [False, True]])
            del panel  # Release the memory maps before Windows removes the temporary directory.
            stamp = (out / "_cache" / "manifest.json").stat().st_mtime_ns
            build_cache(out)
            self.assertEqual((out / "_cache" / "manifest.json").stat().st_mtime_ns, stamp)

    def test_st_ends_when_the_name_changes_back(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            _write_table(out / "namechange.tsv.gz", "ts_code,name,start_date,end_date", [["600000.SH", "*ST浦发", "20180102", "20180102"]])
            st = st_matrix(out, ["20180101", "20180102", "20180103"], ["600000.SH"])
        self.assertEqual(st[:, 0].tolist(), [False, True, False])


if __name__ == "__main__":
    unittest.main()
