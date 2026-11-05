"""特征库和预测力检验（Q2）。标准见 docs/quant-order-plan.md 第 4.7 节。

特征都只用当天收盘及以前的数据。生产环境出计划时会用同一份计算，所以这里不放只能离线跑的东西。

    python -m backend.app.quant.research_features ic --out ~/hermes-stock-research

2019 年以后的数据留给 Q5 做最终验证：这个模块默认算到 2018-12-31，标签也不读之后的价格，
要往后算必须显式传 --allow-validation-data。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from backend.app.quant.factors.factor_analysis_core import compute_ic_series
from backend.app.quant.research_panel import Panel

DEV_START = "20120701"
DEV_END = "20181231"
LABEL_DAYS = 10
MIN_ABS_IC = 0.02
MIN_ABS_ICIR = 0.3
MIN_YEAR_SHARE = 5 / 7
MAX_FEATURE_CORR = 0.8
CHIP_GROUP = "筹码"  # 2018 年才有数据，开发区间只有一年，不判定（第 4.7 节）


@dataclass(frozen=True)
class Feature:
    name: str
    group: str
    compute: Callable[["_Ctx"], pd.DataFrame]


class _Ctx:
    """特征共用的中间量，算一次缓存一次。"""

    def __init__(self, panel: Panel) -> None:
        self.panel = panel
        self._memo: dict[str, pd.DataFrame] = {}

    def _get(self, key: str, make: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        if key not in self._memo:
            self._memo[key] = make()
        return self._memo[key]

    def raw(self, dataset: str, name: str) -> pd.DataFrame:
        return self._get(f"{dataset}.{name}", lambda: pd.DataFrame(np.asarray(self.panel.field(dataset, name), dtype=np.float64)))

    @property
    def close(self) -> pd.DataFrame:
        return self._get("close", lambda: pd.DataFrame(self.panel.adj_close_filled))

    @property
    def high(self) -> pd.DataFrame:
        return self._get("high", lambda: pd.DataFrame(self.panel.adjusted("high")))

    @property
    def low(self) -> pd.DataFrame:
        return self._get("low", lambda: pd.DataFrame(self.panel.adjusted("low")))

    @property
    def open(self) -> pd.DataFrame:
        return self._get("open", lambda: pd.DataFrame(self.panel.adjusted("open")))

    @property
    def ret(self) -> pd.DataFrame:
        """日收益，停牌日是 NaN。"""
        return self._get("ret", lambda: self.close.pct_change(fill_method=None).where(pd.DataFrame(self.panel.traded)))

    def ma(self, n: int) -> pd.DataFrame:
        return self._get(f"ma{n}", lambda: self.close.rolling(n, min_periods=n).mean())

    @property
    def atr(self) -> pd.DataFrame:
        def make() -> pd.DataFrame:
            previous = self.close.shift(1)
            high_low = (self.high - self.low).to_numpy()
            high_close = (self.high - previous).abs().to_numpy()
            low_close = (self.low - previous).abs().to_numpy()
            true_range = pd.DataFrame(np.fmax(np.fmax(high_low, high_close), low_close))
            return true_range.ewm(alpha=1 / 14, adjust=False, ignore_na=True).mean()

        return self._get("atr", make)

    @property
    def big_net(self) -> pd.DataFrame:
        """大单和超大单净买入额。"""

        def make() -> pd.DataFrame:
            return (
                self.raw("moneyflow", "buy_lg_amount")
                + self.raw("moneyflow", "buy_elg_amount")
                - self.raw("moneyflow", "sell_lg_amount")
                - self.raw("moneyflow", "sell_elg_amount")
            )

        return self._get("big_net", make)


def _rolling_corr(x: pd.DataFrame, y: pd.DataFrame, n: int) -> pd.DataFrame:
    both = x.notna() & y.notna()
    x, y = x.where(both), y.where(both)
    min_periods = max(10, n // 2)
    mean_xy = (x * y).rolling(n, min_periods=min_periods).mean()
    mean_x, mean_y = x.rolling(n, min_periods=min_periods).mean(), y.rolling(n, min_periods=min_periods).mean()
    std_x, std_y = x.rolling(n, min_periods=min_periods).std(ddof=0), y.rolling(n, min_periods=min_periods).std(ddof=0)
    denominator = (std_x * std_y).where(lambda frame: frame > 1e-12)
    return (mean_xy - mean_x * mean_y) / denominator


def _macd_hist(c: _Ctx) -> pd.DataFrame:
    dif = c.close.ewm(span=12, adjust=False, ignore_na=True).mean() - c.close.ewm(span=26, adjust=False, ignore_na=True).mean()
    dea = dif.ewm(span=9, adjust=False, ignore_na=True).mean()
    return 2 * (dif - dea) / c.close


def _adx(c: _Ctx) -> pd.DataFrame:
    up = c.high.diff()
    down = -c.low.diff()
    plus = up.where((up > down) & (up > 0), 0.0)
    minus = down.where((down > up) & (down > 0), 0.0)
    plus_di = plus.ewm(alpha=1 / 14, adjust=False, ignore_na=True).mean() / c.atr
    minus_di = minus.ewm(alpha=1 / 14, adjust=False, ignore_na=True).mean() / c.atr
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).where(lambda frame: frame > 1e-12)
    return dx.ewm(alpha=1 / 14, adjust=False, ignore_na=True).mean()


def _rsi(c: _Ctx) -> pd.DataFrame:
    delta = c.close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, ignore_na=True).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, ignore_na=True).mean()
    return 100 - 100 / (1 + gain / loss.where(lambda frame: frame > 1e-12))


def _kdj_k(c: _Ctx) -> pd.DataFrame:
    low = c.low.rolling(9, min_periods=5).min()
    high = c.high.rolling(9, min_periods=5).max()
    rsv = (c.close - low) / (high - low).where(lambda frame: frame > 1e-9)
    return rsv.ewm(alpha=1 / 3, adjust=False, ignore_na=True).mean()


def _boll(c: _Ctx) -> pd.DataFrame:
    mid = c.ma(20)
    std = c.close.rolling(20, min_periods=20).std(ddof=0)
    return (c.close - mid) / (2 * std).where(lambda frame: frame > 1e-9)


def _ma_align(c: _Ctx) -> pd.DataFrame:
    score = (c.ma(5) > c.ma(10)).astype(float) + (c.ma(10) > c.ma(20)).astype(float) + (c.ma(20) > c.ma(60)).astype(float)
    return score.where(c.ma(60).notna())


def _amihud(c: _Ctx) -> pd.DataFrame:
    amount = c.raw("daily", "amount").where(lambda frame: frame > 0)
    return np.log1p((c.ret.abs() / amount * 1e6).rolling(20, min_periods=10).mean())


def _shadow(c: _Ctx, upper: bool) -> pd.DataFrame:
    raw_open = c.open
    close = c.close.where(pd.DataFrame(c.panel.traded))
    body_top = raw_open.where(raw_open > close, close)
    body_bottom = raw_open.where(raw_open < close, close)
    span = (c.high - c.low).where(lambda frame: frame > 1e-9)
    return ((c.high - body_top) if upper else (body_bottom - c.low)) / span


def _distance(c: _Ctx, n: int, high: bool) -> pd.DataFrame:
    if high:
        return c.close / c.high.rolling(n, min_periods=n // 2).max() - 1
    return c.close / c.low.rolling(n, min_periods=n // 2).min() - 1


def _limit_hits(c: _Ctx) -> pd.DataFrame:
    limit = c.raw("stk_limit", "up_limit")
    close = c.raw("daily", "close")
    hit = ((close - limit).abs() < 0.005).astype(float).where(limit.notna())
    return hit.rolling(20, min_periods=10).sum()


FEATURES: tuple[Feature, ...] = (
    # 趋势
    Feature("ma_gap_5", "趋势", lambda c: c.close / c.ma(5) - 1),
    Feature("ma_gap_10", "趋势", lambda c: c.close / c.ma(10) - 1),
    Feature("ma_gap_20", "趋势", lambda c: c.close / c.ma(20) - 1),
    Feature("ma_gap_60", "趋势", lambda c: c.close / c.ma(60) - 1),
    Feature("ma20_slope", "趋势", lambda c: c.ma(20) / c.ma(20).shift(5) - 1),
    Feature("ma_align", "趋势", _ma_align),
    Feature("macd_hist", "趋势", _macd_hist),
    Feature("adx_14", "趋势", _adx),
    # 动量和反转
    Feature("ret_1", "动量", lambda c: c.close / c.close.shift(1) - 1),
    Feature("ret_5", "动量", lambda c: c.close / c.close.shift(5) - 1),
    Feature("ret_20", "动量", lambda c: c.close / c.close.shift(20) - 1),
    Feature("ret_60", "动量", lambda c: c.close / c.close.shift(60) - 1),
    Feature("ret_120", "动量", lambda c: c.close / c.close.shift(120) - 1),
    Feature("ret_60_skip5", "动量", lambda c: c.close.shift(5) / c.close.shift(60) - 1),
    # 波动
    Feature("atr_pct", "波动", lambda c: c.atr / c.close),
    Feature("rv_20", "波动", lambda c: c.ret.rolling(20, min_periods=10).std()),
    Feature("rv_60", "波动", lambda c: c.ret.rolling(60, min_periods=40).std()),
    Feature("range_1", "波动", lambda c: (c.high - c.low) / c.close),
    Feature("range_20", "波动", lambda c: ((c.high - c.low) / c.close).rolling(20, min_periods=10).mean()),
    Feature("gap_1", "波动", lambda c: c.open / c.close.shift(1) - 1),
    Feature("downside_20", "波动", lambda c: c.ret.clip(upper=0).rolling(20, min_periods=10).std()),
    # 量能
    Feature("vol_ratio_5", "量能", lambda c: c.raw("daily", "vol") / c.raw("daily", "vol").rolling(5, min_periods=3).mean()),
    Feature("vol_trend", "量能", lambda c: c.raw("daily", "vol").rolling(5, min_periods=3).mean() / c.raw("daily", "vol").rolling(20, min_periods=10).mean()),
    Feature("turnover_f", "量能", lambda c: c.raw("daily_basic", "turnover_rate_f")),
    Feature("turnover_20", "量能", lambda c: c.raw("daily_basic", "turnover_rate_f").rolling(20, min_periods=10).mean()),
    Feature("amount_20", "量能", lambda c: np.log1p(c.raw("daily", "amount").rolling(20, min_periods=10).mean())),
    Feature("amihud_20", "量能", _amihud),
    Feature("vp_corr_20", "量能", lambda c: _rolling_corr(c.ret, c.raw("daily", "vol").pct_change(fill_method=None), 20)),
    # 位置
    Feature("boll_pos", "位置", _boll),
    Feature("rsi_14", "位置", _rsi),
    Feature("kdj_k", "位置", _kdj_k),
    Feature("close_pos", "位置", lambda c: (c.close - c.low) / (c.high - c.low).where(lambda frame: frame > 1e-9)),
    Feature("upper_shadow", "位置", lambda c: _shadow(c, True)),
    Feature("lower_shadow", "位置", lambda c: _shadow(c, False)),
    Feature("dist_high_20", "位置", lambda c: _distance(c, 20, True)),
    Feature("dist_high_60", "位置", lambda c: _distance(c, 60, True)),
    Feature("dist_high_120", "位置", lambda c: _distance(c, 120, True)),
    Feature("dist_low_20", "位置", lambda c: _distance(c, 20, False)),
    Feature("dist_low_60", "位置", lambda c: _distance(c, 60, False)),
    Feature("dist_low_120", "位置", lambda c: _distance(c, 120, False)),
    Feature("limit_hits_20", "位置", _limit_hits),
    # 资金
    Feature("big_net_1", "资金", lambda c: c.big_net / c.raw("daily", "amount").where(lambda frame: frame > 0)),
    Feature("big_net_5", "资金", lambda c: c.big_net.rolling(5, min_periods=3).sum() / c.raw("daily", "amount").rolling(5, min_periods=3).sum().where(lambda frame: frame > 0)),
    Feature("big_net_20", "资金", lambda c: c.big_net.rolling(20, min_periods=10).sum() / c.raw("daily", "amount").rolling(20, min_periods=10).sum().where(lambda frame: frame > 0)),
    Feature("mf_net_5", "资金", lambda c: c.raw("moneyflow", "net_mf_amount").rolling(5, min_periods=3).sum() / c.raw("daily", "amount").rolling(5, min_periods=3).sum().where(lambda frame: frame > 0)),
    # 规模和估值
    Feature("log_mv", "规模估值", lambda c: np.log(c.raw("daily_basic", "total_mv").where(lambda frame: frame > 0))),
    Feature("log_circ_mv", "规模估值", lambda c: np.log(c.raw("daily_basic", "circ_mv").where(lambda frame: frame > 0))),
    Feature("bp", "规模估值", lambda c: 1 / c.raw("daily_basic", "pb").where(lambda frame: frame > 0)),
    Feature("ep", "规模估值", lambda c: 1 / c.raw("daily_basic", "pe_ttm").where(lambda frame: frame > 0)),
    # 筹码（2018 年才有）
    Feature("winner_rate", CHIP_GROUP, lambda c: c.raw("cyq_perf", "winner_rate")),
    Feature("cost_gap", CHIP_GROUP, lambda c: c.raw("daily", "close") / c.raw("cyq_perf", "cost_50pct").where(lambda frame: frame > 0) - 1),
    Feature("cost_spread", CHIP_GROUP, lambda c: (c.raw("cyq_perf", "cost_95pct") - c.raw("cyq_perf", "cost_5pct")) / c.raw("cyq_perf", "cost_50pct").where(lambda frame: frame > 0)),
)


def check_range(panel: Panel, end: str, *, allow_validation_data: bool) -> None:
    if end > DEV_END and not allow_validation_data:
        raise ValueError(f"{end} 超出开发区间：{DEV_END} 之后的数据留给 Q5 验证")


def forward_return(panel: Panel, days: int, *, last_exit_date: str) -> np.ndarray:
    """当天收盘算特征后：次日开盘买入，持有到第 days 个交易日收盘的收益。

    卖出日晚于 last_exit_date 的是 NaN，所以开发区间的标签读不到验证区间的价格。
    次日没成交或一字涨停（买不进）的也是 NaN。
    """
    close = panel.adj_close_filled
    open_price = panel.adjusted("open")
    rows, cols = close.shape
    result = np.full((rows, cols), np.nan)
    date_array = np.array(panel.dates)
    for t in range(rows - days):
        if date_array[t + days] > last_exit_date:
            break
        result[t] = close[t + days] / open_price[t + 1] - 1
    high_next = np.asarray(panel.field("daily", "close"))
    pre_next = np.asarray(panel.field("daily", "pre_close"))
    with np.errstate(invalid="ignore"):
        locked_up = panel.one_price & (high_next > pre_next)
    result[:-1][locked_up[1:]] = np.nan
    return result


def feature_ic(panel: Panel, values: np.ndarray, label: np.ndarray, rows: np.ndarray) -> pd.Series:
    """values、label 都是 日期 × 股票。只在研究股票池里算，复用因子模块的 IC 算法。"""
    universe = panel.universe[rows]
    index = np.array(panel.dates)[rows]
    factor_df = pd.DataFrame(np.where(universe, values[rows], np.nan), index=index)
    label_df = pd.DataFrame(label[rows], index=index)
    return compute_ic_series(factor_df, label_df)


def summarize_ic(ic: pd.Series, *, step: int = LABEL_DAYS) -> dict[str, float | int | str]:
    """ICIR 每隔 step 个交易日取一次，避免收益窗口重叠把它算高。"""
    mean = float(ic.mean())
    sampled = ic.iloc[::step]
    std = float(sampled.std())
    icir = float(sampled.mean() / std) if std > 0 else 0.0
    yearly = ic.groupby(ic.index.str[:4]).mean()
    agree = int(((yearly > 0) if mean > 0 else (yearly < 0)).sum())
    return {"ic": mean, "icir": icir, "years_agree": agree, "years": int(len(yearly))}


def passes(stats: dict) -> bool:
    return (
        abs(stats["ic"]) >= MIN_ABS_IC
        and abs(stats["icir"]) >= MIN_ABS_ICIR
        and stats["years"] > 0
        and stats["years_agree"] / stats["years"] >= MIN_YEAR_SHARE - 1e-9
    )


def drop_correlated(ranked: list[str], corr: Callable[[str, str], float]) -> tuple[list[str], dict[str, str]]:
    """按 ICIR 从高到低保留，和已保留的相关超过上限的去掉。"""
    kept: list[str] = []
    dropped: dict[str, str] = {}
    for name in ranked:
        twin = next((other for other in kept if abs(corr(name, other)) > MAX_FEATURE_CORR), None)
        if twin is None:
            kept.append(name)
        else:
            dropped[name] = twin
    return kept, dropped


def cross_section_corr(a: np.ndarray, b: np.ndarray, universe: np.ndarray, rows: np.ndarray) -> float:
    """两个特征的横截面秩相关，抽样日期取平均。"""
    values = []
    for t in rows:
        mask = universe[t] & ~np.isnan(a[t]) & ~np.isnan(b[t])
        if mask.sum() < 30:
            continue
        ra, rb = pd.Series(a[t][mask]).rank(), pd.Series(b[t][mask]).rank()
        values.append(float(np.corrcoef(ra, rb)[0, 1]))
    return float(np.mean(values)) if values else 0.0


def compute_all(panel: Panel, out: Path | None = None) -> dict[str, np.ndarray]:
    """算全部特征。给了 out 就逐个存盘再用内存映射读回，内存里不同时放全部。"""
    ctx = _Ctx(panel)
    results: dict[str, np.ndarray] = {}
    traded = panel.traded
    folder = out / "_cache" / "features" if out else None
    if folder:
        folder.mkdir(parents=True, exist_ok=True)
    for feature in FEATURES:
        path = folder / f"{feature.name}.npy" if folder else None
        if path is not None and path.exists():
            results[feature.name] = np.load(path, mmap_mode="r")
            continue
        frame = feature.compute(ctx)
        values = np.where(traded, frame.to_numpy(dtype=np.float64), np.nan)
        values = np.where(np.isfinite(values), values, np.nan)
        if path is not None:
            np.save(path, values.astype(np.float32))
            results[feature.name] = np.load(path, mmap_mode="r")
        else:
            results[feature.name] = values
    return results


def run_ic(out: Path, *, start: str = DEV_START, end: str = DEV_END, allow_validation_data: bool = False) -> dict:
    check_range(None, end, allow_validation_data=allow_validation_data)  # type: ignore[arg-type]
    panel = Panel.open(out)
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= start) & (dates <= end))
    features = compute_all(panel, out)
    labels = {days: forward_return(panel, days, last_exit_date=end) for days in (5, 10, 20)}
    stats: dict[str, dict] = {}
    for feature in FEATURES:
        summary = summarize_ic(feature_ic(panel, np.asarray(features[feature.name]), labels[LABEL_DAYS], rows))
        summary["group"] = feature.group
        for days in (5, 20):
            summary[f"ic_{days}"] = float(feature_ic(panel, np.asarray(features[feature.name]), labels[days], rows).mean())
        stats[feature.name] = summary
        print(f"{feature.name:16s} {feature.group:6s} IC {summary['ic']:+.4f} ICIR {summary['icir']:+.2f} {summary['years_agree']}/{summary['years']} 年 5日 {summary['ic_5']:+.4f} 20日 {summary['ic_20']:+.4f}", flush=True)
    eligible = [name for name, item in stats.items() if item["group"] != CHIP_GROUP and passes(item)]
    ranked = sorted(eligible, key=lambda name: abs(stats[name]["icir"]), reverse=True)
    sample_rows = rows[::20]
    kept, dropped = drop_correlated(
        ranked,
        lambda a, b: cross_section_corr(np.asarray(features[a]), np.asarray(features[b]), panel.universe, sample_rows),
    )
    report = {"start": start, "end": end, "label_days": LABEL_DAYS, "stats": stats, "passed": ranked, "selected": kept, "dropped_as_duplicate": dropped}
    folder = out / "_reports"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"feature_ic_{end}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\n通过 {len(ranked)} 个，去重后 {len(kept)} 个：{kept}\n去掉：{dropped}", flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="特征库和预测力检验")
    parser.add_argument("command", choices=["ic"])
    parser.add_argument("--out", required=True, type=lambda value: Path(value).expanduser())
    parser.add_argument("--end", default=DEV_END)
    parser.add_argument("--allow-validation-data", action="store_true")
    args = parser.parse_args()
    run_ic(args.out, end=args.end, allow_validation_data=args.allow_validation_data)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    raise SystemExit(main())
