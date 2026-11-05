"""研究面板：把 research_data 下载的按日文件拼成“日期 × 股票”的矩阵，只读，按需加载。

全市场 15 年的数据很大，这台机器内存紧，所以矩阵存成 .npy，用内存映射读，用到哪列才占内存。

复权用 close/pre_close 串起来（后复权，第一根 K 线不变），和 tech_signal 同一算法。
后复权的好处是过去的价格不会因为未来的除权而改变，没有未来数据。
"""
from __future__ import annotations

import gzip
import json
import os
import re
from pathlib import Path

import numpy as np

A_SHARE = re.compile(r"(00\d{4}|30\d{4})\.SZ|(60\d{4}|68\d{4})\.SH")
FIELDS: dict[str, tuple[str, ...]] = {
    "daily": ("open", "high", "low", "close", "pre_close", "vol", "amount"),
    "daily_basic": ("turnover_rate_f", "volume_ratio", "pe_ttm", "pb", "total_mv", "circ_mv"),
    "moneyflow": ("buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount", "net_mf_amount"),
    "stk_limit": ("up_limit", "down_limit"),
    "cyq_perf": ("cost_5pct", "cost_50pct", "cost_95pct", "weight_avg", "winner_rate"),
}
MIN_LISTED_BARS = 120
_STEP_RANGE = (0.1, 10.0)


