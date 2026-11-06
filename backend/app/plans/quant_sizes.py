"""把风险预算的目标股数接进计划：每个账户每只持仓，代码算出该卖多少、可以补多少，写进提示当默认值。

数字人可以改，但要在 size_note 写明为什么，没写的存不进去；报告定稿后的补存例外，那一步不能再改正文，只记下来。
第一版不强制股数：回测是随机 8 只满仓起步，和真实持仓不完全一样，先看几天再决定要不要强制。

规则和影子 B 相同（shadow/rules.py）：投入资金只算有 K 线的持仓，不算账户现金和 ETF；
目标股数 = min(风险预算 × 投入资金 ÷ 2 倍 ATR, 单只上限 × 投入资金 ÷ 收盘价) × (0.5 + 模型排名)。

和影子 B 的差别：持股不到 8 只时放宽，风险预算 = 1% × 8 ÷ 只数，单只上限 = max(25%, 1 ÷ 只数)。
1% 是按 8 只验证的，只数少时目标合起来远不到满仓（账户 4 只有 53%），卖出的钱又不买别的股票。
随机 4 只的验证见 docs/quant-order-plan.md 第 4.8.1 节，其他只数按同一公式推。
新开仓不管：验证过的规则不开新仓。有重大消息的股票也不管，要能按需要处理。
算不出来时不拦，计划照常走。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.models import Instrument, PositionAccount, StockModelSignal
from backend.app.core.settings import settings
from backend.app.plans import quant_orders, rules
from backend.app.plans.service import _sellable, open_holdings, to_utc
from backend.app.quant.order_engine import OrderLevels
from backend.app.shadow import rules as shadow_rules

logger = logging.getLogger(__name__)

LOT = 100
TYPICAL_STOCKS = 8  # 风险预算 1% 验证时的持股只数
TOLERANCE = 0.1  # 差一手或差 10% 以内不算改动
NO_REASON = "数字人没有写理由"


@dataclass(frozen=True)
class SizeAdvice:
    account_id: int
    symbol: str
    name: str
    shares: float
    target: float
    advised: int  # 正数建议卖，负数可以补，0 不动
    why: str = ""  # 不动的原因


def budget_for(stock_count: int) -> tuple[float, float]:
    """按持股只数定风险预算和单只上限。8 只及以上和影子 B 一样。"""
    count = max(stock_count, 1)
    return shadow_rules.RISK_BUDGET * max(1.0, TYPICAL_STOCKS / count), max(shadow_rules.POSITION_CAP, 1.0 / count)


def advise(symbol: str, shares: float, levels: OrderLevels, invested: float, rank: float | None, buy_k: float | None, limit_ratio: float, stock_count: int = TYPICAL_STOCKS) -> tuple[float, int, str]:
    """一只持仓的建议：目标股数、建议股数、不动的原因。和影子 B 的规则共用一份代码。"""
    tilt = shadow_rules.rank_tilt(rank)
    budget, cap = budget_for(stock_count)
    target = shadow_rules.target_shares(invested, levels.close, levels.atr, risk_budget=budget, position_cap=cap) * tilt
    order = shadow_rules.plan_order(symbol, shares, levels, invested, math.inf, tilt=tilt, buy_k=buy_k, limit_ratio=limit_ratio, risk_budget=budget, position_cap=cap)
    if order is not None:
        return target, order.quantity if order.side == "sell" else -order.quantity, ""
    if shares > target * (1 + shadow_rules.BAND):
        return target, 0, "超过目标，但卖单统计上没有优势，不挂" if levels.sell is None else "超出不到一手，不动"
    if shares < target * (1 - shadow_rules.BAND):
        return target, 0, "低于目标，但买入模型判断不挂，不补" if buy_k is None else "差不到一手，不动"
    return target, 0, "在目标附近，不动"


def size_advice(db: Session, owner_user_id: int, plan_date: str, now: datetime) -> dict[tuple[int, str], SizeAdvice]:
    """这位用户每个账户每只持仓的建议。没有 K 线的持仓（比如 ETF）和有重大消息的股票不在结果里。"""
    if not settings.quant_sizes_enabled:
        return {}
    active = set(db.scalars(select(PositionAccount.id).where(PositionAccount.owner_user_id == owner_user_id, PositionAccount.status == "active")).all())
    holdings = {key: row for key, row in open_holdings(db, owner_user_id).items() if key[0] in active}
    levels = quant_orders.levels_for(db, {symbol for _, symbol in holdings}, plan_date)
    if not levels:
        return {}
    signals = {
        row.symbol: row
        for row in db.execute(
            select(StockModelSignal.symbol, StockModelSignal.model_rank, StockModelSignal.buy_k).where(
                StockModelSignal.trade_date == quant_orders.basis_date(plan_date), StockModelSignal.symbol.in_(levels)
            )
        ).all()
    }
    names = dict(db.execute(select(Instrument.symbol, Instrument.name).where(Instrument.symbol.in_(levels))).all())
    invested: dict[int, float] = {}
    counts: dict[int, int] = {}
    for (account_id, symbol), row in holdings.items():
        if symbol in levels:
            invested[account_id] = invested.get(account_id, 0.0) + float(row.position_quantity) * levels[symbol].close
            counts[account_id] = counts.get(account_id, 0) + 1
    urgent = quant_orders.urgent_symbols(db, owner_user_id, set(levels), to_utc(now))
    same_day = plan_date == now.date().isoformat()
    advice: dict[tuple[int, str], SizeAdvice] = {}
    for (account_id, symbol), row in holdings.items():
        if symbol not in levels or symbol in urgent:
            continue
        signal = signals.get(symbol)
        target, advised, why = advise(
            symbol,
            float(row.position_quantity),
            levels[symbol],
            invested[account_id],
            signal.model_rank if signal else None,
            signal.buy_k if signal else None,
            rules.limit_ratio(symbol, names.get(symbol)),
            counts[account_id],
        )
        if advised > 0:
            advised = min(advised, int(_sellable(row, same_day=same_day) // LOT) * LOT)
        name = (row.symbol_name or names.get(symbol) or symbol).strip()
        advice[(account_id, symbol)] = SizeAdvice(account_id, symbol, name, float(row.position_quantity), target, advised, why)
    return advice


def enforceable_sizes(db: Session, owner_user_id: int, source: str, plan_date: str, now: datetime) -> dict[tuple[int, str], SizeAdvice]:
    """保存计划时要核对的建议。出错返回空：核对不了就不核对。"""
    if source not in quant_orders.QUANT_SOURCES:
        return {}
    try:
        return size_advice(db, owner_user_id, plan_date, now)
    except Exception as exc:  # noqa: BLE001 - 股数建议只是约束，出错不能挡住计划
        logger.warning("量化股数计算失败，这次不核对: %s", str(exc)[:200])
        return {}


def deviates(advised: int, sells: float, buys: float) -> bool:
    tolerance = max(LOT, abs(advised) * TOLERANCE)
    if advised < 0:
        return sells > 0 or buys > -advised + tolerance
    return buys > 0 or abs(sells - advised) > tolerance


def apply_sizes(items: list[dict[str, Any]], advice: dict[tuple[int, str], SizeAdvice], *, require_notes: bool) -> list[str]:
    """把建议记到每条计划上，核对改动。和建议一致的条目不留理由；改了没写理由的，主运行退回，补存时记下来。"""
    by_key: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for item in items:
        by_key.setdefault((item["account_id"], item["symbol"]), []).append(item)
    issues: list[str] = []
    for key, adv in advice.items():
        rows = by_key.get(key, [])
        sells = sum(row["quantity"] or 0 for row in rows if row["action"] in rules.SELL_ACTIONS)
        buys = sum(row["quantity"] or 0 for row in rows if row["action"] in rules.BUY_ACTIONS)
        for row in rows:
            row["advised_quantity"] = float(adv.advised)
        if not deviates(adv.advised, sells, buys):
            for row in rows:
                row["size_note"] = None
            continue
        if any(row.get("size_note") for row in rows):
            continue
        if not require_notes:
            if rows:
                rows[0]["size_note"] = NO_REASON
            continue
        planned = f"卖 {rules.format_quantity(sells)} 股" if sells else f"买 {rules.format_quantity(buys)} 股" if buys else "不动"
        where = "在这一条的 size_note 写明为什么" if rows else "存一条 action 为 hold 的并在 size_note 写明为什么"
        issues.append(f"账户 {key[0]} {adv.name}：代码建议{rules.describe_advice(adv.advised)}，计划是{planned}。要么改成代码的数，要么{where}")
    return issues


def size_block(db: Session, owner_user_id: int, plan_date: str, now: datetime) -> str:
    """写进出计划任务提示的一段：每个账户每只持仓的目标股数和建议。算不出来返回空。"""
    try:
        advice = size_advice(db, owner_user_id, plan_date, now)
        if not advice:
            return ""
        labels = dict(db.execute(select(PositionAccount.id, PositionAccount.broker_name).where(PositionAccount.id.in_({key[0] for key in advice}))).all())
    except Exception as exc:  # noqa: BLE001 - 股数建议只是提示，出错不能耽误报告
        logger.warning("量化股数计算失败，这次不写进提示: %s", str(exc)[:200])
        return ""
    lines: list[str] = []
    for account_id in sorted({key[0] for key in advice}):
        lines.append(f"账户 {account_id}（{labels.get(account_id) or ''}）：")
        for (_, symbol), adv in sorted((key, value) for key, value in advice.items() if key[0] == account_id):
            text = adv.why or rules.describe_advice(adv.advised)
            lines.append(f"- {adv.name}（{symbol.split('.')[0]}）｜持 {rules.format_quantity(adv.shares)} 股，目标约 {adv.target:,.0f} 股｜{text}")
    return (
        "量化股数（代码按风险预算和模型排名算的，持股不到 8 只的账户已放宽，账户现金和 ETF 不算进投入资金）：\n"
        + "\n".join(lines)
        + "\n规则：股数默认就用这里的数。要改（多卖、少卖、该卖不卖、不该卖却卖、补得比这里多）就在那一条的 size_note 写明为什么，"
        "没写存不进去，拿着不动也算一条（action 用 hold）。可以补的只用之前卖出的钱，不动账户原有现金。"
        "有重大消息的股票不在这里，股数按需要定。"
    )
