"""研究数据底座：把全市场历史数据按交易日拉到本地文件，训练和回测都从这里读。

这些数据量大（2012 年起全市场），不进生产库；生产库只留日常计算需要的长度。
按交易日拉，所以包含当时存在、后来退市的股票，没有幸存者偏差。

用法（token 从环境变量读，不写进代码和文件）：

    STOCK_TUSHARE_TOKEN=... python -m backend.app.quant.research_data pull --out ~/hermes-stock-research
    STOCK_TUSHARE_TOKEN=... python -m backend.app.quant.research_data pull --out ~/hermes-stock-research --only-quarterly
    python -m backend.app.quant.research_data check --out ~/hermes-stock-research

文件布局：
- 日频数据集：<out>/<数据集>/<YYYYMMDD>.tsv.gz
- 季频 VIP 财报：<out>/<数据集>/<YYYY0331|0630|0930|1231>.tsv.gz（保留 ann_date / f_ann_date 披露日供 PIT 回测）
第一行都是字段名。可以中断后续跑；已经拉到且过完法定披露期的不再拉。
一个文件写完才改名，所以目录里的文件都是完整的。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

TUSHARE_URL = "https://api.tushare.pro"
HISTORY_START = "20120101"
_RETRY_WAITS = (5, 30, 70)
_MAX_CONSECUTIVE_FAILURES = 12

Caller = Callable[[str, dict[str, Any], str], list[list[Any]]]


@dataclass(frozen=True)
class DailyDataset:
    """按交易日拉的数据集。"""

    name: str
    api: str
    fields: str
    start: str = HISTORY_START


DAILY_DATASETS = (
    DailyDataset("daily", "daily", "ts_code,open,high,low,close,pre_close,vol,amount"),
    DailyDataset("daily_basic", "daily_basic", "ts_code,turnover_rate_f,volume_ratio,pe_ttm,pb,total_mv,circ_mv"),
    DailyDataset("moneyflow", "moneyflow", "ts_code,buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount"),
    DailyDataset("stk_limit", "stk_limit", "ts_code,up_limit,down_limit"),
    # 筹码数据 TuShare 从 2018 年开始才有
    DailyDataset("cyq_perf", "cyq_perf", "ts_code,cost_5pct,cost_50pct,cost_95pct,weight_avg,winner_rate", start="20180101"),
)


@dataclass(frozen=True)
class QuarterlyDataset:
    """按财报报告期（YYYY0331/0630/0930/1231）批量拉取的全市场 VIP 财报数据集。

    保留原始 ann_date / f_ann_date 公告披露日，回测时只取 ann_date <= 交易日 的记录（PIT 口径）。
    """

    name: str
    api: str
    fields: str
    start: str = HISTORY_START


QUARTERLY_VIP_DATASETS = (
    QuarterlyDataset(
        "income_vip",
        "income_vip",
        "ts_code,ann_date,f_ann_date,end_date,report_type,basic_eps,diluted_eps,total_revenue,revenue,oper_cost,sell_exp,admin_exp,fin_exp,rd_exp,operate_profit,total_profit,n_income,n_income_attr_p",
    ),
    QuarterlyDataset(
        "balancesheet_vip",
        "balancesheet_vip",
        "ts_code,ann_date,f_ann_date,end_date,report_type,total_share,money_cap,accounts_receiv,inventories,total_cur_assets,total_assets,st_borr,lt_borr,total_cur_liab,total_liab,total_hldr_eqy_exc_min_int",
    ),
    QuarterlyDataset(
        "cashflow_vip",
        "cashflow_vip",
        "ts_code,ann_date,f_ann_date,end_date,report_type,net_profit,c_fr_sale_sg,n_cashflow_act,n_cashflow_inv_act,n_cash_flows_fnc_act,free_cashflow,c_cash_equ_end_period",
    ),
    QuarterlyDataset(
        "fina_indicator_vip",
        "fina_indicator_vip",
        "ts_code,ann_date,end_date,eps,dt_eps,roe,roe_waa,roe_dt,roa,grossprofit_margin,netprofit_margin,debt_to_assets,current_ratio,quick_ratio,tr_yoy,or_yoy,netprofit_yoy,dt_netprofit_yoy,q_sales_yoy,q_profit_yoy,ocf_yoy",
    ),
    QuarterlyDataset(
        "forecast_vip",
        "forecast_vip",
        "ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max,last_parent_net,first_ann_date,summary",
    ),
    QuarterlyDataset(
        "express_vip",
        "express_vip",
        "ts_code,ann_date,end_date,revenue,operate_profit,total_profit,n_income,total_assets,total_hldr_eqy_exc_min_int,diluted_eps,diluted_roe,yoy_net_profit,bps",
    ),
)
_QUARTER_ENDS = ("0331", "0630", "0930", "1231")
INDEX_CODES = ("000300.SH", "000001.SH", "399001.SZ", "399006.SZ", "000905.SH", "000852.SH")
INDEX_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,vol,amount"
_PAGE_SIZE = 5000


def report_quarters(start: str, end: str) -> list[str]:
    """返回 [start, end] 区间内的标准财报报告期（YYYY0331/0630/0930/1231）。"""
    if not start or not end or start > end:
        return []
    start_year = int(start[:4])
    end_year = int(end[:4])
    quarters: list[str] = []
    for year in range(start_year, end_year + 1):
        for suffix in _QUARTER_ENDS:
            period = f"{year}{suffix}"
            if start <= period <= end:
                quarters.append(period)
    return quarters


def quarter_settled_date(period: str) -> str:
    """返回某报告期法定披露截止日之后的稳定日期（过此日期拉取过的文件不再重拉）。"""
    year = int(period[:4])
    suffix = period[4:8]
    if suffix == "0331":
        return f"{year}0505"
    if suffix == "0630":
        return f"{year}0905"
    if suffix == "0930":
        return f"{year}1105"
    return f"{year + 1}0505"


def _quarter_is_complete(target: Path, period: str, end: str) -> bool:
    if not target.exists():
        return False
    written_day = datetime.fromtimestamp(target.stat().st_mtime).strftime("%Y%m%d")
    settled = quarter_settled_date(period)
    return written_day >= settled or written_day >= end


def tushare_caller(token: str) -> Caller:
    def call(api: str, params: dict[str, Any], fields: str) -> list[list[Any]]:
        body = json.dumps({"api_name": api, "token": token, "params": params, "fields": fields}).encode()
        request = urllib.request.Request(TUSHARE_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
        if payload.get("code") != 0:
            raise RuntimeError(str(payload.get("msg"))[:200])
        return (payload.get("data") or {}).get("items") or []

    return call


def _write_table(target: Path, fields: str, rows: list[list[Any]]) -> None:
    """先写临时文件再改名，进程被杀也不会留下半个文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    lines = [fields.replace(",", "\t")]
    lines.extend("\t".join("" if value is None else str(value) for value in row) for row in rows)
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.replace(tmp, target)