def _read_day(path: Path) -> tuple[list[str], list[list[str]]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        header, *rows = fh.read().split("\n")
    return header.split("\t"), [row.split("\t") for row in rows if row]


def _save_atomic(path: Path, array: np.ndarray) -> None:
    """先写临时文件再改名，进程被杀不会留下半个文件。"""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        np.save(fh, array)
    os.replace(tmp, path)


def build_cache(out: Path) -> Path:
    """把按日文件拼成矩阵。只重建文件数变了的数据集，所以可以边下载边用，下完再跑一次就补齐。"""
    cache = out / "_cache"
    cache.mkdir(parents=True, exist_ok=True)
    day_files = sorted((out / "daily").glob("*.tsv.gz"))
    dates = [path.name[:8] for path in day_files]
    manifest = cache / "manifest.json"
    saved = json.loads(manifest.read_text()) if manifest.exists() else {}
    counts = {dataset: len(list((out / dataset).glob("*.tsv.gz"))) for dataset in FIELDS}
    same_days = saved.get("dates") == dates
    if same_days and saved.get("counts") == counts:
        return cache

    if same_days:
        codes = saved["codes"]
    else:
        codes_set: set[str] = set()
        for path in day_files:
            _, rows = _read_day(path)
            codes_set.update(row[0] for row in rows if A_SHARE.fullmatch(row[0]))
        codes = sorted(codes_set)
    column = {code: j for j, code in enumerate(codes)}
    row_of = {day: i for i, day in enumerate(dates)}
    for dataset, names in FIELDS.items():
        if same_days and saved.get("counts", {}).get(dataset) == counts[dataset]:
            continue
        arrays = {name: np.full((len(dates), len(codes)), np.nan, dtype=np.float32) for name in names}
        for path in sorted((out / dataset).glob("*.tsv.gz")):
            i = row_of.get(path.name[:8])
            if i is None:
                continue
            header, rows = _read_day(path)
            position = {name: header.index(name) for name in names if name in header}
            for row in rows:
                j = column.get(row[0])
                if j is None:
                    continue
                for name, k in position.items():
                    if k < len(row) and row[k]:
                        arrays[name][i, j] = float(row[k])
        for name, array in arrays.items():
            _save_atomic(cache / f"{dataset}.{name}.npy", array)
        del arrays
    tmp = manifest.with_name(manifest.name + ".tmp")
    tmp.write_text(json.dumps({"dates": dates, "codes": codes, "counts": counts}))
    os.replace(tmp, manifest)
    return cache


def st_matrix(out: Path, dates: list[str], codes: list[str]) -> np.ndarray:
    """某天某只股票是不是 ST：按当时的名字判断，没有未来数据。"""
    column = {code: j for j, code in enumerate(codes)}
    array = np.zeros((len(dates), len(codes)), dtype=bool)
    date_array = np.array(dates)
    path = out / "namechange.tsv.gz"
    if not path.exists():
        return array
    header, rows = _read_day(path)
    code_at, name_at, start_at, end_at = (header.index(key) for key in ("ts_code", "name", "start_date", "end_date"))
    for row in rows:
        row = row + [""] * (len(header) - len(row))
        j = column.get(row[code_at])
        if j is None or "ST" not in row[name_at].upper():
            continue
        start_i = int(np.searchsorted(date_array, row[start_at] or dates[0]))
        end_i = int(np.searchsorted(date_array, row[end_at], side="right")) if row[end_at] else len(dates)
        array[start_i:end_i, j] = True
    return array


class Panel:
    """日期 × 股票的只读矩阵。测试可以直接传数组，不用文件。"""

    def __init__(self, dates: list[str], codes: list[str], arrays: dict[tuple[str, str], np.ndarray], st: np.ndarray | None = None, *, min_listed: int = MIN_LISTED_BARS) -> None:
        self.min_listed = min_listed
        self.dates = list(dates)
        self.codes = list(codes)
        self._arrays = arrays
        self._st = st if st is not None else np.zeros((len(dates), len(codes)), dtype=bool)
        self._cache_dir: Path | None = None
        self._derived: dict[str, np.ndarray] = {}

    @classmethod
    def open(cls, out: Path, *, end: str | None = None) -> "Panel":
        """end 之后的行直接不读：开发阶段从物理上碰不到验证数据，内存也省一半。"""
        cache = build_cache(out)
        manifest = json.loads((cache / "manifest.json").read_text())
        dates = [day for day in manifest["dates"] if end is None or day <= end]
        panel = cls(dates, manifest["codes"], {})
        panel._cache_dir = cache
        panel._st = st_matrix(out, dates, panel.codes)
        return panel

    def field(self, dataset: str, name: str) -> np.ndarray:
        key = (dataset, name)
        if key not in self._arrays:
            if self._cache_dir is None:
                return np.full((len(self.dates), len(self.codes)), np.nan, dtype=np.float32)
            self._arrays[key] = np.load(self._cache_dir / f"{dataset}.{name}.npy", mmap_mode="r")[: len(self.dates)]
        return self._arrays[key]

    @property
    def st(self) -> np.ndarray:
        return self._st

    @property
    def traded(self) -> np.ndarray:
        if "traded" not in self._derived:
            close = self.field("daily", "close")
            self._derived["traded"] = ~np.isnan(close) & (close > 0)
        return self._derived["traded"]

    @property
    def factor(self) -> np.ndarray:
        """后复权因子：今天的价格乘它，就和第一根 K 线可比。"""
        if "factor" not in self._derived:
            close = np.asarray(self.field("daily", "close"), dtype=np.float64)
            pre_close = np.asarray(self.field("daily", "pre_close"), dtype=np.float64)
            last = np.full(close.shape[1], np.nan)
            factor = np.ones_like(close)
            running = np.ones(close.shape[1])
            for i in range(close.shape[0]):
                with np.errstate(divide="ignore", invalid="ignore"):
                    step = last / pre_close[i]
                usable = (step >= _STEP_RANGE[0]) & (step <= _STEP_RANGE[1])
                running = np.where(usable, running * step, running)
                factor[i] = running
                last = np.where(np.isnan(close[i]), last, close[i])
            self._derived["factor"] = factor
        return self._derived["factor"]

    def adjusted(self, name: str) -> np.ndarray:
        """复权后的开高低收。没有成交的日子是 NaN。"""
        key = f"adj_{name}"
        if key not in self._derived:
            self._derived[key] = np.asarray(self.field("daily", name), dtype=np.float64) * self.factor
        return self._derived[key]

    @property
    def adj_close_filled(self) -> np.ndarray:
        """停牌日沿用上一个收盘价。"""
        if "adj_close_filled" not in self._derived:
            filled = self.adjusted("close").copy()
            for i in range(1, filled.shape[0]):
                missing = np.isnan(filled[i])
                filled[i, missing] = filled[i - 1, missing]
            self._derived["adj_close_filled"] = filled
        return self._derived["adj_close_filled"]

    @property
    def listed_bars(self) -> np.ndarray:
        if "listed" not in self._derived:
            self._derived["listed"] = np.cumsum(self.traded, axis=0)
        return self._derived["listed"]

    @property
    def one_price(self) -> np.ndarray:
        """全天一个价，一字板。"""
        if "one_price" not in self._derived:
            high, low = self.field("daily", "high"), self.field("daily", "low")
            self._derived["one_price"] = np.abs(np.asarray(high) - np.asarray(low)) < 1e-6
        return self._derived["one_price"]

    @property
    def universe(self) -> np.ndarray:
        """当天可以进入研究的股票：有成交、上市满 120 根、不是 ST。"""
        if "universe" not in self._derived:
            amount = np.asarray(self.field("daily", "amount"))
            with np.errstate(invalid="ignore"):
                self._derived["universe"] = self.traded & (self.listed_bars >= self.min_listed) & ~self.st & (amount > 0)
        return self._derived["universe"]
