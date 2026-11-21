"""研究数据底座：续跑、翻页、失败重试、完整性检查。打桩接口，不联网。"""
from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.quant import research_data
from backend.app.quant.research_data import (
    DAILY_DATASETS,
    QUARTERLY_VIP_DATASETS,
    Puller,
    check,
    read_table,
    report_quarters,
)


class _Api:
    """按接口名返回假数据，并记录调用。"""

    def __init__(self, days: list[str], *, fail_first: int = 0) -> None:
        self.days = days
        self.calls: list[tuple[str, dict]] = []
        self.fail_first = fail_first

    def __call__(self, api: str, params: dict, fields: str) -> list[list]:
        self.calls.append((api, params))
        if api == "trade_cal":
            return [[day] for day in self.days]
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("连接断开")
        width = len(fields.split(","))
        return [[f"000001.SZ"] + [1.5] * (width - 1)]


class PullTests(unittest.TestCase):
    DAYS = ["20170103", "20180103"]

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.out = Path(self.dir.name)
        for target, value in ((research_data, "_RETRY_WAITS"),):
            patcher = patch.object(target, value, (0, 0, 0))
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, api: _Api, **kwargs) -> list[str]:
        return Puller(api, self.out, workers=2, log=lambda message: None, **kwargs).run("20170101", "20181231")

    def test_every_dataset_is_written_from_its_own_start_date(self) -> None:
        self.assertEqual(self._run(_Api(self.DAYS)), [])
        self.assertTrue((self.out / "daily" / "20170103.tsv.gz").exists())
        self.assertTrue((self.out / "cyq_perf" / "20180103.tsv.gz").exists())
        self.assertFalse((self.out / "cyq_perf" / "20170103.tsv.gz").exists())
        self.assertTrue((self.out / "namechange.tsv.gz").exists())
        self.assertTrue((self.out / "stock_basic_D.tsv.gz").exists())
        self.assertEqual(len(list((self.out / "index_daily").glob("*.tsv.gz"))), len(research_data.INDEX_CODES))

    def test_files_read_back_with_field_names(self) -> None:
        self._run(_Api(self.DAYS))
        rows = read_table(self.out / "daily" / "20170103.tsv.gz")
        self.assertEqual(rows[0]["ts_code"], "000001.SZ")
        self.assertEqual(rows[0]["close"], "1.5")

    def test_a_second_run_only_fetches_what_is_missing(self) -> None:
        self._run(_Api(self.DAYS))
        (self.out / "daily" / "20180103.tsv.gz").unlink()
        again = _Api(self.DAYS)
        self._run(again)
        pulled = [call for call in again.calls if call[0] != "trade_cal"]
        self.assertEqual(pulled, [("daily", {"trade_date": "20180103", "limit": research_data._PAGE_SIZE, "offset": 0})])

    def test_long_days_are_paged(self) -> None:
        class Paged(_Api):
            def __call__(self, api, params, fields):
                self.calls.append((api, params))
                if api == "trade_cal":
                    return [["20180103"]]
                width = len(fields.split(","))
                size = research_data._PAGE_SIZE if params.get("offset", 0) == 0 and api == "daily" else 1
                return [["000001.SZ"] + [1.0] * (width - 1)] * size

        self._run(Paged(["20180103"]))
        self.assertEqual(len(read_table(self.out / "daily" / "20180103.tsv.gz")), research_data._PAGE_SIZE + 1)

    def test_a_flaky_connection_is_retried(self) -> None:
        self.assertEqual(self._run(_Api(self.DAYS, fail_first=2)), [])

    def test_too_many_failures_stop_the_run_and_keep_what_was_fetched(self) -> None:
        class Dead(_Api):
            def __call__(self, api, params, fields):
                if api == "trade_cal":
                    return [[day] for day in self.days]
                raise RuntimeError("超过频率限制")

        days = [f"2018{month:02d}03" for month in range(1, 13)]
        with patch.object(research_data, "_MAX_CONSECUTIVE_FAILURES", 3):
            missing = self._run(Dead(days))
        self.assertTrue(missing)

    def test_an_interrupted_write_leaves_no_partial_file(self) -> None:
        target = self.out / "daily" / "20180103.tsv.gz"
        with patch.object(research_data.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                research_data._write_table(target, "ts_code,close", [["a", 1]])
        self.assertFalse(target.exists())

    def test_check_reports_gaps(self) -> None:
        self._run(_Api(self.DAYS))
        (self.out / "moneyflow" / "20170103.tsv.gz").unlink()
        report = check(self.out)
        self.assertEqual(report["daily_days"], 2)
        self.assertEqual(report["moneyflow"]["missing"], ["20170103"])
        self.assertEqual(report["cyq_perf"], {"files": 1, "expected": 1, "missing": []})
        self.assertEqual(report["daily_rows_last_day"], 1)

    def test_the_token_is_not_kept_on_disk(self) -> None:
        self._run(_Api(self.DAYS))
        for path in self.out.rglob("*.gz"):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                self.assertNotIn("token", fh.read())

    def test_every_dataset_has_a_code_column_first(self) -> None:
        for dataset in (*DAILY_DATASETS, *QUARTERLY_VIP_DATASETS):
            self.assertTrue(dataset.fields.startswith("ts_code,"), dataset.name)
        for q_dataset in QUARTERLY_VIP_DATASETS:
            self.assertIn("ann_date", q_dataset.fields.split(","), q_dataset.name)

    def test_quarterly_vip_datasets_support_only_quarterly_and_paging(self) -> None:
        self.assertEqual(
            report_quarters("20170101", "20181231"),
            ["20170331", "20170630", "20170930", "20171231", "20180331", "20180630", "20180930", "20181231"],
        )

        class PagedVip(_Api):
            def __call__(self, api, params, fields):
                self.calls.append((api, params))
                width = len(fields.split(","))
                size = research_data._PAGE_SIZE if params.get("offset", 0) == 0 and api == "cashflow_vip" else 1
                return [["000001.SZ"] + [2.0] * (width - 1)] * size

        api = PagedVip([])
        missing = Puller(api, self.out, workers=2, log=lambda m: None).run(
            "20180101", "20180630", only_quarterly=True
        )
        self.assertEqual(missing, [])
        self.assertFalse(any(name == "trade_cal" for name, _ in api.calls))
        self.assertEqual(
            len(read_table(self.out / "cashflow_vip" / "20180331.tsv.gz")),
            research_data._PAGE_SIZE + 1,
        )
        (self.out / "income_vip" / "20180630.tsv.gz").unlink()
        report = check(self.out)
        self.assertEqual(report["income_vip"]["missing"], ["20180630"])
        self.assertEqual(report["cashflow_vip"]["missing"], [])

    def test_unsettled_recent_quarter_refreshes_after_disclosure_advances(self) -> None:
        import os as _os
        from datetime import datetime as _dt

        first_api = _Api([])
        Puller(first_api, self.out, workers=2, log=lambda m: None).run(
            "20261001", "20261231", only_quarterly=True
        )
        feb_ts = _dt(2027, 2, 15, 12, 0).timestamp()
        for q_ds in QUARTERLY_VIP_DATASETS:
            _os.utime(self.out / q_ds.name / "20261231.tsv.gz", (feb_ts, feb_ts))

        same_day_api = _Api([])
        Puller(same_day_api, self.out, workers=2, log=lambda m: None).run(
            "20261001", "20270215", only_quarterly=True
        )
        self.assertEqual(same_day_api.calls, [], "同一截止日不重复重拉")

        settled_api = _Api([])
        Puller(settled_api, self.out, workers=2, log=lambda m: None).run(
            "20261001", "20270510", only_quarterly=True
        )
        refreshed_2026 = [c for c in settled_api.calls if c[1].get("period") == "20261231"]
        self.assertEqual(len(refreshed_2026), len(QUARTERLY_VIP_DATASETS), "披露期推进后自动补刷未定稿年报")


if __name__ == "__main__":
    unittest.main()
