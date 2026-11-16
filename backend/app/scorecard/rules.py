"""建议成绩单的规则：一条建议算对还是错。纯计算，不读库。

口径（2026-11-15 写定，改了要在 docs/advice-scorecard.md 里记一笔）：

- 建议买或拿着（buy/add/hold）：之后跑赢全市场平均算对。
- 建议卖（sell/reduce/stop_loss）：之后跑输全市场平均算对，因为卖掉躲开了相对下跌。
- 从建议生效那天的收盘算起，到第 5、10、20 个交易日收盘。只看方向对不对，不看挂单价多赚的那一点。
- 差距不到 1 个百分点算“差不多”，不算对也不算错：这么小的差距分不出是判断还是运气。
- 每一类至少 30 条有结果的建议才下结论，不够就只显示数字、不评价。
"""
from __future__ import annotations

from typing import Any, Iterable

HORIZONS = (5, 10, 20)
PRIMARY_HORIZON = 10
DEAD_BAND = 0.01
MIN_SAMPLES = 30
MIN_MARKET_STOCKS = 3000  # 当天有行情的股票少于这么多，说明收盘数据没到齐
RETURN_CLIP = 0.31  # 单日涨跌超过这个幅度的（新股首日、错误数据）按上限算，不让它带偏全市场平均
MODEL_HIGH = 0.7
MODEL_LOW = 0.3

UP = "up"
DOWN = "down"
RIGHT = "right"
WRONG = "wrong"
FLAT = "flat"
NO_DATA = "no_data"

_UP_ACTIONS = frozenset({"buy", "add", "hold"})
_DOWN_ACTIONS = frozenset({"sell", "reduce", "stop_loss"})
ACTION_GROUPS = {"buy": "买入", "add": "买入", "hold": "继续拿着", "sell": "卖出", "reduce": "卖出", "stop_loss": "止损"}
MODEL_VIEW_LABELS = {"agree": "模型也这么看", "disagree": "模型看法相反", "neutral": "模型没有明确看法", "unknown": "当时没有模型数据"}


def direction(action: str | None) -> str | None:
    """这条操作押的是涨还是跌；撤单这类不表态的返回 None。"""
    if action in _UP_ACTIONS:
        return UP
    if action in _DOWN_ACTIONS:
        return DOWN
    return None


def daily_return(close: float | None, pre_close: float | None) -> float | None:
    """用昨收算当天涨跌，除权日也对。数据不全返回 None。"""
    if close is None or pre_close is None or pre_close <= 0 or close <= 0:
        return None
    return max(-RETURN_CLIP, min(RETURN_CLIP, close / pre_close - 1))


def compound(returns: Iterable[float]) -> float:
    total = 1.0
    for value in returns:
        total *= 1 + value
    return total - 1


def verdict(side: str, excess: float) -> str:
    edge = excess if side == UP else -excess
    if edge >= DEAD_BAND:
        return RIGHT
    if edge <= -DEAD_BAND:
        return WRONG
    return FLAT


def edge(side: str, excess: float) -> float:
    """照这条建议做，比不做多赚（正）或少赚（负）多少，相对全市场平均。"""
    return excess if side == UP else -excess


def model_view(side: str, rank: float | None) -> str:
    if rank is None:
        return "unknown"
    if rank >= MODEL_HIGH:
        return "agree" if side == UP else "disagree"
    if rank <= MODEL_LOW:
        return "agree" if side == DOWN else "disagree"
    return "neutral"


def latest_per_day(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一个账户同一天同一只股票同一个方向只留最后那份计划里的一条。不同用户、不同账户各算各的。

    items 要带 owner、account、advice_date、symbol、direction、order。
    """
    kept: dict[tuple, dict[str, Any]] = {}
    for item in items:
        key = (item["owner"], item["account"], item["advice_date"], item["symbol"], item["direction"])
        if key not in kept or item["order"] >= kept[key]["order"]:
            kept[key] = item
    return list(kept.values())


def summarize(rows: Iterable[dict[str, Any]], *, min_samples: int = MIN_SAMPLES) -> dict[str, Any]:
    """rows 要带 verdict、direction、excess。没到期和没有行情的不算。"""
    counts = {RIGHT: 0, WRONG: 0, FLAT: 0}
    edges: list[float] = []
    for row in rows:
        if row["verdict"] not in counts:
            continue
        counts[row["verdict"]] += 1
        edges.append(edge(row["direction"], row["excess"]))
    total = len(edges)
    decided = counts[RIGHT] + counts[WRONG]
    return {
        "total": total,
        "right": counts[RIGHT],
        "wrong": counts[WRONG],
        "flat": counts[FLAT],
        "right_rate": counts[RIGHT] / decided if decided else None,
        "avg_edge": sum(edges) / total if total else None,
        "enough": total >= min_samples,
    }
