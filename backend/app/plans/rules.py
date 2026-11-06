"""计划规则：任务归类、交易单位、涨跌停、触价判断、正文核对。全是纯函数，不碰数据库。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable

SOURCE_LABELS = {
    "premarket": "早盘计划",
    "midday": "午盘调整",
    "evening": "晚间计划",
    "weekly": "下周计划",
    "monthly": "下月计划",
    "selection": "选股计划",
}
ACTION_LABELS = {
    "buy": "买入",
    "add": "加仓",
    "sell": "卖出",
    "reduce": "减仓",
    "hold": "拿着",
    "stop_loss": "止损",
    "cancel": "撤单",
}
TRIGGER_TYPES = ("limit", "rise_to", "fall_to", "none")
BUY_ACTIONS = frozenset({"buy", "add"})
SELL_ACTIONS = frozenset({"sell", "reduce"})
NO_TRIGGER_ACTIONS = frozenset({"hold", "cancel"})
MARKET_CLOSE = time(15, 0)

# 内置任务按 job_key，用户自建任务（job_key 是随机的）按名称。
_JOB_KEY_SOURCES = {
    "evening-daily-2200": "evening",
    "weekend-review": "weekly",
    "month-end-review": "monthly",
    "selection-action-plan-2000": "selection",
}
_JOB_NAME_SOURCES = (
    ("挂单调整", "midday"),
    ("挂单清单", "premarket"),
    ("晚间日报", "evening"),
    ("下周操作计划", "weekly"),
    ("下月操作计划", "monthly"),
    ("全自动选股", "selection"),
)
_SAME_DAY_SOURCES = frozenset({"premarket", "midday"})
_MULTI_DAY_SOURCES = frozenset({"weekly", "monthly"})

_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_NUMBER = re.compile(r"(?<![\d.])\d+(?:\.\d+)?")


def plan_source_for_job(job_key: str | None, name: str | None) -> str | None:
    """这个定时任务出的是不是操作计划；不是就返回 None，流程和原来一样。"""
    source = _JOB_KEY_SOURCES.get(str(job_key or ""))
    if source:
        return source
    for marker, candidate in _JOB_NAME_SOURCES:
        if marker in str(name or ""):
            return candidate
    return None


def next_open_day(after: date, is_open: Callable[[date], bool]) -> date:
    day = after
    for _ in range(30):
        day += timedelta(days=1)
        if is_open(day):
            return day
    return after + timedelta(days=1)


def effective_plan_date(source: str, now: datetime, is_open: Callable[[date], bool]) -> date:
    """计划从哪个交易日开始生效：早盘、午盘盯当天，其余（含收盘后手动重跑）盯下一个交易日。"""
    today = now.date()
    if source in _SAME_DAY_SOURCES and is_open(today) and now.time() < MARKET_CLOSE:
        return today
    return next_open_day(today, is_open)


def is_multi_day(source: str) -> bool:
    return source in _MULTI_DAY_SOURCES


def item_family(action: str) -> str | None:
    if action in BUY_ACTIONS:
        return "buy"
    if action in SELL_ACTIONS or action == "stop_loss":
        return "sell"
    return None


def trade_family(action: str | None) -> str | None:
    if action in ("buy", "add"):
        return "buy"
    if action in ("sell", "reduce", "close"):
        return "sell"
    return None


# ------------------------------------------------------------------ 交易规则
def _is_star(symbol: str) -> bool:
    return symbol.startswith(("688", "689"))


def _is_bse(symbol: str) -> bool:
    return symbol.endswith(".BJ")


def limit_ratio(symbol: str, name: str | None) -> float:
    if _is_bse(symbol):
        return 0.30
    if _is_star(symbol) or symbol.startswith(("300", "301", "302")):
        return 0.20
    return 0.05 if "ST" in str(name or "").upper() else 0.10


def price_limits(symbol: str, name: str | None, reference: float) -> tuple[float, float]:
    ratio = limit_ratio(symbol, name)
    return round(reference * (1 - ratio), 2), round(reference * (1 + ratio), 2)


def quantity_problem(symbol: str, action: str, quantity: float, held: float = 0) -> str | None:
    """A 股交易单位：一般 100 股整数倍，科创板至少 200 股，北交所至少 100 股。

    卖出时不足一手的零股要一次卖掉，所以卖光，或者整手加上全部零股，也是合规的。
    """
    if quantity <= 0:
        return "股数要大于 0"
    selling = action not in BUY_ACTIONS
    if selling and held and quantity == held:
        return None
    if _is_star(symbol):
        return None if quantity >= 200 else "科创板至少 200 股"
    if _is_bse(symbol):
        return None if quantity >= 100 else "北交所至少 100 股"
    if float(quantity) % 100 == 0 or (selling and held and float(quantity) % 100 == float(held) % 100):
        return None
    return "股数要是 100 的整数倍"


# ------------------------------------------------------------------ 触价判断
@dataclass(frozen=True)
class TriggerHit:
    via: str  # price：现价碰到；spike：两次刷新之间的尖峰，靠当日最高/最低价发现
    price: float


def watch_direction(action: str, trigger_type: str) -> str | None:
    """up：价格涨到计划价算碰到；down：跌到计划价算碰到；None：不用盯。"""
    if action in NO_TRIGGER_ACTIONS or trigger_type == "none":
        return None
    if action == "stop_loss" or trigger_type == "fall_to":
        return "down"
    if trigger_type == "rise_to":
        return "up"
    return "up" if action in SELL_ACTIONS else "down"


def evaluate_trigger(
    *,
    direction: str,
    trigger_price: float,
    price: float,
    high: float | None,
    low: float | None,
    baseline_high: float | None,
    baseline_low: float | None,
) -> TriggerHit | None:
    """基准是第一次盯价时的当日最高/最低价：之前的高低点不算，避免午盘计划被上午的高点误触发。"""
    if direction == "up":
        if price >= trigger_price:
            return TriggerHit("price", price)
        if high is not None and baseline_high is not None and high > baseline_high and high >= trigger_price:
            return TriggerHit("spike", high)
        return None
    if price <= trigger_price:
        return TriggerHit("price", price)
    if low is not None and baseline_low is not None and low < baseline_low and low <= trigger_price:
        return TriggerHit("spike", low)
    return None


# ------------------------------------------------------------------ 文字
def format_price(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}" if round(value, 2) == round(value, 4) else f"{value:.3f}"


def format_quantity(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f}"


def describe_plan(action: str, trigger_type: str, price: float | None, quantity: float | None) -> str:
    """一条计划的白话说法，例如“涨到 24.13 卖 500 股”。"""
    if action == "hold":
        return "拿着不动"
    if action == "cancel":
        return "撤掉挂单"
    shares = f"{format_quantity(quantity)} 股"
    level = format_price(price)
    if action == "stop_loss":
        return f"跌破 {level} 止损 {shares}"
    verb = "买" if action in BUY_ACTIONS else "卖"
    if trigger_type == "rise_to":
        return f"涨到 {level} {verb} {shares}"
    if trigger_type == "fall_to":
        return f"跌到 {level} {verb} {shares}"
    return f"挂 {level} {verb} {shares}"


# ------------------------------------------------------------------ 正文核对
def describe_advice(advised: int | float) -> str:
    """代码建议的股数：正数卖，负数可以补，0 不动。"""
    if advised > 0:
        return f"卖 {format_quantity(advised)} 股"
    if advised < 0:
        return f"可以补 {format_quantity(-advised)} 股"
    return "不动"


def _plain_number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def text_numbers(text: str) -> set[str]:
    found: set[str] = set()
    for value in _NUMBER.findall(_THOUSANDS.sub("", text)):
        found.add(value.rstrip("0").rstrip(".") if "." in value else value)
    return found


def text_problem(
    *,
    symbol: str,
    name: str | None,
    price: float | None,
    quantity: float | None,
    text: str,
    numbers: set[str],
) -> str | None:
    """这条计划在正文里找不找得到；思路和白话改写的 check_facts_kept 一样，只看有没有，不看在哪一行。"""
    code = symbol.split(".")[0]
    if code not in text and not (name and name in text):
        return "正文里没有这只股票"
    if price is not None and _plain_number(price) not in numbers:
        return f"正文里没有价格 {format_price(price)}"
    if quantity is not None and _plain_number(quantity) not in numbers:
        return f"正文里没有股数 {format_quantity(quantity)}"
    return None