def read_table(path: Path) -> list[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        header, *rows = fh.read().split("\n")
    names = header.split("\t")
    return [dict(zip(names, row.split("\t"))) for row in rows if row]


class Puller:
    """多线程按天拉取。出错退避重试；连续失败太多就停，已拉到的文件保留。"""

    def __init__(self, call: Caller, out: Path, *, workers: int = 12, log: Callable[[str], None] = print) -> None:
        self.call = call
        self.out = out
        self.workers = workers
        self.log = log
        self.stop = threading.Event()
        self.failures = 0
        self.done = 0
        self._lock = threading.Lock()

    def _with_retry(self, label: str, action: Callable[[], None]) -> None:
        if self.stop.is_set():
            return
        for wait in _RETRY_WAITS:
            try:
                action()
                with self._lock:
                    self.failures = 0
                    self.done += 1
                    if self.done % 500 == 0:
                        self.log(f"{time.strftime('%H:%M:%S')} 已拉 {self.done} 个文件")
                return
            except Exception as exc:  # noqa: BLE001 - 接口偶尔断连，退避后重试
                self.log(f"{time.strftime('%H:%M:%S')} 失败 {label} {type(exc).__name__} {str(exc)[:120]}")
                time.sleep(wait)
        with self._lock:
            self.failures += 1
            if self.failures >= _MAX_CONSECUTIVE_FAILURES:
                self.log("连续失败太多，停止；已拉到的文件保留，重跑会接着拉")
                self.stop.set()

    def _pull_day(self, dataset: DailyDataset, day: str) -> None:
        target = self.out / dataset.name / f"{day}.tsv.gz"
        if target.exists():
            return

        def action() -> None:
            rows: list[list[Any]] = []
            offset = 0
            while True:
                page = self.call(dataset.api, {"trade_date": day, "limit": _PAGE_SIZE, "offset": offset}, dataset.fields)
                rows.extend(page)
                if len(page) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE
            _write_table(target, dataset.fields, rows)

        self._with_retry(f"{dataset.name}/{day}", action)

    def _pull_quarter(self, dataset: QuarterlyDataset, period: str, end: str) -> None:
        target = self.out / dataset.name / f"{period}.tsv.gz"
        if _quarter_is_complete(target, period, end):
            return

        def action() -> None:
            rows: list[list[Any]] = []
            offset = 0
            while True:
                page = self.call(dataset.api, {"period": period, "limit": _PAGE_SIZE, "offset": offset}, dataset.fields)
                rows.extend(page)
                if len(page) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE
            _write_table(target, dataset.fields, rows)

        self._with_retry(f"{dataset.name}/{period}", action)

    def _pull_index(self, code: str, start: str, end: str) -> None:
        target = self.out / "index_daily" / f"{code}.{end}.tsv.gz"
        if target.exists():
            return
        self._with_retry(
            f"index_daily/{code}",
            lambda: _write_table(target, INDEX_FIELDS, self.call("index_daily", {"ts_code": code, "start_date": start, "end_date": end}, INDEX_FIELDS)),
        )

    def _pull_paged(self, target: Path, api: str, params: dict[str, Any], fields: str) -> None:
        if target.exists():
            return

        def action() -> None:
            rows: list[list[Any]] = []
            offset = 0
            while True:
                page = self.call(api, {**params, "limit": _PAGE_SIZE, "offset": offset}, fields)
                rows.extend(page)
                if len(page) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE
            _write_table(target, fields, rows)

        self._with_retry(target.name, action)

    def run(self, start: str, end: str, *, only_quarterly: bool = False) -> list[str]:
        q_jobs = [
            (dataset, period)
            for dataset in QUARTERLY_VIP_DATASETS
            for period in report_quarters(max(start, dataset.start), end)
        ]
        if only_quarterly:
            self.log(f"{time.strftime('%H:%M:%S')} 仅拉季频 VIP 财报：{len(q_jobs)} 个文件（{start} 至 {end}）")
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                list(pool.map(lambda job: self._pull_quarter(job[0], job[1], end), q_jobs))
            missing_q = [
                f"{dataset.name}/{period}"
                for dataset, period in q_jobs
                if not (self.out / dataset.name / f"{period}.tsv.gz").exists()
            ]
            self.log(f"{time.strftime('%H:%M:%S')} 结束：共 {len(q_jobs)} 个文件，缺 {len(missing_q)} 个 {missing_q[:5]}")
            return missing_q

        calendar = self.call("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end, "is_open": "1"}, "cal_date")
        days = sorted(str(row[0]) for row in calendar)
        self.log(f"{time.strftime('%H:%M:%S')} 交易日 {len(days)} 个（{days[0]} 至 {days[-1]}），季频财报任务 {len(q_jobs)} 个")
        self._pull_paged(self.out / "namechange.tsv.gz", "namechange", {}, "ts_code,name,start_date,end_date")
        for status in ("L", "D", "P"):
            self._pull_paged(
                self.out / f"stock_basic_{status}.tsv.gz",
                "stock_basic",
                {"list_status": status},
                "ts_code,name,industry,market,list_date,delist_date",
            )
        for code in INDEX_CODES:
            self._pull_index(code, start, end)
        jobs = [(dataset, day) for dataset in DAILY_DATASETS for day in days if day >= dataset.start]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(lambda job: self._pull_day(*job), jobs))
            list(pool.map(lambda job: self._pull_quarter(job[0], job[1], end), q_jobs))
        missing = [f"{dataset.name}/{day}" for dataset, day in jobs if not (self.out / dataset.name / f"{day}.tsv.gz").exists()]
        missing.extend(
            f"{dataset.name}/{period}"
            for dataset, period in q_jobs
            if not (self.out / dataset.name / f"{period}.tsv.gz").exists()
        )
        total_jobs = len(jobs) + len(q_jobs)
        self.log(f"{time.strftime('%H:%M:%S')} 结束：共 {total_jobs} 个文件，缺 {len(missing)} 个 {missing[:5]}")
        return missing


def check(out: Path) -> dict[str, Any]:
    """数据完整性：每个数据集覆盖到哪天/哪一季、缺哪些交易日或报告期、行数和日线差多少。"""
    daily_dir = out / "daily"
    days = sorted(path.name[:8] for path in daily_dir.glob("*.tsv.gz"))
    report: dict[str, Any] = {"daily_days": len(days), "first": days[0] if days else None, "last": days[-1] if days else None}
    for dataset in DAILY_DATASETS:
        expected = [day for day in days if day >= dataset.start]
        have = {path.name[:8] for path in (out / dataset.name).glob("*.tsv.gz")}
        report[dataset.name] = {"files": len(have & set(expected)), "expected": len(expected), "missing": sorted(set(expected) - have)[:5]}
    q_periods_on_disk = sorted(
        {
            path.name[:8]
            for dataset in QUARTERLY_VIP_DATASETS
            for path in (out / dataset.name).glob("*.tsv.gz")
            if len(path.name) >= 8 and path.name[:8].isdigit()
        }
    )
    span_starts = [x for x in (days[0] if days else None, q_periods_on_disk[0] if q_periods_on_disk else None) if x]
    span_ends = [x for x in (days[-1] if days else None, q_periods_on_disk[-1] if q_periods_on_disk else None) if x]
    q_start = min(span_starts) if span_starts else None
    q_end = max(span_ends) if span_ends else None
    for dataset in QUARTERLY_VIP_DATASETS:
        expected_q = report_quarters(max(q_start, dataset.start), q_end) if q_start and q_end else []
        have_q = {path.name[:8] for path in (out / dataset.name).glob("*.tsv.gz")}
        report[dataset.name] = {
            "files": len(have_q & set(expected_q)),
            "expected": len(expected_q),
            "missing": sorted(set(expected_q) - have_q)[:5],
        }
    if days:
        report["daily_rows_last_day"] = len(read_table(daily_dir / f"{days[-1]}.tsv.gz"))
    return report


def _wait_until(clock: str) -> None:
    """交易时段不大量调用，免得和线上每分钟的价格同步抢额度。"""
    now = datetime.now()
    hour, minute = map(int, clock.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target > now:
        print(f"等到 {clock} 再开始", flush=True)
        time.sleep((target - now).total_seconds())


def main() -> int:
    parser = argparse.ArgumentParser(description="研究数据底座")
    parser.add_argument("command", choices=["pull", "check"])
    parser.add_argument("--out", required=True, type=lambda value: Path(value).expanduser())
    parser.add_argument("--start", default=HISTORY_START)
    parser.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--only-quarterly", action="store_true", help="仅拉取季频 VIP 财报数据集")
    parser.add_argument("--not-before", help="今天几点之后才开始，如 15:05")
    args = parser.parse_args()
    if args.command == "check":
        print(json.dumps(check(args.out), ensure_ascii=False, indent=1))
        return 0
    token = os.environ.get("STOCK_TUSHARE_TOKEN", "").strip()
    if not token:
        print("缺少环境变量 STOCK_TUSHARE_TOKEN", file=sys.stderr)
        return 2
    if args.not_before:
        _wait_until(args.not_before)
    log = lambda message: print(message, flush=True)  # noqa: E731
    missing = Puller(tushare_caller(token), args.out, workers=args.workers, log=log).run(
        args.start, args.end, only_quarterly=args.only_quarterly
    )
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
