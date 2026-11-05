"""用纯 NumPy 算 LightGBM 树模型的预测，线上不用装 LightGBM。

模型文件由 research_models.export_rank_model 导出：每棵树存成几列数组，叶子节点的左右孩子都是 -1。
和 LightGBM 的结果逐条核对过（research_models.export_rank_model 导出时会比较）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np


def flatten_tree(root: dict[str, Any]) -> dict[str, list]:
    """把 LightGBM dump_model 的嵌套结构压平成数组。"""
    feature: list[int] = []
    threshold: list[float] = []
    default_left: list[bool] = []
    nan_is_zero: list[bool] = []
    left: list[int] = []
    right: list[int] = []
    value: list[float] = []

    def walk(node: dict[str, Any]) -> int:
        index = len(feature)
        feature.append(-1)
        threshold.append(0.0)
        default_left.append(False)
        nan_is_zero.append(False)
        left.append(-1)
        right.append(-1)
        value.append(0.0)
        if "leaf_value" in node:
            value[index] = float(node["leaf_value"])
            return index
        if node.get("decision_type") != "<=":
            raise ValueError(f"不支持的分裂方式：{node.get('decision_type')}")
        if node.get("missing_type") == "Zero":
            raise ValueError("不支持把 0 当缺失的模型")
        feature[index] = int(node["split_feature"])
        threshold[index] = float(node["threshold"])
        default_left[index] = bool(node["default_left"])
        # LightGBM 在 missing_type 不是 NaN 时把缺失当 0 算
        nan_is_zero[index] = node.get("missing_type") != "NaN"
        left[index] = walk(node["left_child"])
        right[index] = walk(node["right_child"])
        return index

    walk(root)
    return {
        "feature": feature,
        "threshold": threshold,
        "default_left": default_left,
        "nan_is_zero": nan_is_zero,
        "left": left,
        "right": right,
        "value": value,
    }


def load_model(path: Path) -> dict[str, Any] | None:
    try:
        model = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for tree in model["trees"]:
        for key, kind in (
            ("feature", np.int32),
            ("threshold", np.float64),
            ("default_left", bool),
            ("nan_is_zero", bool),
            ("left", np.int32),
            ("right", np.int32),
            ("value", np.float64),
        ):
            tree[key] = np.array(tree[key], dtype=kind)
    return model


def _tree_predict(tree: dict[str, Any], x: np.ndarray) -> np.ndarray:
    rows = np.arange(len(x))
    nodes = np.zeros(len(x), dtype=np.int32)
    active = tree["feature"][nodes] >= 0
    while active.any():
        at = nodes[active]
        values = x[rows[active], tree["feature"][at]].astype(np.float64)
        missing = np.isnan(values)
        # 缺失当 0 的节点，把缺失换成 0 再比较
        values = np.where(missing & tree["nan_is_zero"][at], 0.0, values)
        still_missing = np.isnan(values)
        go_left = np.where(still_missing, tree["default_left"][at], values <= tree["threshold"][at])
        nodes[active] = np.where(go_left, tree["left"][at], tree["right"][at])
        active = tree["feature"][nodes] >= 0
    return tree["value"][nodes]


def predict_probability(model: dict[str, Any], x: np.ndarray) -> np.ndarray:
    """二分类模型：所有树的叶子值相加是对数几率。"""
    return 1.0 / (1.0 + np.exp(-predict(model, x)))


def predict(model: dict[str, Any], x: np.ndarray) -> np.ndarray:
    """x 的列顺序要和 model["names"] 一致。LightGBM 回归的结果是所有树的叶子值相加。"""
    total = np.zeros(len(x), dtype=np.float64)
    for tree in model["trees"]:
        total += _tree_predict(tree, x)
    return total
