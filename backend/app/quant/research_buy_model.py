"""买入模型（标准见 docs/quant-order-plan.md 第 4.9 节）：昨收下方每个候选价的成交概率和成交后走势，取价值最大的价。

    python -m backend.app.quant.research_buy_model dev --out ~/hermes-stock-research
    python -m backend.app.quant.research_buy_model final --variant 名字 --out ~/hermes-stock-research --allow-validation-data

每年只用之前的数据训练，结果存成“日期 × 股票”的挂单倍数矩阵（NaN 表示不挂），模拟和价值统计都读这个矩阵。
开发阶段面板只读到 2018 年底，从物理上碰不到验证数据。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.app.quant.order_engine import BUY_COST, BUY_GRID, MIN_NET_EDGE
from backend.app.quant.order_table_build import HORIZON, _usable, evaluate_policy, market_state
from backend.app.quant.research_features import DEV_END, DEV_START, compute_all
from backend.app.quant.research_models import FIRST_TEST_YEAR, RANK_MODEL_FEATURES, TRAIN_STEP, _fit, assemble, calibration, touch_samples, walk_forward
from backend.app.quant.research_panel import Panel

BASELINE_PCT = 0.01
SHORT_TERM_FEATURES = ("atr_pct", "rv_20", "ret_1", "ret_5", "range_1", "close_pos", "vol_ratio_5")
ALL_FEATURES = RANK_MODEL_FEATURES + SHORT_TERM_FEATURES
FIRST_FINAL_YEAR = 2019
ONLINE_VARIANT = "full14_edge"  # 第 4.9 节两关都通过的做法
FILL_MODEL_PATH = Path(__file__).with_name("buy_fill_model.json")
EDGE_MODEL_PATH = Path(__file__).with_name("buy_edge_model.json")
_CALIBRATION_STEP = 5


@dataclass(frozen=True)
class Variant:
    name: str
    features: tuple[str, ...]
    min_edge: float = 0.0
    direct: bool = False  # True：一个模型直接预测价值；False：概率 × 走势


VARIANTS = {
    variant.name: variant
    for variant in (
        Variant("rank7", RANK_MODEL_FEATURES),
        Variant("full14", ALL_FEATURES),
        Variant("full14_edge", ALL_FEATURES, min_edge=MIN_NET_EDGE),
        Variant("direct14", ALL_FEATURES, direct=True),
    )
}


def choose(probability: np.ndarray | None, edge: np.ndarray, grid: np.ndarray, min_edge: float = 0.0) -> np.ndarray:
    """每行选价值最大的候选价。edge 是扣费前走势，形状 (股票, 候选价)；probability 为 None 时 edge 已经是价值。"""
    net = edge - BUY_COST if probability is not None else edge
    value = probability * net if probability is not None else net
    value = np.where((value > 0) & (net >= min_edge), value, -np.inf)
    best = value.argmax(axis=1)
    out = grid[best].astype(np.float32)
    out[np.isneginf(value[np.arange(len(best)), best])] = np.nan
    return out


def _samples(panel: Panel, state: dict[str, np.ndarray], features: dict[str, np.ndarray], variant: Variant, train_rows: np.ndarray) -> dict[str, np.ndarray]:
    return touch_samples(panel, state, assemble(features, list(variant.features), train_rows), train_rows, side="buy", grid=BUY_GRID)


def fit_heads(panel: Panel, state: dict[str, np.ndarray], features: dict[str, np.ndarray], variant: Variant, train_rows: np.ndarray) -> tuple[Any, Any]:
    samples = _samples(panel, state, features, variant, train_rows)
    if variant.direct:
        target = np.where(samples["touched"] == 1, samples["edge"] - BUY_COST, 0.0).astype(np.float32)
        return None, _fit(samples["x"], target, "regression")
    touched = samples["touched"] == 1
    fill_model = _fit(samples["x"], samples["touched"], "binary")
    return fill_model, _fit(samples["x"][touched], samples["edge"][touched], "regression")


def _predict(fill_model: Any, edge_model: Any, x: np.ndarray, grid: np.ndarray) -> tuple[np.ndarray | None, np.ndarray]:
    probability = np.zeros((len(x), len(grid))) if fill_model is not None else None
    edge = np.zeros((len(x), len(grid)))
    for column, k in enumerate(grid):
        rows = np.column_stack([x, np.full(len(x), k, dtype=np.float32)])
        if fill_model is not None:
            probability[:, column] = fill_model.predict(rows)
        edge[:, column] = edge_model.predict(rows)
    return probability, edge


def predict_year(panel: Panel, state: dict[str, np.ndarray], features: dict[str, np.ndarray], variant: Variant, models: tuple[Any, Any], rows: np.ndarray, matrix: np.ndarray) -> pd.DataFrame | None:
    """把这一年的挂单倍数写进矩阵。只用 t 当天和之前的信息。"""
    fill_model, edge_model = models
    grid = np.array(BUY_GRID, dtype=np.float32)
    low = panel.adjusted("low")
    predicted, actual = [], []
    for position, t in enumerate(rows.tolist()):
        stocks = np.flatnonzero(state["eligible"][t])
        if not len(stocks):
            continue
        x = assemble(features, list(variant.features), np.array([t]))[0][stocks]
        probability, edge = _predict(fill_model, edge_model, x, grid)
        matrix[t, stocks] = choose(probability, edge, grid, variant.min_edge)
        if probability is not None and position % _CALIBRATION_STEP == 0 and t + 1 < low.shape[0]:
            base, step = state["close"][t][stocks], state["atr"][t][stocks]
            usable = panel.traded[t + 1][stocks] & ~panel.one_price[t + 1][stocks]
            for column, k in enumerate(grid):
                predicted.append(probability[usable, column])
                actual.append((low[t + 1][stocks] <= base - k * step)[usable])
    if not predicted:
        return None
    return calibration(np.concatenate(predicted), np.concatenate(actual).astype(np.int8))


def evaluate_matrix(panel: Panel, state: dict[str, np.ndarray], matrix: np.ndarray, start: str, end: str) -> pd.DataFrame:
    """按矩阵挂单的实际价值。同一批股票挂昨收 -1% 也算一遍，看价值来自选价还是来自不挂。"""
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= start) & (dates <= end))
    rows = rows[rows + 1 + HORIZON < len(dates)]
    open_, low = panel.adjusted("open"), panel.adjusted("low")
    records: dict[str, list[float]] = {}
    for t in rows.tolist():
        mask = _usable(panel, state, t)
        if not mask.any():
            continue
        close, atr = state["close"][t][mask], state["atr"][t][mask]
        chosen = np.asarray(matrix[t][mask], dtype=np.float64)
        placed = ~np.isnan(chosen)
        later = state["close"][t + 1 + HORIZON][mask]
        bench = state["market"][t + 1 + HORIZON] / state["market"][t + 1] - 1

        def realized(price: np.ndarray, where: np.ndarray) -> tuple[float, int]:
            with np.errstate(invalid="ignore"):
                hit = low[t + 1][mask] <= price
            fill = np.minimum(open_[t + 1][mask], price)
            move = later / fill - 1 - bench
            good = hit & where & ~np.isnan(move)
            return float((move[good] - BUY_COST).sum()), int(good.sum())

        value, fills = realized(close - np.nan_to_num(chosen) * atr, placed)
        same_value, _ = realized(close * (1 - BASELINE_PCT), placed)
        bucket = records.setdefault(panel.dates[t][:4], [0.0, 0.0, 0.0, 0.0, 0.0])
        bucket[0] += float(len(close))
        bucket[1] += float(placed.sum())
        bucket[2] += float(fills)
        bucket[3] += value
        bucket[4] += same_value
    frame = pd.DataFrame(records, index=["股票日", "挂单", "成交", "价值", "同股-1%价值"]).T
    frame["挂单率%"] = frame["挂单"] / frame["股票日"] * 100
    frame["成交率%"] = frame["成交"] / frame["挂单"].where(frame["挂单"] > 0) * 100
    frame["价值%"] = frame["价值"] / frame["股票日"] * 100
    frame["同股-1%价值%"] = frame["同股-1%价值"] / frame["股票日"] * 100
    return frame


def matrix_path(out: Path, variant: str, last_day: str) -> Path:
    return out / "_cache" / f"buy_k_{variant}_{last_day}.npy"


def run_variant(panel: Panel, state: dict[str, np.ndarray], features: dict[str, np.ndarray], variant: Variant, out: Path, first_year: int, end: str) -> dict[str, Any]:
    matrix = np.full((len(panel.dates), len(panel.codes)), np.nan, dtype=np.float32)
    worst = {}
    for split in walk_forward(panel.dates, first_year=first_year, end=end):
        models = fit_heads(panel, state, features, variant, split.train[::TRAIN_STEP])
        calib = predict_year(panel, state, features, variant, models, split.test, matrix)
        if calib is not None:
            worst[split.year] = float(calib["差"].abs().max())
        print(f"{variant.name} {split.year} 完成，成交概率最大偏差 {worst.get(split.year, float('nan')) * 100:.1f} 点", flush=True)
    np.save(matrix_path(out, variant.name, panel.dates[-1]), matrix)
    start = f"{first_year}0101"
    baseline = evaluate_policy(panel, state, "buy", {}, start, end, fixed_pct=BASELINE_PCT)
    frame = evaluate_matrix(panel, state, matrix, start, end)
    table = pd.DataFrame(
        {
            "模型价值%": frame["价值%"],
            "基线-1%%": baseline["每次挂单价值%"],
            "同股-1%价值%": frame["同股-1%价值%"],
            "挂单率%": frame["挂单率%"],
            "成交率%": frame["成交率%"],
            "基线成交率%": baseline["成交率%"],
        }
    )
    print(table.round(4).to_string(), flush=True)
    wins = int((table["模型价值%"] > table["基线-1%%"]).sum())
    summary = {
        "variant": variant.name,
        "value": float(table["模型价值%"].mean()),
        "baseline": float(table["基线-1%%"].mean()),
        "same_stocks": float(table["同股-1%价值%"].mean()),
        "wins": wins,
        "years": len(table),
        "calibration_worst": worst,
        "by_year": table.reset_index(names="年").to_dict("records"),
    }
    print({key: summary[key] for key in ("variant", "value", "baseline", "same_stocks", "wins", "years")}, flush=True)
    return summary


def run_dev(out: Path) -> list[dict[str, Any]]:
    panel = Panel.open(out, end=DEV_END)
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= DEV_START) & (dates <= DEV_END))
    features = compute_all(panel, out, rows=rows, names=list(ALL_FEATURES))
    state = market_state(panel)
    results = [run_variant(panel, state, features, variant, out, FIRST_TEST_YEAR, DEV_END) for variant in VARIANTS.values()]
    (out / "_reports" / f"buy_model_{DEV_END}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1, default=float))
    return results


def run_final(out: Path, name: str) -> dict[str, Any]:
    panel = Panel.open(out)
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= DEV_START) & (dates <= panel.dates[-1]))
    features = compute_all(panel, out, rows=rows, names=list(ALL_FEATURES))
    state = market_state(panel)
    result = run_variant(panel, state, features, VARIANTS[name], out, FIRST_FINAL_YEAR, panel.dates[-1])
    (out / "_reports" / f"buy_model_final_{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=float))
    return result


def _write_model(booster: Any, names: list[str], last_day: str, path: Path) -> None:
    from backend.app.quant import tree_model

    document = {
        "names": names,
        "trained_until": last_day,
        "note": "LightGBM 导出，用 quant/tree_model.py 计算。特征后面多一列 k（昨收下方几倍 ATR）",
        "trees": [tree_model.flatten_tree(tree["tree_structure"]) for tree in booster.dump_model()["tree_info"]],
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def export_models(out: Path) -> dict[str, Any]:
    """用全部数据训练线上用的两个模型，导出后和 LightGBM 逐条核对。"""
    from backend.app.quant import tree_model

    panel = Panel.open(out)
    last = panel.dates[-1]
    dates = np.array(panel.dates)
    rows = np.flatnonzero((dates >= DEV_START) & (dates <= last))
    variant = VARIANTS[ONLINE_VARIANT]
    names = list(variant.features)
    features = compute_all(panel, out, rows=rows, names=names)
    samples = _samples(panel, market_state(panel), features, variant, rows[::TRAIN_STEP])
    touched = samples["touched"] == 1
    fill_model = _fit(samples["x"], samples["touched"], "binary")
    edge_model = _fit(samples["x"][touched], samples["edge"][touched], "regression")
    _write_model(fill_model, names, last, FILL_MODEL_PATH)
    _write_model(edge_model, names, last, EDGE_MODEL_PATH)
    check = samples["x"][:: max(1, len(samples["x"]) // 200_000)]
    return {
        "trained_until": last,
        "samples": len(samples["x"]),
        "fill_difference": float(np.abs(fill_model.predict(check) - tree_model.predict_probability(tree_model.load_model(FILL_MODEL_PATH), check)).max()),
        "edge_difference": float(np.abs(edge_model.predict(check) - tree_model.predict(tree_model.load_model(EDGE_MODEL_PATH), check)).max()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="买入模型")
    parser.add_argument("command", choices=["dev", "final", "export"])
    parser.add_argument("--out", required=True, type=lambda value: Path(value).expanduser())
    parser.add_argument("--variant", choices=sorted(VARIANTS))
    parser.add_argument("--allow-validation-data", action="store_true")
    args = parser.parse_args()
    if args.command == "dev":
        run_dev(args.out)
        return 0
    if args.command == "export":
        print(export_models(args.out), flush=True)
        return 0
    if not args.allow_validation_data or not args.variant:
        parser.error("final 只跑一次：要写明 --variant 和 --allow-validation-data")
    run_final(args.out, args.variant)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    raise SystemExit(main())
