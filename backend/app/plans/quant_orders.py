"""把量化挂单价接进计划：出计划前写进提示，保存计划时校验，数字人改不了卖价和止损价。

只管盘前、午盘、晚间这三种第二天或当天执行的计划；下周、下月计划和选股计划的价格不是下一个交易日的，不管。
算不出来时（没有统计表、K 线不够）不拦，计划照常走，绝不能因为这里出错耽误报告。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.models import Instrument, MarketEvent, Position, StockModelSignal, WatchlistItem
from backend.app.core.settings import settings
from backend.app.events.rules import CRITICAL
from backend.app.plans import rules
from backend.app.plans.service import to_utc
from backend.app.quant.order_engine import OrderLevels, describe_orders, load_table, model_buy_price, plan_orders, price_problem
from backend.app.quant.tech_signal_service import load_bars

logger = logging.getLogger(__name__)

QUANT_SOURCES = frozenset({"premarket", "midday", "evening"})
_URGENT_WINDOW = timedelta(days=3)
_table_cache: dict[str, Any] = {}


def _table() -> dict[str, Any] | None:
    if "table" not in _table_cache:
        _table_cache["table"] = load_table()
    return _table_cache["table"]


def basis_date(plan_date: str) -> str:
    from backend.app.market.stock_tools import _latest_completed_trade_date

    return _latest_completed_trade_date(datetime.fromisoformat(plan_date))


def levels_for(db: Session, symbols: set[str], plan_date: str) -> dict[str, OrderLevels]:
    """用计划生效前一个交易日的收盘 K 线计算挂单价。"""
    table = _table()
    if not symbols or table is None or not settings.quant_orders_enabled:
        return {}
    basis = basis_date(plan_date)
    names = dict(db.execute(select(Instrument.symbol, Instrument.name).where(Instrument.symbol.in_(symbols))).all())
    bars = load_bars(db, sorted(symbols), basis)
    levels: dict[str, OrderLevels] = {}
    for symbol, series in bars.items():
        if not series or series[-1].trade_date != basis:
            continue
        result = plan_orders(series, table, limit_ratio=rules.limit_ratio(symbol, names.get(symbol)))
        if result is not None:
            levels[symbol] = result
    return levels


def model_buys(db: Session, levels: dict[str, OrderLevels], plan_date: str) -> dict[str, float | None]:
    """买入模型给的买价，None 是模型说不挂。当天没算出信号的股票不在结果里，不校验。"""
    if not levels:
        return {}
    rows = db.execute(
        select(StockModelSignal.symbol, StockModelSignal.buy_k).where(StockModelSignal.trade_date == basis_date(plan_date), StockModelSignal.symbol.in_(levels))
    ).all()
    names = dict(db.execute(select(Instrument.symbol, Instrument.name).where(Instrument.symbol.in_(levels))).all())
    return {
        row.symbol: None if row.buy_k is None else model_buy_price(levels[row.symbol].close, levels[row.symbol].atr, row.buy_k, rules.limit_ratio(row.symbol, names.get(row.symbol)))
        for row in rows
    }


def enforceable_buys(db: Session, source: str, plan_date: str, symbols: set[str]) -> dict[str, float | None]:
    """保存计划时要校验的买价。出错返回空：校验不了就不校验。"""
    if source not in QUANT_SOURCES:
        return {}
    try:
        return model_buys(db, levels_for(db, symbols, plan_date), plan_date)
    except Exception as exc:  # noqa: BLE001 - 量化价只是约束，出错不能挡住计划
        logger.warning("买入模型的价计算失败，这次不校验: %s", str(exc)[:200])
        return {}


def buy_problem(buys: dict[str, float | None], symbol: str, action: str, price: float) -> str | None:
    if rules.item_family(action) != "buy" or symbol not in buys:
        return None
    expected = buys[symbol]
    if expected is None:
        return "买入模型判断这只股票下一个交易日挂买单没有优势，不要挂买单"
    if abs(float(price) - expected) > 0.011:
        return f"买价要用系统算的 {expected:.2f} 元，现在写的是 {float(price):.2f} 元"
    return None


def urgent_symbols(db: Session, owner_user_id: int, symbols: set[str], now_utc: datetime) -> set[str]:
    """立案、退市这类重大消息之后要能马上卖，价格不受统计规则限制。"""
    if not symbols:
        return set()
    return set(
        db.scalars(
            select(MarketEvent.symbol).where(
                MarketEvent.owner_user_id == owner_user_id,
                MarketEvent.symbol.in_(symbols),
                MarketEvent.severity == CRITICAL,
                MarketEvent.created_at >= now_utc - _URGENT_WINDOW,
            )
        ).all()
    )


def enforceable_levels(db: Session, owner_user_id: int, source: str, plan_date: str, symbols: set[str], now: datetime) -> dict[str, OrderLevels]:
    """保存计划时要校验的股票。出错返回空：校验不了就不校验。"""
    if source not in QUANT_SOURCES:
        return {}
    try:
        levels = levels_for(db, symbols, plan_date)
        for symbol in urgent_symbols(db, owner_user_id, set(levels), to_utc(now)):
            levels.pop(symbol, None)
        return levels
    except Exception as exc:  # noqa: BLE001 - 量化价只是约束，出错不能挡住计划
        logger.warning("量化挂单价计算失败，这次不校验: %s", str(exc)[:200])
        return {}


def item_problem(levels: OrderLevels | None, action: str, trigger_type: str, price: float) -> str | None:
    """跌破才卖的条件单按止损校验（不能比下限更近），其他卖出按卖价校验。买单这一版不校验。"""
    if levels is None or rules.item_family(action) != "sell":
        return None
    return price_problem(levels, "stop_loss" if action == "stop_loss" or trigger_type == "fall_to" else "sell", price)


def plan_block(db: Session, owner_user_id: int, plan_date: str, now: datetime) -> str:
    """写进出计划任务提示的一段：每只持仓和自选的量化价。"""
    held = db.scalars(
        select(Position.symbol).where(Position.owner_user_id == owner_user_id, Position.status == "open", Position.quantity > 0)
    ).all()
    watched = db.scalars(
        select(WatchlistItem.symbol).where(WatchlistItem.owner_user_id == owner_user_id, WatchlistItem.status == "active")
    ).all()
    symbols = set(held) | set(watched)
    levels = levels_for(db, symbols, plan_date)
    if not levels:
        return ""
    urgent = urgent_symbols(db, owner_user_id, set(levels), to_utc(now))
    names = dict(db.execute(select(Instrument.symbol, Instrument.name).where(Instrument.symbol.in_(levels))).all())
    buys = model_buys(db, levels, plan_date)

    def buy_text(symbol: str) -> str:
        if symbol not in buys:
            return ""
        return f"｜买单 {buys[symbol]:.2f} 元" if buys[symbol] is not None else "｜买单：买入模型判断没有优势，不挂"

    lines = [describe_orders((names.get(symbol) or symbol).strip(), symbol, level) + buy_text(symbol) for symbol, level in sorted(levels.items())]
    buy_rule = (
        "买单同样只能用上面的价，标了“不挂”的不要挂买单，写别的价存不进去。买单的价由买入模型算（2019 年以来验证过），买多少股看下面的量化股数。"
        if buys
        else "买价这一版还没有量化结果，由你定。"
    )
    note = f"\n这几只有重大消息，卖价不受上面限制，可以按需要定：{'、'.join(sorted(urgent))}。" if urgent else ""
    return (
        "量化挂单价（代码按 2012 年以来全市场统计算出：每个价位的成交概率和成交后相对大盘的走势，取价值最大的价）：\n"
        + "\n".join(lines)
        + note
        + "\n规则：卖单只能用上面的价，save_trade_plan 会校验，写别的价存不进去；标了“不挂”的不要挂卖单。"
        "止损可以不设，设了不能高于上面的价：回测里 1 到 2 倍 ATR 的止损把年化打到负数。"
        "卖多少股看下面的量化股数（没有就由你判断），不要因为股票走弱就挂靠近昨收的卖单。"
        + buy_rule
        + "正文每条卖单写清价格依据，比如“昨收+1.5 倍 ATR，成交概率 3%”。"
    )
