"""选股回测（Q6）：按排名买前 N 只、定期换仓，扣费后能不能跑赢“全部合格股票等权”。标准见 docs/quant-order-plan.md 第 4.10 节。

    python -m backend.app.quant.research_selection dev --out ~/hermes-stock-research
    python -m backend.app.quant.research_selection final --out ~/hermes-stock-research
    python -m backend.app.quant.research_selection picks --out ~/hermes-stock-research

三种排名放在一起比：
1. model：模型 1（7 个通过检验的特征，逐年只用之前的数据训练）。
2. composite：同样 7 个特征按方向等权，不用模型。模型赢不了它就用它。
3. current：线上自动选股现在的五项打分（估值、动量、流动性、低波动），只做对照。

dev 只读 2018 年及以前；final 读 2019 年以后，只能跑一次；picks 要 final 通过才出名单。
模拟按能不能成交来算：次日开盘买卖，停牌和一字板不能成交，卖不掉的继续占名额。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from backend.app.quant import research_models as rm
from backend.app.quant.order_engine import BUY_COST, SELL_COST
from backend.app.quant.research_features import DEV_END, DEV_START, compute_all, forward_return
from backend.app.quant.research_panel import Panel

# 以下常量在看结果之前写定（2026-11-11），之后不改；要改另开版本。
TOP_NS = (10, 30)
HOLD_DAYS = rm.HORIZON  # 换仓间隔和模型预测的 10 日一致
HOLD_BAND = 2.0  # 已持有的股票排名还在前 2N 就不换，省换手
MIN_AMOUNT = 20_000.0  # 20 日平均成交额下限，千元（2000 万元）。模型偏爱成交额小的，没有下限会选到买不进的
CURRENT_MIN_AMOUNT = 50_000.0  # 线上规则自己的下限
FIRST_DEV_YEAR = rm.FIRST_TEST_YEAR
FIRST_FINAL_YEAR = 2019
YEAR_DAYS = 244
STRATEGIES = ("model", "composite", "current")
CANDIDATES = ("model", "composite")
FEATURE_SIGNS = {name: -1.0 for name in rm.RANK_MODEL_FEATURES}  # 第 4.7 节：7 个都是反向，7/7 年一致
CURRENT_WEIGHTS = {"value_pe": 0.20, "value_pb": 0.15, "momentum_20": 0.25, "liquidity_20": 0.15, "low_volatility_20": 0.25}

DEV_MIN_EXCESS = 0.05
FINAL_MIN_EXCESS = 0.03
MIN_INFORMATION_RATIO = 0.5
DEV_MIN_YEAR_SHARE = 3 / 4
FINAL_MIN_YEAR_SHARE = 0.6
MAX_EXTRA_DRAWDOWN = 0.05


@dataclass(frozen=True)
class Costs:
    buy: float = BUY_COST
    sell: float = SELL_COST

    def scaled(self, times: float) -> "Costs":
        return Costs(self.buy * times, self.sell * times)


BASE_COSTS = Costs()
STRESS_COSTS = BASE_COSTS.scaled(2.0)


def pool(panel: Panel, min_amount: float = MIN_AMOUNT) -> np.ndarray:
    """当天可以选的股票：研究股票池里，20 日平均成交额够得上下限。"""
    amount = pd.DataFrame(np.asarray(panel.field("daily", "amount"), dtype=np.float64))
    average = amount.rolling(20, min_periods=10).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        return panel.universe & (average >= min_amount)


def model_scores(
    panel: Panel,
    features: dict[str, np.ndarray],
    label: np.ndarray,
    *,
    first_year: int,
    end: str,
    names: tuple[str, ...] = rm.RANK_MODEL_FEATURES,
    fit: Callable[[np.ndarray, np.ndarray, str], Any] | None = None,
) -> np.ndarray:
    """模型 1 的滚动预测：每一年只用之前的数据训练。没预测到的日子是 NaN。"""
    fit = fit or rm._fit
    universe = panel.universe
    scores = np.full((len(panel.dates), len(panel.codes)), np.nan, dtype=np.float32)
    for split in rm.walk_forward(panel.dates, first_year=first_year, end=end):
        train_rows = split.train[:: rm.TRAIN_STEP]
        train_x, train_y = rm.assemble(features, list(names), train_rows), rm.target_rank(label, train_rows)
        mask = universe[train_rows] & ~np.isnan(train_y)
        model = fit(train_x[mask], train_y[mask], "regression")
        test_x = rm.assemble(features, list(names), split.test)
        test_mask = universe[split.test]
        predicted = np.full(test_mask.shape, np.nan, dtype=np.float32)
        predicted[test_mask] = model.predict(test_x[test_mask])
        scores[split.test] = predicted
        print(f"model {split.year} 预测完成，训练样本 {int(mask.sum())}", flush=True)
    return scores


def composite_scores(features: dict[str, np.ndarray], rows: np.ndarray, shape: tuple[int, int], signs: dict[str, float] = FEATURE_SIGNS) -> np.ndarray:
    """7 个特征的当日排名按方向等权。缺任何一个特征的股票不打分。"""
    scores = np.full(shape, np.nan, dtype=np.float32)
    total = np.zeros((len(rows), shape[1]), dtype=np.float64)
    for name, sign in signs.items():
        ranked = rm.rank_features(features[name], rows)
        total += ranked if sign > 0 else 1.0 - ranked
    scores[rows] = total / len(signs)
    return scores


def current_rule_scores(panel: Panel, rows: np.ndarray) -> np.ndarray:
    """线上自动选股的五项打分，口径照 automatic_selection_scoring。

    研究数据里没有行业，所以没有“每个行业最多 4 只”和市值分桶上限，这是和线上唯一的差别。
    """
    close = pd.DataFrame(panel.adj_close_filled)
    daily = close.pct_change(fill_method=None).where(pd.DataFrame(panel.traded))
    amount = pd.DataFrame(np.asarray(panel.field("daily", "amount"), dtype=np.float64)).rolling(20, min_periods=20).mean()
    pe = pd.DataFrame(np.asarray(panel.field("daily_basic", "pe_ttm"), dtype=np.float64))
    pb = pd.DataFrame(np.asarray(panel.field("daily_basic", "pb"), dtype=np.float64))
    parts = {
        "value_pe": -np.log(pe.where(pe > 0)),
        "value_pb": -np.log(pb.where(pb > 0)),
        "momentum_20": close / close.shift(20) - 1,
        "liquidity_20": np.log(amount.where(amount > 0)),
        "low_volatility_20": -daily.rolling(20, min_periods=20).std(),
    }
    eligible = pd.DataFrame(panel.universe).iloc[rows] & (amount.iloc[rows] >= CURRENT_MIN_AMOUNT)
    for frame in parts.values():
        eligible &= frame.iloc[rows].notna()
    total = pd.DataFrame(0.0, index=eligible.index, columns=eligible.columns)
    for name, weight in CURRENT_WEIGHTS.items():
        values = parts[name].iloc[rows].where(eligible)
        clipped = values.clip(values.quantile(0.01, axis=1), values.quantile(0.99, axis=1), axis=0)
        spread = clipped.std(axis=1, ddof=0)
        total += weight * clipped.sub(clipped.mean(axis=1), axis=0).div(spread.where(spread > 1e-12), axis=0).fillna(0.0)
    scores = np.full(close.shape, np.nan, dtype=np.float32)
    scores[rows] = total.where(eligible).to_numpy(dtype=np.float32)
    return scores


def _rebalance(panel: Panel, day: int, score: np.ndarray, shares: dict[int, float], cash: float, top_n: int, band: float, costs: Costs) -> tuple[float, float]:
    """day 这天开盘按前一天收盘的排名换仓。返回剩下的现金和卖出金额。"""
    open_price = panel.adjusted("open")[day]
    traded, locked = panel.traded[day], panel.one_price[day]
    raw_close, pre_close = panel.field("daily", "close")[day], panel.field("daily", "pre_close")[day]
    order = np.argsort(-np.where(np.isnan(score), -np.inf, score), kind="stable")
    order = order[: int((~np.isnan(score)).sum())]
    rank_of = {int(stock): position for position, stock in enumerate(order.tolist())}
    sold = 0.0
    for stock in list(shares):
        if rank_of.get(stock, math.inf) < band * top_n:
            continue
        locked_down = bool(locked[stock]) and float(raw_close[stock]) < float(pre_close[stock])
        if not traded[stock] or locked_down or not np.isfinite(open_price[stock]):
            continue  # 停牌或一字跌停卖不掉，继续占名额
        proceeds = shares.pop(stock) * float(open_price[stock])
        sold += proceeds
        cash += proceeds * (1 - costs.sell)
    slots = top_n - len(shares)
    if slots <= 0 or cash <= 0:
        return cash, sold
    budget = cash / slots
    for stock in order.tolist():
        if slots <= 0:
            break
        if stock in shares or not traded[stock] or locked[stock] or not np.isfinite(open_price[stock]):
            continue  # 停牌或一字板买不进，顺延到下一名
        shares[stock] = budget / (float(open_price[stock]) * (1 + costs.buy))
        cash -= budget
        slots -= 1
    return cash, sold


def simulate(
    panel: Panel,
    scores: np.ndarray,
    *,
    top_n: int,
    start: str,
    end: str,
    offset: int = 0,
    hold_days: int = HOLD_DAYS,
    band: float = HOLD_BAND,
    costs: Costs = BASE_COSTS,
) -> dict[str, Any]:
    """收盘后排名、次日开盘成交，每 hold_days 个交易日换一次。净值从 1 开始，按复权收盘价每天估值。"""
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= start) & (dates <= end))
    close = panel.adj_close_filled
    decide = set(rows[offset::hold_days].tolist())
    shares: dict[int, float] = {}
    cash, sold_total = 1.0, 0.0
    pending: np.ndarray | None = None
    nav = np.empty(len(rows))
    for k, day in enumerate(rows.tolist()):
        if pending is not None:
            cash, sold = _rebalance(panel, day, pending, shares, cash, top_n, band, costs)
            sold_total += sold
            pending = None
        nav[k] = cash + sum(amount * float(close[day, stock]) for stock, amount in shares.items())
        if day in decide and k + 1 < len(rows):
            pending = np.asarray(scores[day], dtype=np.float64)
    return {"dates": dates[rows], "nav": nav, "sold": sold_total, "holdings": len(shares)}


def benchmark_nav(panel: Panel, members: np.ndarray, start: str, end: str) -> np.ndarray:
    """全部可选股票等权、每天再平衡、不扣费。成员用前一天的名单，没有未来数据。"""
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= start) & (dates <= end))
    close = panel.adj_close_filled
    nav = np.ones(len(rows))
    for k in range(1, len(rows)):
        day, before = rows[k], rows[k] - 1
        with np.errstate(invalid="ignore", divide="ignore"):
            change = close[day] / close[before] - 1
        usable = members[before] & np.isfinite(change)
        nav[k] = nav[k - 1] * (1 + (float(change[usable].mean()) if usable.any() else 0.0))
    return nav


def _drawdown(nav: np.ndarray) -> float:
    return float((nav / np.maximum.accumulate(nav) - 1).min())


def _annual(nav: np.ndarray) -> float:
    return float(nav[-1] ** (YEAR_DAYS / max(len(nav) - 1, 1)) - 1)


def by_year(dates: np.ndarray, nav: np.ndarray) -> dict[str, float]:
    years = np.array([day[:4] for day in dates])
    result, before = {}, 1.0
    for year in sorted(set(years.tolist())):
        last = float(nav[np.flatnonzero(years == year)[-1]])
        result[year] = last / before - 1
        before = last
    return result


def metrics(dates: np.ndarray, nav: np.ndarray, bench: np.ndarray, sold: float = 0.0) -> dict[str, Any]:
    daily = np.diff(nav) / nav[:-1]
    bench_daily = np.diff(bench) / bench[:-1]
    active = daily - bench_daily
    spread = float(daily.std())
    active_spread = float(active.std())
    mine, base = by_year(dates, nav), by_year(dates, bench)
    return {
        "annual": _annual(nav),
        "bench_annual": _annual(bench),
        "excess": _annual(nav) - _annual(bench),
        "sharpe": float(daily.mean() / spread * math.sqrt(YEAR_DAYS)) if spread > 0 else 0.0,
        "information_ratio": float(active.mean() / active_spread * math.sqrt(YEAR_DAYS)) if active_spread > 0 else 0.0,
        "drawdown": _drawdown(nav),
        "bench_drawdown": _drawdown(bench),
        "turnover": sold / float(nav.mean()) / (max(len(nav) - 1, 1) / YEAR_DAYS),
        "excess_by_year": {year: mine[year] - base[year] for year in mine},
    }


def summarize(runs: list[dict[str, Any]], stress: list[dict[str, Any]]) -> dict[str, Any]:
    """同一种做法从不同日子开始换仓各跑一遍，取中位数：结果不该取决于哪天开始。"""
    keys = ("annual", "bench_annual", "excess", "sharpe", "information_ratio", "drawdown", "bench_drawdown", "turnover")
    summary: dict[str, Any] = {key: float(np.median([run[key] for run in runs])) for key in keys}
    years = sorted(runs[0]["excess_by_year"])
    summary["excess_by_year"] = {year: float(np.median([run["excess_by_year"][year] for run in runs])) for year in years}
    summary["years"] = len(years)
    summary["years_ahead"] = sum(1 for value in summary["excess_by_year"].values() if value > 0)
    summary["worst_start_excess"] = float(min(run["excess"] for run in runs))
    summary["stress_excess"] = float(np.median([run["excess"] for run in stress]))
    return summary


def evaluate(panel: Panel, scores: np.ndarray, bench: np.ndarray, *, top_n: int, start: str, end: str) -> dict[str, Any]:
    def one(offset: int, costs: Costs) -> dict[str, Any]:
        run = simulate(panel, scores, top_n=top_n, start=start, end=end, offset=offset, costs=costs)
        return metrics(run["dates"], run["nav"], bench, run["sold"])

    return summarize([one(offset, BASE_COSTS) for offset in range(HOLD_DAYS)], [one(offset, STRESS_COSTS) for offset in range(HOLD_DAYS)])


def dev_passes(item: dict[str, Any], current: dict[str, Any]) -> bool:
    return (
        item["excess"] >= DEV_MIN_EXCESS
        and item["information_ratio"] >= MIN_INFORMATION_RATIO
        and item["years_ahead"] >= DEV_MIN_YEAR_SHARE * item["years"]
        and item["worst_start_excess"] > 0
        and item["excess"] > current["excess"]
    )


def dev_choice(results: dict[str, dict[str, dict[str, Any]]]) -> dict[str, str | None]:
    """每个只数进最终验证的做法：模型通过而且不输给等权才用模型，否则等权通过就用等权，都不行就没有。"""
    chosen: dict[str, str | None] = {}
    for top_n in results["current"]:
        current = results["current"][top_n]
        model, composite = results["model"][top_n], results["composite"][top_n]
        if dev_passes(model, current) and model["excess"] > composite["excess"]:
            chosen[top_n] = "model"
        elif dev_passes(composite, current):
            chosen[top_n] = "composite"
        else:
            chosen[top_n] = None
    return chosen


def final_passes(item: dict[str, Any]) -> bool:
    return (
        item["excess"] >= FINAL_MIN_EXCESS
        and item["information_ratio"] >= MIN_INFORMATION_RATIO
        and item["years_ahead"] >= FINAL_MIN_YEAR_SHARE * item["years"]
        and item["drawdown"] >= item["bench_drawdown"] - MAX_EXTRA_DRAWDOWN
        and item["worst_start_excess"] > 0
        and item["stress_excess"] > 0
    )


def _all_scores(panel: Panel, out: Path | None, *, first_year: int, end: str, strategies: tuple[str, ...]) -> dict[str, np.ndarray]:
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= DEV_START) & (dates <= end))
    test_rows = np.flatnonzero((dates >= f"{first_year}0101") & (dates <= end))
    members = pool(panel)
    scores: dict[str, np.ndarray] = {}
    if "current" in strategies:
        scores["current"] = current_rule_scores(panel, test_rows)
    if set(strategies) & set(CANDIDATES):
        features = compute_all(panel, out, rows=rows, names=list(rm.RANK_MODEL_FEATURES))
        if "composite" in strategies:
            scores["composite"] = composite_scores(features, test_rows, members.shape)
        if "model" in strategies:
            label = forward_return(panel, rm.HORIZON, last_exit_date=end)
            scores["model"] = model_scores(panel, features, label, first_year=first_year, end=end)
    return {name: np.where(members, values, np.nan) for name, values in scores.items()}


def _print_table(results: dict[str, dict[str, dict[str, Any]]]) -> None:
    rows = []
    for name, by_n in results.items():
        for top_n, item in by_n.items():
            rows.append(
                {
                    "做法": name,
                    "只数": top_n,
                    "年化%": item["annual"] * 100,
                    "基准年化%": item["bench_annual"] * 100,
                    "超额%": item["excess"] * 100,
                    "信息比率": item["information_ratio"],
                    "最大回撤%": item["drawdown"] * 100,
                    "基准回撤%": item["bench_drawdown"] * 100,
                    "跑赢年数": f"{item['years_ahead']}/{item['years']}",
                    "最差起点超额%": item["worst_start_excess"] * 100,
                    "双倍费用超额%": item["stress_excess"] * 100,
                    "年换手": item["turnover"],
                }
            )
    print(pd.DataFrame(rows).round(2).to_string(index=False), flush=True)


def _reports(out: Path) -> Path:
    folder = out / "_reports"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def dev_report_path(out: Path) -> Path:
    return _reports(out) / f"selection_dev_{DEV_END}.json"


def final_report_path(out: Path) -> Path:
    return _reports(out) / "selection_final.json"


def run_stage(panel: Panel, out: Path | None, *, first_year: int, end: str, strategies: tuple[str, ...], top_ns: tuple[int, ...]) -> dict[str, dict[str, dict[str, Any]]]:
    start = f"{first_year}0101"
    scores = _all_scores(panel, out, first_year=first_year, end=end, strategies=strategies)
    bench = benchmark_nav(panel, pool(panel), start, end)
    results: dict[str, dict[str, dict[str, Any]]] = {}
    for name in strategies:
        results[name] = {str(top_n): evaluate(panel, scores[name], bench, top_n=top_n, start=start, end=end) for top_n in top_ns}
        print(f"{name} 回测完成", flush=True)
    return results


def run_dev(out: Path) -> dict[str, Any]:
    panel = Panel.open(out, end=DEV_END)
    results = run_stage(panel, out, first_year=FIRST_DEV_YEAR, end=DEV_END, strategies=STRATEGIES, top_ns=TOP_NS)
    _print_table(results)
    report = {"range": [f"{FIRST_DEV_YEAR}0101", DEV_END], "results": results, "chosen": dev_choice(results)}
    dev_report_path(out).write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"进最终验证的做法（按只数）：{report['chosen']}", flush=True)
    return report


def run_final(out: Path) -> dict[str, Any]:
    target = final_report_path(out)
    if target.exists():
        raise SystemExit(f"最终验证已经跑过（{target}），2019 年以后的数据只能看一次。要换做法请另开版本、另写标准。")
    if not dev_report_path(out).exists():
        raise SystemExit("先跑 dev：最终验证只验证开发区间选出来的做法。")
    chosen = {top_n: name for top_n, name in json.loads(dev_report_path(out).read_text())["chosen"].items() if name}
    if not chosen:
        raise SystemExit("开发区间没有做法通过，不做最终验证。")
    panel = Panel.open(out)
    end = panel.dates[-1]
    results: dict[str, dict[str, dict[str, Any]]] = {}
    for name in sorted(set(chosen.values()) | {"current"}):
        top_ns = tuple(int(top_n) for top_n, picked in chosen.items() if picked == name or name == "current")
        results.update(run_stage(panel, out, first_year=FIRST_FINAL_YEAR, end=end, strategies=(name,), top_ns=top_ns))
    _print_table(results)
    verdict = {top_n: {"strategy": name, "passed": final_passes(results[name][top_n])} for top_n, name in chosen.items()}
    report = {"range": [f"{FIRST_FINAL_YEAR}0101", end], "results": results, "verdict": verdict}
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"最终判定：{verdict}", flush=True)
    return report


def run_picks(out: Path, top_n: int | None = None) -> dict[str, Any]:
    """最新一个交易日收盘后的名单。只有最终验证通过的做法和只数才出。"""
    if not final_report_path(out).exists():
        raise SystemExit("还没有最终验证的结果，不出名单。")
    verdict = json.loads(final_report_path(out).read_text())["verdict"]
    passed = {int(key): item["strategy"] for key, item in verdict.items() if item["passed"]}
    if not passed:
        raise SystemExit("最终验证没有通过，不出名单。")
    top_n = top_n or min(passed)
    if top_n not in passed:
        raise SystemExit(f"{top_n} 只没有通过最终验证，通过的是 {sorted(passed)}。")
    panel = Panel.open(out)
    last = len(panel.dates) - 1
    dates = np.array(panel.dates)
    rows = np.flatnonzero(dates >= DEV_START)
    features = compute_all(panel, out, rows=rows, names=list(rm.RANK_MODEL_FEATURES))
    members = pool(panel)
    if passed[top_n] == "model":
        label = forward_return(panel, rm.HORIZON, last_exit_date=panel.dates[-1])
        train_rows = rows[rows < last - rm.HORIZON][:: rm.TRAIN_STEP]
        train_x, train_y = rm.assemble(features, list(rm.RANK_MODEL_FEATURES), train_rows), rm.target_rank(label, train_rows)
        mask = panel.universe[train_rows] & ~np.isnan(train_y)
        model = rm._fit(train_x[mask], train_y[mask], "regression")
        today = rm.assemble(features, list(rm.RANK_MODEL_FEATURES), np.array([last]))[0]
        score = np.full(len(panel.codes), np.nan)
        score[panel.universe[last]] = model.predict(today[panel.universe[last]])
    else:
        score = composite_scores(features, np.array([last]), members.shape)[last].astype(np.float64)
    score = np.where(members[last], score, np.nan)
    order = np.argsort(-np.where(np.isnan(score), -np.inf, score), kind="stable")[: min(top_n, int((~np.isnan(score)).sum()))]
    picks = [{"code": panel.codes[stock], "rank": position + 1, "score": float(score[stock])} for position, stock in enumerate(order.tolist())]
    result = {"trade_date": panel.dates[last], "strategy": passed[top_n], "top_n": top_n, "hold_days": HOLD_DAYS, "picks": picks}
    (_reports(out) / f"selection_picks_{panel.dates[last]}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps(result, ensure_ascii=False, indent=1), flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="选股回测")
    parser.add_argument("command", choices=["dev", "final", "picks"])
    parser.add_argument("--out", required=True, type=lambda value: Path(value).expanduser())
    parser.add_argument("--top-n", type=int, default=None)
    args = parser.parse_args()
    if args.command == "dev":
        run_dev(args.out)
    elif args.command == "final":
        run_final(args.out)
    else:
        run_picks(args.out, args.top_n)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    raise SystemExit(main())
