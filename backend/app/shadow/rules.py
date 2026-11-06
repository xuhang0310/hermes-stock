"""影子账户的挂单规则。纯计算，数字人碰不到。回测和验证见 docs/quant-order-plan.md 第 0 节。

算法是风险预算（固定风险比例定仓位），加上限价单的再平衡：

    目标股数 = min( 1% × 投入资金 ÷ (2 × ATR)， 25% × 投入资金 ÷ 收盘价 )

- 投入资金 = 持仓市值 + 卖出后留着买回的钱。账户里原来闲置的现金不算，总仓位不会高过开始时。
- 持股超过目标 5% 以上：挂卖单，股数是超出的部分，价格用 order_engine 算的；统计上没优势就不挂。
- 持股低于目标 5% 以上且有卖出款：挂买单买回，价格是昨收 -1%，金额不超过卖出款。
- 不设止损：回测里 2 倍 ATR 止损把年化从 +5.8% 拉到 -0.7%。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from backend.app.quant.order_engine import BUY_COST, SELL_COST, OrderLevels, model_buy_price

RISK_BUDGET = 0.01
POSITION_CAP = 0.25
BAND = 0.05
SIZING_ATR = 2.0
BUY_DISCOUNT = 0.01
LOT = 100
STAR_MIN = 200
ANNUALIZE_MIN_DAYS = 60  # 不满这么多交易日，年化没有意义
TRADING_DAYS = 244
DISCOUNT = object()  # 买回价用昨收 -1%，影子 A、B 的规则


@dataclass(frozen=True)
class PlannedOrder:
    side: str  # sell / buy
    price: float
    quantity: int
    basis: str


def target_shares(invested: float, close: float, atr: float, *, risk_budget: float = RISK_BUDGET, position_cap: float = POSITION_CAP) -> float:
    if invested <= 0 or close <= 0 or atr <= 0:
        return 0.0
    return min(risk_budget * invested / (SIZING_ATR * atr), position_cap * invested / close)


def _round_lot(symbol: str, quantity: float) -> int:
    """向下取整到整手。科创板最少 200 股，之上可以 1 股递增，这里统一按整百，简单也不违规。"""
    shares = int(quantity // LOT) * LOT
    if symbol.startswith("688") and shares < STAR_MIN:
        return 0
    return max(shares, 0)


def rank_tilt(rank: float | None) -> float:
    """影子 B：目标股数乘以 0.5 + 排名，排名最后减半，最前 1.5 倍。没有排名不调整。"""
    return 1.0 if rank is None else 0.5 + rank


def plan_order(symbol: str, shares: float, levels: OrderLevels, invested: float, pool: float, *, tilt: float = 1.0, buy_k: Any = DISCOUNT, limit_ratio: float = 0.1, risk_budget: float = RISK_BUDGET, position_cap: float = POSITION_CAP) -> PlannedOrder | None:
    """这只股票下一个交易日挂什么单；不挂返回 None。

    buy_k 是影子 C 用的：买入模型选的倍数，None 表示模型说不买。
    risk_budget、position_cap 是真实账户按持股只数调整时用的，影子账户一直用默认值。
    """
    target = target_shares(invested, levels.close, levels.atr, risk_budget=risk_budget, position_cap=position_cap) * tilt
    if target <= 0:
        return None
    if shares > target * (1 + BAND):
        if levels.sell is None:
            return None
        quantity = _round_lot(symbol, shares - target)
        if quantity <= 0:
            return None
        return PlannedOrder(
            "sell",
            levels.sell.price,
            quantity,
            f"超出风险预算（目标约 {target:,.0f} 股）；卖价为昨收+{levels.sell.atr_multiple:g} 倍 ATR，历史成交概率 {levels.sell.fill * 100:.0f}%",
        )
    if shares < target * (1 - BAND) and pool > 0:
        if buy_k is DISCOUNT:
            price: float | None = math.floor(levels.close * (1 - BUY_DISCOUNT) * 100 + 1e-9) / 100
            how = "买价为昨收 -1%"
        elif buy_k is None:
            return None
        else:
            price = model_buy_price(levels.close, levels.atr, buy_k, limit_ratio)
            how = f"买入模型选的价，昨收-{buy_k:g} 倍 ATR"
        if price is None or price <= 0:
            return None
        quantity = _round_lot(symbol, min(target - shares, pool / (price * (1 + BUY_COST))))
        if quantity <= 0:
            return None
        return PlannedOrder("buy", price, quantity, f"低于风险预算（目标约 {target:,.0f} 股），用卖出款买回；{how}")
    return None


def fill_price(side: str, price: float, open_price: float, high: float, low: float) -> float | None:
    """当天能不能成交，成交价多少。全天一个价的一字板排不到队，算没成交。"""
    if high - low < 1e-9:
        return None
    if side == "sell":
        return max(open_price, price) if high >= price else None
    return min(open_price, price) if low <= price else None


def sell_proceeds(quantity: float, price: float) -> float:
    return quantity * price * (1 - SELL_COST)


def buy_cost(quantity: float, price: float) -> float:
    return quantity * price * (1 + BUY_COST)


def ex_rights_ratio(previous_close: float | None, pre_close: float | None) -> float:
    """除权除息日持股要跟着调整，否则送转和分红会变成假的亏损。分红按再投入算。"""
    if not previous_close or not pre_close:
        return 1.0
    ratio = previous_close / pre_close
    return ratio if 0.1 <= ratio <= 10.0 else 1.0


def total_return(first: float, last: float) -> float | None:
    return last / first - 1 if first > 0 else None


def annualized(first: float, last: float, days: int) -> float | None:
    """不满 60 个交易日返回 None。"""
    if first <= 0 or last <= 0 or days < ANNUALIZE_MIN_DAYS:
        return None
    return (last / first) ** (TRADING_DAYS / days) - 1


def max_drawdown(values: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1)
    return worst
