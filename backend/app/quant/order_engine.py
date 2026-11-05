"""量化挂单价 v1：按趋势和波动分组查全市场统计表，算每个价位的价值，选最优价。

方案见 docs/quant-order-plan.md 第 0 节。统计表 order_table.json 由 order_table_build 用历史数据生成。

    卖单和止损的价值 = 成交概率 × ( -成交后 10 日相对大盘的涨跌 - 费用 )
    买单的价值       = 成交概率 × (  成交后 10 日相对大盘的涨跌 - 费用 )

卖单的最大价值不大于 0 就不挂。止损可以不设，设了不能比 3 倍 ATR 更近。买单没有跑赢简单基线，这一版只算不用。
这里只有纯计算，同样的 K 线和统计表永远得到同样的价格。后面的预测模型替换统计表时，plan_orders 的输入输出不变。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.app.quant.tech_signal import MIN_BARS, Bar, _atr, _ma, adjusted_bars

TABLE_PATH = Path(__file__).with_name("order_table.json")
SELL_COST = 0.0013
BUY_COST = 0.0008
UP, DOWN, RANGE = "up", "down", "range"
TREND_LABELS = {UP: "上升", DOWN: "下降", RANGE: "震荡"}
VOL_EDGES = (0.025, 0.04)  # ATR 占股价的比例，分低、中、高三档
VOL_LABELS = ("低波动", "中波动", "高波动")
SELL_GRID = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
BUY_GRID = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
STOP_GRID = (1.0, 1.5, 2.0, 2.5, 3.0)
# 止损的下限，不是建议价。模拟里紧止损明显拖累收益（2012-08 至 2018-12，随机 8 只，只加止损）：
# 1 倍 ATR 年化 -0.46%，2 倍 -0.69%，3 倍 +7.76%，一直拿着 +5.83%。所以可以不设，设了不能比 3 倍更近。
STOP_ATR = 3.0
# 成交一次扣费后至少多赚这么多才算有优势。差得太小就是噪音，还会选出紧贴昨收的卖价
MIN_NET_EDGE = 0.002


@dataclass(frozen=True)
class Level:
    price: float
    atr_multiple: float
    fill: float  # 成交概率
    edge: float  # 成交后 10 日相对大盘的涨跌
    value: float  # 每挂一次的价值


@dataclass(frozen=True)
class OrderLevels:
    close: float
    atr: float
    trend: str
    vol_bucket: int
    sell: Level | None
    buy: Level | None
    stop: Level | None


def load_table(path: Path = TABLE_PATH) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def vol_bucket(atr_pct: float) -> int:
    return 0 if atr_pct < VOL_EDGES[0] else 1 if atr_pct < VOL_EDGES[1] else 2


def trend_of(close: float, ma20: float, ma20_before: float) -> str:
    if close > ma20 and ma20 > ma20_before:
        return UP
    if close < ma20 and ma20 < ma20_before:
        return DOWN
    return RANGE


def net_edge(side: str, edge: float) -> float:
    """成交一次扣费后比继续拿着（或不买）多赚多少。"""
    return edge - BUY_COST if side == "buy" else -edge - SELL_COST


def side_value(side: str, fill: float, edge: float) -> float:
    return fill * net_edge(side, edge)


def _tick(price: float, *, up: bool) -> float:
    cents = price * 100
    return (math.ceil(cents - 1e-9) if up else math.floor(cents + 1e-9)) / 100


def _best(table: dict[str, Any], side: str, cell: str, close: float, atr: float, low: float, high: float) -> Level | None:
    best: Level | None = None
    for key, stats in (table.get(side, {}).get(cell) or {}).items():
        multiple = float(key)
        raw = close + multiple * atr if side == "sell" else close - multiple * atr
        price = _tick(raw, up=side == "sell")
        if not low <= price <= high:
            continue
        if net_edge(side, float(stats["edge"])) < MIN_NET_EDGE:
            continue
        level = Level(price, multiple, float(stats["fill"]), float(stats["edge"]), side_value(side, float(stats["fill"]), float(stats["edge"])))
        if best is None or level.value > best.value:
            best = level
    return best


def _stop(table: dict[str, Any], cell: str, close: float, atr: float, low: float) -> Level | None:
    stats = (table.get("stop", {}).get(cell) or {}).get(str(STOP_ATR))
    if stats is None:
        return None
    price = max(_tick(close - STOP_ATR * atr, up=False), low)
    return Level(price, STOP_ATR, float(stats["fill"]), float(stats["edge"]), side_value("stop", float(stats["fill"]), float(stats["edge"])))


def plan_orders(bars: list[Bar], table: dict[str, Any], *, limit_ratio: float = 0.1) -> OrderLevels | None:
    """按日期升序的 K 线算下一个交易日的挂单价。K 线不够返回 None。"""
    if len(bars) < MIN_BARS:
        return None
    adjusted = adjusted_bars(bars)
    closes = [bar.close for bar in adjusted]
    close = closes[-1]
    atr = _atr(adjusted)
    ma20, before = _ma(closes, 20), sum(closes[-25:-5]) / 20
    if not close or not atr or ma20 is None:
        return None
    trend = trend_of(close, ma20, before)
    bucket = vol_bucket(atr / close)
    cell = f"{trend}|{bucket}"
    low, high = round(close * (1 - limit_ratio), 2), round(close * (1 + limit_ratio), 2)
    sell = _best(table, "sell", cell, close, atr, low, high)
    buy = _best(table, "buy", cell, close, atr, low, high)
    return OrderLevels(
        close=close,
        atr=atr,
        trend=trend,
        vol_bucket=bucket,
        sell=sell if sell is not None and sell.value > 0 else None,
        buy=buy if buy is not None and buy.value > 0 else None,
        stop=_stop(table, cell, close, atr, low),
    )


def model_buy_price(close: float, atr: float, k: float, limit_ratio: float = 0.1) -> float | None:
    """买入模型选的挂单价。低于跌停价就挂不上，返回 None。"""
    price = _tick(close - k * atr, up=False)
    return price if price > 0 and price >= _tick(close * (1 - limit_ratio), up=True) else None


def describe_level(level: Level, close: float) -> str:
    return (
        f"{level.price:.2f} 元（昨收{'+' if level.price >= close else '-'}{level.atr_multiple:g} 倍 ATR，"
        f"成交概率 {level.fill * 100:.0f}%，成交后 10 日相对大盘 {level.edge * 100:+.2f}%）"
    )


def describe_orders(name: str, symbol: str, levels: OrderLevels) -> str:
    head = (
        f"- {name}（{symbol.split('.')[0]}）｜{TREND_LABELS[levels.trend]}趋势、{VOL_LABELS[levels.vol_bucket]}"
        f"｜昨收 {levels.close:.2f}，ATR {levels.atr:.2f}"
    )
    sell = f"卖单 {describe_level(levels.sell, levels.close)}" if levels.sell else "卖单：统计上没有优势，不挂"
    stop = f"止损如果设，不能高于 {levels.stop.price:.2f} 元" if levels.stop else "止损：没有数据"
    return f"{head}｜{sell}｜{stop}"


def price_problem(levels: OrderLevels, action: str, price: float) -> str | None:
    """数字人写的价格和系统算的不一致时返回原因。买单这一版不校验。"""
    if action == "stop_loss":
        if levels.stop is not None and float(price) > levels.stop.price + 0.011:
            return f"止损不能高于 {levels.stop.price:.2f} 元（昨收-{levels.stop.atr_multiple:g} 倍 ATR，回测里更近的止损明显拖累收益），现在写的是 {float(price):.2f} 元；也可以不设止损"
        return None
    if action in ("sell", "reduce"):
        level, label, missing = levels.sell, "卖价", "这只股票现在挂卖单统计上没有优势，不要挂卖单，可以只留止损"
    else:
        return None
    if level is None:
        return missing
    if abs(float(price) - level.price) > 0.011:
        return f"{label}要用系统算的 {describe_level(level, levels.close)}，现在写的是 {float(price):.2f} 元"
    return None
