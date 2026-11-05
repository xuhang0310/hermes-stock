"""三个预测模型（Q3）：只在开发区间（2018 年及以前）滚动训练和比较，2019 年之后留给 Q5。

    python -m backend.app.quant.research_models run --out ~/hermes-stock-research

1. 收益排名：未来 10 日相对全市场的分位。梯度提升回归。
2. 成交概率：明天价格碰到“昨收 ± k 倍 ATR”的概率。梯度提升二分类，k 是输入之一。
   它和分位数回归是同一件事的两种做法（都在估明日最高、最低价的分布），分类直接输出成交概率，校准更好检查。
3. 成交后走势：已知碰到 k，成交价到 10 日后相对全市场的涨跌。只用碰到过的样本训练，
   估的正是公式需要的“成交条件下的期望”，所以不需要再做样本选择修正。

每个模型都和基线比，最后看的是“每挂一次的实际价值”，跑不赢 order_table 那张统计表就不用。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pandas as pd

from backend.app.quant.factors.factor_analysis_core import compute_ic_series
from backend.app.quant.order_engine import SELL_COST, SELL_GRID
from backend.app.quant.order_table_build import CELLS, best_multiples, build_table, market_state
from backend.app.quant.research_features import DEV_END, DEV_START, compute_all, forward_return
from backend.app.quant.research_panel import Panel

HORIZON = 10
FIRST_TEST_YEAR = 2015
TRAIN_STEP = 5  # 训练样本隔 5 天取一天，相邻日的标签高度重叠，全用只是重复
# 影子 B 验证通过的特征。线上只同步这些特征要的数据，所以不跟着特征检验报告变；换特征要另开影子账户。
RANK_MODEL_FEATURES = ("amount_20", "vp_corr_20", "turnover_f", "ret_20", "ma_gap_60", "dist_low_20", "dist_low_120")
EVAL_STEP = 5
SEED = 20261105
_BOOST_ROUNDS = 200
_LGB_COMMON = {
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 500,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "feature_fraction": 0.8,
    "lambda_l2": 1.0,
    "seed": SEED,
    "num_threads": 4,
    "verbosity": -1,
}


@dataclass(frozen=True)
class Split:
    year: int
    train: np.ndarray
    test: np.ndarray


def walk_forward(dates: list[str], *, first_year: int = FIRST_TEST_YEAR, start: str = DEV_START, end: str = DEV_END, gap: int = HORIZON + 1) -> Iterator[Split]:
    """用之前的数据训练，预测这一年。训练集最后留出 gap 天，它们的标签会读到这一年的价格。"""
    array = np.array(dates)
    last_year = int(end[:4])
    for year in range(first_year, last_year + 1):
        test = np.flatnonzero((array >= f"{year}0101") & (array <= f"{year}1231") & (array <= end))
        if not len(test):
            continue
        train = np.flatnonzero(array >= start)
        train = train[train < test[0] - gap]
        if len(train):
            yield Split(year, train, test)


def rank_features(values: np.ndarray, rows: np.ndarray) -> np.ndarray:
    """每天做横截面排名（0 到 1），不同年份的数值才可比。缺值保持 NaN。"""
    out = np.full((len(rows), values.shape[1]), np.nan, dtype=np.float32)
    frame = pd.DataFrame(np.asarray(values[rows], dtype=np.float64))
    out[:] = frame.rank(axis=1, pct=True).to_numpy(dtype=np.float32)
    return out


def assemble(features: dict[str, np.ndarray], names: list[str], rows: np.ndarray) -> np.ndarray:
    """形状 (日期, 股票, 特征) 的排名矩阵。"""
    return np.stack([rank_features(features[name], rows) for name in names], axis=2)


def _fit(x: np.ndarray, y: np.ndarray, objective: str):
    """用 LightGBM 原生接口，不依赖 scikit-learn。binary 的 predict 直接返回概率。"""
    import lightgbm

    return lightgbm.train({**_LGB_COMMON, "objective": objective}, lightgbm.Dataset(x, label=y), num_boost_round=_BOOST_ROUNDS)


def target_rank(label: np.ndarray, rows: np.ndarray) -> np.ndarray:
    return pd.DataFrame(label[rows]).rank(axis=1, pct=True).to_numpy(dtype=np.float32)


def model_rank_ic(panel: Panel, features: dict[str, np.ndarray], names: list[str], signs: dict[str, float], label: np.ndarray) -> pd.DataFrame:
    """模型 1：逐年样本外的 Rank IC，和“已选特征按方向等权”的基线比。"""
    universe = panel.universe
    results = []
    for split in walk_forward(panel.dates):
        train_rows = split.train[::TRAIN_STEP]
        train_x = assemble(features, names, train_rows)
        train_y = target_rank(label, train_rows)
        mask = universe[train_rows] & ~np.isnan(train_y)
        model = _fit(train_x[mask], train_y[mask], "regression")

        test_rows = split.test
        test_x = assemble(features, names, test_rows)
        test_mask = universe[test_rows]
        predicted = np.full(test_mask.shape, np.nan, dtype=np.float32)
        predicted[test_mask] = model.predict(test_x[test_mask])
        index = np.array(panel.dates)[test_rows]
        label_df = pd.DataFrame(label[test_rows], index=index)
        model_ic = compute_ic_series(pd.DataFrame(predicted, index=index), label_df)
        sign_vector = np.array([signs[name] for name in names], dtype=np.float32)
        with np.errstate(invalid="ignore"):
            base = np.nanmean(test_x * sign_vector + (1 - sign_vector) / 2, axis=2)
        base_ic = compute_ic_series(pd.DataFrame(np.where(test_mask, base, np.nan), index=index), label_df)
        results.append({"年": split.year, "模型IC": float(model_ic.mean()), "基线IC": float(base_ic.mean())})
        print(f"模型1 {split.year}: 模型 IC {model_ic.mean():+.4f}，基线 IC {base_ic.mean():+.4f}", flush=True)
    return pd.DataFrame(results)


def touch_samples(panel: Panel, state: dict[str, np.ndarray], x: np.ndarray, rows: np.ndarray, *, side: str = "sell", grid: tuple[float, ...] = SELL_GRID, per_stock_day: int = 2, seed: int = SEED, only_touched: bool = False) -> dict[str, np.ndarray]:
    """每个股票日随机抽几档价位，算碰没碰到、碰到后的走势。x 的第一维和 rows 对齐。"""
    rng = np.random.default_rng(seed)
    high, low, open_ = panel.adjusted("high"), panel.adjusted("low"), panel.adjusted("open")
    close, atr, market = state["close"], state["atr"], state["market"]
    grid_array = np.array(grid, dtype=np.float32)
    out_x, out_k, out_touch, out_edge = [], [], [], []
    for position, t in enumerate(rows):
        if t + 1 + HORIZON >= close.shape[0]:
            continue
        mask = state["eligible"][t] & panel.traded[t + 1] & ~panel.one_price[t + 1]
        stocks = np.flatnonzero(mask)
        if not len(stocks):
            continue
        bench = market[t + 1 + HORIZON] / market[t + 1] - 1
        for _ in range(per_stock_day):
            k = grid_array[rng.integers(0, len(grid_array), len(stocks))]
            base, step = close[t][stocks], atr[t][stocks]
            if side == "sell":
                price = base + k * step
                touched = high[t + 1][stocks] >= price
                fill = np.maximum(open_[t + 1][stocks], price)
            else:
                price = base - k * step
                touched = low[t + 1][stocks] <= price
                fill = np.minimum(open_[t + 1][stocks], price)
            edge = close[t + 1 + HORIZON][stocks] / fill - 1 - bench
            keep = touched & ~np.isnan(edge) if only_touched else ~np.isnan(edge)
            out_x.append(x[position][stocks[keep]])
            out_k.append(k[keep])
            out_touch.append(touched[keep])
            out_edge.append(edge[keep].astype(np.float32))
    return {
        "x": np.column_stack([np.concatenate(out_x), np.concatenate(out_k)]),
        "touched": np.concatenate(out_touch).astype(np.int8),
        "edge": np.concatenate(out_edge),
    }


def calibration(predicted: np.ndarray, actual: np.ndarray, *, bins: int = 10) -> pd.DataFrame:
    """按预测概率分组，每组的预测均值和实际成交率。"""
    order = pd.qcut(predicted, bins, labels=False, duplicates="drop")
    frame = pd.DataFrame({"组": order, "预测": predicted, "实际": actual})
    table = frame.groupby("组").agg(预测=("预测", "mean"), 实际=("实际", "mean"), 样本=("实际", "size"))
    table["差"] = table["预测"] - table["实际"]
    return table


def evaluate_year(panel: Panel, state: dict[str, np.ndarray], features: dict[str, np.ndarray], names: list[str], split: Split) -> dict[str, Any]:
    """一年的样本外：模型 2、3 和统计表比，最后比实际挂单价值。只看卖单。"""
    train_rows = split.train[::TRAIN_STEP]
    train_x = assemble(features, names, train_rows)
    all_samples = touch_samples(panel, state, train_x, train_rows)
    fill_model = _fit(all_samples["x"], all_samples["touched"], "binary")
    touched = all_samples["touched"] == 1
    edge_model = _fit(all_samples["x"][touched], all_samples["edge"][touched], "regression")
    del all_samples, train_x

    start, end = panel.dates[split.train[0]], panel.dates[split.train[-1]]
    table = build_table(panel, state, start, end)
    multiples = best_multiples(table, "sell")
    lookup = np.array([np.nan if multiples.get(i) is None else multiples[i] for i in range(len(CELLS))])

    test_rows = split.test[::EVAL_STEP]
    test_x = assemble(features, names, test_rows)
    high, open_ = panel.adjusted("high"), panel.adjusted("open")
    close, atr, market = state["close"], state["atr"], state["market"]
    grid = np.array(SELL_GRID, dtype=np.float32)
    fill_pred, fill_true = [], []
    totals = {"days": 0, "model_value": 0.0, "table_value": 0.0, "model_fills": 0, "table_fills": 0, "model_orders": 0, "table_orders": 0}
    for position, t in enumerate(test_rows):
        if t + 1 + HORIZON >= close.shape[0]:
            continue
        stocks = np.flatnonzero(state["eligible"][t] & panel.traded[t + 1] & ~panel.one_price[t + 1])
        if not len(stocks):
            continue
        base, step = close[t][stocks], atr[t][stocks]
        high_next, open_next = high[t + 1][stocks], open_[t + 1][stocks]
        later = close[t + 1 + HORIZON][stocks]
        bench = market[t + 1 + HORIZON] / market[t + 1] - 1
        rows_x = test_x[position][stocks]
        best_value = np.full(len(stocks), -np.inf)
        best_k = np.full(len(stocks), np.nan)
        for k in grid:
            features_k = np.column_stack([rows_x, np.full(len(stocks), k, dtype=np.float32)])
            probability = fill_model.predict(features_k)
            edge = edge_model.predict(features_k)
            value = probability * (-edge - SELL_COST)
            better = value > best_value
            best_value[better], best_k[better] = value[better], k
            fill_pred.append(probability)
            fill_true.append(high_next >= base + k * step)
        best_k[best_value <= 0] = np.nan

        def realized(chosen: np.ndarray) -> tuple[float, int, int]:
            price = base + chosen * step
            with np.errstate(invalid="ignore"):
                hit = high_next >= price
            fill = np.maximum(open_next, price)
            move = later / fill - 1 - bench
            good = hit & ~np.isnan(chosen) & ~np.isnan(move)
            return float((-move[good] - SELL_COST).sum()), int(good.sum()), int((~np.isnan(chosen)).sum())

        value_m, fills_m, orders_m = realized(best_k)
        value_t, fills_t, orders_t = realized(lookup[state["cell"][t][stocks]])
        totals["days"] += len(stocks)
        totals["model_value"] += value_m
        totals["table_value"] += value_t
        totals["model_fills"] += fills_m
        totals["table_fills"] += fills_t
        totals["model_orders"] += orders_m
        totals["table_orders"] += orders_t
    calib = calibration(np.concatenate(fill_pred), np.concatenate(fill_true).astype(np.int8))
    return {
        "年": split.year,
        "模型价值%": totals["model_value"] / totals["days"] * 100,
        "统计表价值%": totals["table_value"] / totals["days"] * 100,
        "模型挂单率%": totals["model_orders"] / totals["days"] * 100,
        "统计表挂单率%": totals["table_orders"] / totals["days"] * 100,
        "模型成交次数": totals["model_fills"],
        "统计表成交次数": totals["table_fills"],
        "校准最大差": float(calib["差"].abs().max()),
        "calibration": calib,
    }


def predict_ranks(out: Path, names: list[str]) -> Path:
    """滚动预测模型 1 的排名，存成“日期 × 股票”的矩阵。每一年都只用之前的数据训练，所以可以直接用来模拟。"""
    panel = Panel.open(out)
    last = panel.dates[-1]
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= DEV_START) & (dates <= last))
    features = compute_all(panel, out, rows=rows, names=names)
    label = forward_return(panel, HORIZON, last_exit_date=last)
    universe = panel.universe
    predicted = np.full((len(panel.dates), len(panel.codes)), np.nan, dtype=np.float32)
    for split in walk_forward(panel.dates, end=last):
        train_rows = split.train[::TRAIN_STEP]
        train_x, train_y = assemble(features, names, train_rows), target_rank(label, train_rows)
        mask = universe[train_rows] & ~np.isnan(train_y)
        model = _fit(train_x[mask], train_y[mask], "regression")
        test_x = assemble(features, names, split.test)
        test_mask = universe[split.test]
        scores = np.full(test_mask.shape, np.nan, dtype=np.float32)
        scores[test_mask] = model.predict(test_x[test_mask])
        predicted[split.test] = pd.DataFrame(scores).rank(axis=1, pct=True).to_numpy(dtype=np.float32)
        print(f"{split.year} 预测完成，训练样本 {int(mask.sum())}", flush=True)
    target = out / "_cache" / f"model1_ranks_{last}.npy"
    np.save(target, predicted)
    return target


def export_rank_model(out: Path, names: list[str], target: Path) -> dict[str, Any]:
    """用全部数据训练模型 1，导出成线上用的文件，并和 LightGBM 逐条核对。"""
    from backend.app.quant import tree_model

    panel = Panel.open(out)
    last = panel.dates[-1]
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= DEV_START) & (dates <= last))
    features = compute_all(panel, out, rows=rows, names=names)
    label = forward_return(panel, HORIZON, last_exit_date=last)
    train_rows = rows[::TRAIN_STEP]
    train_x, train_y = assemble(features, names, train_rows), target_rank(label, train_rows)
    mask = panel.universe[train_rows] & ~np.isnan(train_y)
    booster = _fit(train_x[mask], train_y[mask], "regression")
    dump = booster.dump_model()
    document = {
        "names": names,
        "trained_until": last,
        "horizon": HORIZON,
        "note": "LightGBM 模型 1 导出，用 quant/tree_model.py 计算",
        "trees": [tree_model.flatten_tree(tree["tree_structure"]) for tree in dump["tree_info"]],
    }
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, target)
    sample = train_x[mask][:: max(1, int(mask.sum()) // 200_000)]
    difference = np.abs(booster.predict(sample) - tree_model.predict(tree_model.load_model(target), sample))
    return {"trees": len(document["trees"]), "trained_until": last, "rows": int(mask.sum()), "max_difference": float(difference.max())}


def run(out: Path) -> dict[str, Any]:
    panel = Panel.open(out, end=DEV_END)
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= DEV_START) & (dates <= DEV_END))
    report = json.loads((out / "_reports" / f"feature_ic_{DEV_END}.json").read_text())
    names = list(report["selected"])
    signs = {name: 1.0 if report["stats"][name]["ic"] > 0 else -1.0 for name in names}
    print(f"特征 {len(names)} 个：{names}", flush=True)
    features = compute_all(panel, out, rows=rows)
    state = market_state(panel)
    label = forward_return(panel, HORIZON, last_exit_date=DEV_END)
    rank_table = model_rank_ic(panel, features, names, signs, label)
    print(rank_table.round(4).to_string(index=False), flush=True)
    years = []
    for split in walk_forward(panel.dates):
        result = evaluate_year(panel, state, features, names, split)
        calib = result.pop("calibration")
        print(f"\n模型2、3 {split.year}: {json.dumps({k: round(v, 4) if isinstance(v, float) else v for k, v in result.items()}, ensure_ascii=False)}", flush=True)
        print(calib.round(4).to_string(), flush=True)
        years.append(result)
    frame = pd.DataFrame(years)
    summary = {"rank": rank_table.to_dict("records"), "orders": years}
    folder = out / "_reports"
    (folder / f"models_{DEV_END}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float))
    print("\n" + frame.round(4).to_string(index=False), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="预测模型（只在开发区间）")
    parser.add_argument("command", choices=["run", "predict", "export"])
    parser.add_argument("--out", required=True, type=lambda value: Path(value).expanduser())
    args = parser.parse_args()
    if args.command == "export":
        target = Path(__file__).with_name("rank_model.json")
        print(export_rank_model(args.out, list(RANK_MODEL_FEATURES), target), flush=True)
    elif args.command == "predict":
        report = json.loads((args.out / "_reports" / f"feature_ic_{DEV_END}.json").read_text())
        print(predict_ranks(args.out, list(report["selected"])), flush=True)
    else:
        run(args.out)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    raise SystemExit(main())
