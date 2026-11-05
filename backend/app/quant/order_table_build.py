"""生成量化挂单价的统计表：全市场每只股票每天，在各个价位挂单，统计成交概率和成交后走势。

    python -m backend.app.quant.order_table_build evaluate --out ~/hermes-stock-research
    python -m backend.app.quant.order_table_build build --out ~/hermes-stock-research

evaluate 用开发区间建表，在验证区间比较代码选的价和基线。build 用全部数据生成上线用的表。
指标用当天收盘算，挂单下一个交易日成交，没有未来数据。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.app.quant.order_engine import (
    BUY_COST,
    BUY_GRID,
    DOWN,
    MIN_NET_EDGE,
    RANGE,
    SELL_COST,
    SELL_GRID,
    STOP_GRID,
    TABLE_PATH,
    UP,
    VOL_EDGES,
    net_edge,
    side_value,
)
from backend.app.quant.research_panel import Panel

HORIZON = 10
TRENDS = (UP, DOWN, RANGE)
BUCKETS = (0, 1, 2)
CELLS = [f"{trend}|{bucket}" for trend in TRENDS for bucket in BUCKETS]
DEV_RANGE = ("20120701", "20181231")
VALIDATION_RANGE = ("20190101", "20261231")
FULL_RANGE = ("20120701", "20261231")
GRIDS = {"sell": SELL_GRID, "buy": BUY_GRID, "stop": STOP_GRID}


def market_state(panel: Panel) -> dict[str, np.ndarray]:
    close = panel.adj_close_filled
    frame = pd.DataFrame(close)
    high, low = panel.adjusted("high"), panel.adjusted("low")
    previous = frame.shift(1).to_numpy()
    true_range = np.fmax(np.fmax(high - low, np.abs(high - previous)), np.abs(low - previous))
    atr = pd.DataFrame(true_range).ewm(alpha=1 / 14, adjust=False, ignore_na=True).mean().to_numpy()
    ma20 = frame.rolling(20, min_periods=20).mean().to_numpy()
    before = np.vstack([np.full((5, close.shape[1]), np.nan), ma20[:-5]])
    with np.errstate(invalid="ignore", divide="ignore"):
        trend = np.full(close.shape, 2, dtype=np.int8)
        trend[(close > ma20) & (ma20 > before)] = 0
        trend[(close < ma20) & (ma20 < before)] = 1
        atr_pct = atr / close
        bucket = np.where(atr_pct < VOL_EDGES[0], 0, np.where(atr_pct < VOL_EDGES[1], 1, 2)).astype(np.int8)
    daily = frame.pct_change(fill_method=None).where(pd.DataFrame(panel.traded))
    market = (1 + daily.mean(axis=1).fillna(0)).cumprod().to_numpy()
    eligible = panel.universe & ~np.isnan(atr) & ~np.isnan(ma20) & ~np.isnan(before)
    return {"close": close, "atr": atr, "cell": trend * 3 + bucket, "market": market, "eligible": eligible}


def _fills(panel: Panel, state: dict[str, np.ndarray], side: str, multiple: float, t: int, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """第 t+1 天在第 t 天算的价位挂单：哪些成交了，成交价多少。"""
    close, atr = state["close"][t][mask], state["atr"][t][mask]
    open_next = panel.adjusted("open")[t + 1][mask]
    if side == "sell":
        price = close + multiple * atr
        return panel.adjusted("high")[t + 1][mask] >= price, np.maximum(open_next, price)
    price = close - multiple * atr
    return panel.adjusted("low")[t + 1][mask] <= price, np.minimum(open_next, price)


def _usable(panel: Panel, state: dict[str, np.ndarray], t: int) -> np.ndarray:
    return state["eligible"][t] & panel.traded[t + 1] & ~panel.one_price[t + 1]


def build_table(panel: Panel, state: dict[str, np.ndarray], start: str, end: str) -> dict[str, Any]:
    """按趋势和波动分组统计。成交后走势用 10 日后收盘相对成交价，再减同期全市场等权。"""
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= start) & (dates <= end))
    rows = rows[rows + 1 + HORIZON < len(dates)]
    size = len(CELLS)
    table: dict[str, Any] = {"fit_range": [start, end], "horizon": HORIZON}
    for side, grid in GRIDS.items():
        table[side] = {}
        totals = {k: {"n": np.zeros(size), "fills": np.zeros(size), "edge": np.zeros(size), "raw": np.zeros(size)} for k in grid}
        for t in rows:
            mask = _usable(panel, state, t)
            if not mask.any():
                continue
            cells = state["cell"][t][mask]
            bench = state["market"][t + 1 + HORIZON] / state["market"][t + 1] - 1
            later = state["close"][t + 1 + HORIZON][mask]
            for k in grid:
                hit, fill = _fills(panel, state, side, k, t, mask)
                move = later / fill - 1
                good = hit & ~np.isnan(move)
                bucket = totals[k]
                bucket["n"] += np.bincount(cells, minlength=size)
                bucket["fills"] += np.bincount(cells[good], minlength=size)
                bucket["edge"] += np.bincount(cells[good], weights=move[good] - bench, minlength=size)
                bucket["raw"] += np.bincount(cells[good], weights=move[good], minlength=size)
        for index, cell in enumerate(CELLS):
            table[side][cell] = {}
            for k in grid:
                bucket = totals[k]
                fills = bucket["fills"][index]
                if bucket["n"][index] == 0 or fills < 200:  # 样本太少不算数
                    continue
                table[side][cell][str(k)] = {
                    "fill": round(float(fills / bucket["n"][index]), 5),
                    "edge": round(float(bucket["edge"][index] / fills), 5),
                    "raw": round(float(bucket["raw"][index] / fills), 5),
                    "n": int(fills),
                }
    return table


def best_multiples(table: dict[str, Any], side: str) -> dict[int, float | None]:
    """每个分组价值最大的挂单倍数；不大于 0 是 None。"""
    chosen: dict[int, float | None] = {}
    for index, cell in enumerate(CELLS):
        options = {
            float(k): side_value(side, s["fill"], s["edge"])
            for k, s in (table[side].get(cell) or {}).items()
            if net_edge(side, s["edge"]) >= MIN_NET_EDGE
        }
        best = max(options, key=options.get) if options else None
        chosen[index] = best if best is not None and options[best] > 0 else None
    return chosen


def evaluate_policy(panel: Panel, state: dict[str, np.ndarray], side: str, multiples: dict[int, float | None], start: str, end: str, *, fixed_pct: float | None = None) -> pd.DataFrame:
    """按某种选价办法挂单，统计每挂一次的实际价值。fixed_pct 是基线：昨收加减固定比例。"""
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= start) & (dates <= end))
    rows = rows[rows + 1 + HORIZON < len(dates)]
    records: dict[str, list[float]] = {}
    cost = BUY_COST if side == "buy" else SELL_COST
    sign = 1 if side == "buy" else -1
    for t in rows:
        mask = _usable(panel, state, t)
        if not mask.any():
            continue
        cells = state["cell"][t][mask]
        close, atr = state["close"][t][mask], state["atr"][t][mask]
        if fixed_pct is not None:
            price = close * (1 + fixed_pct if side == "sell" else 1 - fixed_pct)
            placed = np.ones(len(close), dtype=bool)
        else:
            lookup = np.array([np.nan if multiples.get(i) is None else multiples[i] for i in range(len(CELLS))])
            chosen = lookup[cells]
            placed = ~np.isnan(chosen)
            price = close + chosen * atr if side == "sell" else close - chosen * atr
        open_next = panel.adjusted("open")[t + 1][mask]
        with np.errstate(invalid="ignore"):
            if side == "sell":
                hit, fill = panel.adjusted("high")[t + 1][mask] >= price, np.maximum(open_next, price)
            else:
                hit, fill = panel.adjusted("low")[t + 1][mask] <= price, np.minimum(open_next, price)
        later = state["close"][t + 1 + HORIZON][mask]
        bench = state["market"][t + 1 + HORIZON] / state["market"][t + 1] - 1
        move = later / fill - 1 - bench
        good = hit & placed & ~np.isnan(move)
        bucket = records.setdefault(panel.dates[t][:4], [0.0, 0.0, 0.0, 0.0])
        bucket[0] += float(len(close))  # 股票日
        bucket[1] += float(placed.sum())  # 挂单次数
        bucket[2] += float(good.sum())  # 成交次数
        bucket[3] += float((sign * move[good] - cost).sum())  # 价值合计
    frame = pd.DataFrame(records, index=["股票日", "挂单", "成交", "价值"]).T
    frame["挂单率%"] = frame["挂单"] / frame["股票日"] * 100
    frame["成交率%"] = frame["成交"] / frame["挂单"].where(frame["挂单"] > 0) * 100
    frame["每次挂单价值%"] = frame["价值"] / frame["股票日"] * 100
    frame["每次成交价值%"] = frame["价值"] / frame["成交"].where(frame["成交"] > 0) * 100
    return frame


def run_evaluate(panel: Panel) -> dict[str, Any]:
    state = market_state(panel)
    table = build_table(panel, state, *DEV_RANGE)
    verdict: dict[str, Any] = {}
    for side, baseline_pct in (("sell", 0.01), ("buy", 0.01), ("stop", 0.05)):
        chosen = best_multiples(table, "sell" if side == "sell" else side)
        print(f"\n===== {side}：开发区间选出的倍数 " + "，".join(f"{CELLS[i]}={v}" for i, v in chosen.items()))
        if side == "stop":
            ordered = {i: 2.0 for i in chosen}
            policy = evaluate_policy(panel, state, side, ordered, *VALIDATION_RANGE)
            print("固定 2 倍 ATR 止损（验证区间）")
            print(policy[["挂单率%", "成交率%", "每次挂单价值%", "每次成交价值%"]].round(4).to_string())
            continue
        policy = evaluate_policy(panel, state, side, chosen, *VALIDATION_RANGE)
        baseline = evaluate_policy(panel, state, side, chosen, *VALIDATION_RANGE, fixed_pct=baseline_pct)
        print(f"代码选价（验证区间）\n{policy[['挂单率%', '成交率%', '每次挂单价值%', '每次成交价值%']].round(4).to_string()}")
        print(f"基线 昨收±{baseline_pct:.0%}（验证区间）\n{baseline[['挂单率%', '成交率%', '每次挂单价值%', '每次成交价值%']].round(4).to_string()}")
        mine, base = policy["价值"].sum() / policy["股票日"].sum(), baseline["价值"].sum() / baseline["股票日"].sum()
        better = int((policy["每次挂单价值%"] > baseline["每次挂单价值%"]).sum())
        verdict[side] = {
            "value_pct": mine * 100,
            "baseline_pct": base * 100,
            "years_better": better,
            "years": len(policy),
            "passed": bool(mine > 0 and mine > base and better >= 0.6 * len(policy)),
        }
        print(f"合计：代码选价 {mine * 100:+.4f}%，基线 {base * 100:+.4f}%，{better}/{len(policy)} 年高于基线 → {'通过' if verdict[side]['passed'] else '没通过'}")
    return verdict


def run_build(panel: Panel, path: Path = TABLE_PATH) -> dict[str, Any]:
    table = build_table(panel, market_state(panel), *FULL_RANGE)
    path.write_text(json.dumps(table, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description="量化挂单价统计表")
    parser.add_argument("command", choices=["evaluate", "build"])
    parser.add_argument("--out", required=True, type=lambda value: Path(value).expanduser())
    args = parser.parse_args()
    panel = Panel.open(args.out)
    if args.command == "evaluate":
        print(json.dumps(run_evaluate(panel), ensure_ascii=False, indent=1))
    else:
        run_build(panel)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    raise SystemExit(main())
