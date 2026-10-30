"""Scoped portfolio evidence and bookkeeping exposed through the MCP gateway."""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.core.models import PositionAccount
from backend.app.data.repository import StockRepository
from backend.app.data.user_scope import data_user_scope
from backend.app.market import digital_human_tools, stock_tools
from backend.app.mcp_gateway.mutations import run_idempotent_mutation


def _user_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("必须提供认证用户")
    return value


def _account_id(value: int | None) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
        raise ValueError("account_id 必须是正整数")
    return value


def _positive_number(value: float, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是大于零的有限数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是大于零的有限数字") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field} 必须是大于零的有限数字")
    return number


def _symbol(value: str) -> str:
    symbol = stock_tools.normalize_a_share_symbol(str(value or ""))
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
        raise ValueError("请提供明确的 A 股代码，例如 600036.SH；名称请先解析为证券代码")
    return symbol


def _require_account(db, user_id: int, account_id: int | None, *, lock: bool = False):
    if account_id is None:
        return None
    query = select(PositionAccount).where(
        PositionAccount.id == account_id,
        PositionAccount.owner_user_id == user_id,
        PositionAccount.status != "archived",
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    account = db.scalar(query)
    if account is None:
        raise ValueError("证券账户不存在、已归档或不属于当前用户")
    return account


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def list_position_accounts(user_id: int) -> dict[str, Any]:
    """List the authenticated user's accounts without recomputing or writing balances."""
    user_id = _user_id(user_id)
    with SessionLocal() as db:
        repo = StockRepository(db).for_user(user_id)
        accounts = db.scalars(select(PositionAccount).where(
            PositionAccount.owner_user_id == user_id,
            PositionAccount.status != "archived",
        ).order_by(PositionAccount.sort_order, PositionAccount.id)).all()
        return {"accounts": [repo._account_out(row) for row in accounts], "read_at": _now(),
                "note": "资金为系统已记录快照；read_at 是读取时间，不代表行情更新时间。"}


def get_positions(user_id: int, account_id: int | None = None, symbol: str | None = None) -> dict[str, Any]:
    """Read current holdings; never fall back to another user's account."""
    user_id, account_id = _user_id(user_id), _account_id(account_id)
    normalized = _symbol(symbol) if symbol is not None else None
    with SessionLocal() as db:
        _require_account(db, user_id, account_id)
        rows = StockRepository(db).for_user(user_id).positions(account_id)
    if normalized:
        rows = [row for row in rows if row["symbol"] == normalized]
    return {"positions": rows, "count": len(rows), "account_id": account_id,
            "symbol": normalized, "read_at": _now(), "source": "stored_position_snapshots",
            "freshness": "持仓与价格为已保存快照，记录更新时间不等于行情日期；分析单股时应再获取行情证据。"}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def analyze_portfolio(user_id: int, account_id: int | None = None) -> dict[str, Any]:
    """Return portfolio concentration, missing evidence and conditional review guidance."""
    snapshot = get_positions(user_id, account_id)
    rows = snapshot["positions"]
    known_values = [_finite(row.get("market_value")) for row in rows]
    known_total = sum(value for value in known_values if value is not None)
    complete_values = bool(rows) and all(value is not None and value >= 0 for value in known_values)
    symbol_values: dict[str, float] = {}
    gaps: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    for row, value in zip(rows, known_values, strict=True):
        identity = {"symbol": row["symbol"], "account_id": row.get("account_id")}
        if value is not None:
            symbol_values[row["symbol"]] = symbol_values.get(row["symbol"], 0.0) + value
        if value is None or value < 0:
            gaps.append({**identity, "reason": "持仓市值缺失或异常，不能完整计算组合权重"})
        if row.get("stop_price") is None:
            flags.append({**identity, "reason": "未记录止损参考价"})
        if not row.get("thesis"):
            flags.append({**identity, "reason": "未记录持有理由"})
    weights = [{"symbol": symbol, "market_value": round(value, 2),
                "weight_pct": round(value / known_total * 100, 2)}
               for symbol, value in sorted(symbol_values.items(), key=lambda item: -item[1])
               if complete_values and known_total > 0]
    gaps.append({"reason": "组合市值来自持仓快照，未逐股刷新行情；需核对行情日期后形成交易观点"})
    return {**snapshot,
            "summary": {"position_count": len(rows), "symbol_count": len({row["symbol"] for row in rows}),
                        "known_market_value": round(known_total, 2), "valuation_complete": complete_values,
                        "weights": weights, "weight_basis": "仅持仓市值，不含现金"},
            "risk_flags": flags, "data_gaps": gaps,
            "decision_guidance": {"status": "evidence_only", "action": None,
                "instructions": ["核对持仓快照和最新行情日期，缺失价格不可按零估值。",
                                 "结合单股权重、持有理由与已记录的止损价评估集中风险。",
                                 "对重点持仓调用 analyze_position，分别说明继续持有和减仓的依据及触发条件。",
                                 "不因单一盈亏数字直接给出清仓或继续持有结论。"]}}


def analyze_position(
    user_id: int,
    symbol: str,
    account_id: int | None = None,
    quantity: float | None = None,
    cost_price: float | None = None,
) -> dict[str, Any]:
    """Gather decision evidence, preserving stale data, missing inputs and ambiguity."""
    user_id, account_id = _user_id(user_id), _account_id(account_id)
    normalized = _symbol(symbol)
    if quantity is not None:
        quantity = _positive_number(quantity, "quantity")
    if cost_price is not None:
        cost_price = _positive_number(cost_price, "cost_price")
    stored_positions = get_positions(user_id, account_id, normalized)["positions"]
    plan_fields = ("account_id", "symbol", "name", "quantity", "cost_price", "stop_price",
                   "take_profit_price", "thesis", "risk_note", "as_of_date", "updated_at")
    position_plans = [{key: row.get(key) for key in plan_fields} for row in stored_positions]
    with data_user_scope(user_id):
        evidence = digital_human_tools.get_position_decision_context(
            normalized, account_id=account_id, quantity=quantity, cost_price=cost_price,
        )
    market = evidence.get("market") or {}
    gaps = list((evidence.get("research") or {}).get("errors") or [])
    technical = evidence.get("technical") or {}
    if technical.get("error"):
        gaps.append({"dataset": "technical", "error": str(technical["error"])})
    elif (_finite(technical.get("bar_count")) or 0) <= 0:
        gaps.append({"dataset": "technical", "error": "缺少可用的技术分析行情数据"})
    blockers = []
    if evidence.get("error"):
        blockers.append(str(evidence["error"]))
    account_ambiguous = bool(evidence.get("account_ambiguous") or (account_id is None and len(position_plans) > 1))
    if account_ambiguous:
        blockers.append("同一股票存在多个账户持仓，请指定 account_id 后评估成本和仓位")
    if market.get("stale") or market.get("intraday_unavailable"):
        blockers.append(market.get("freshness_note") or "行情不是当前应有交易日数据")
    if (_finite(market.get("price")) or 0) <= 0 or not market.get("trade_date"):
        blockers.append("缺少可核对日期的有效价格")
    try:
        trade_day = datetime.strptime(str(market.get("trade_date") or "").replace("-", ""), "%Y%m%d").date()
        expected_day = datetime.strptime(str(market.get("expected_trade_date") or "").replace("-", ""), "%Y%m%d").date()
    except ValueError:
        blockers.append("缺少可核验的行情日期或应有交易日，无法确认行情新鲜度")
    else:
        if trade_day < expected_day and not (market.get("stale") or market.get("intraday_unavailable")):
            blockers.append("行情日期早于应有交易日，不能作为当前行情作出判断")
    position = evidence.get("position") or {}
    if position.get("quantity") is None or position.get("cost_price") is None:
        blockers.append("缺少明确的持仓数量或成本，不能计算个人持仓盈亏")
    return {**evidence, "data_gaps": gaps, "account_ambiguous": account_ambiguous,
            "position_plans": position_plans,
            "position_plan": position_plans[0] if len(position_plans) == 1 and not account_ambiguous else None,
            "decision_guidance": {"status": "needs_data" if blockers else "evidence_only",
                "action": None, "blockers": blockers,
                "instructions": ["这是结构化研究证据，尚未生成买卖结论。",
                                 "先写明标的、账户、行情交易日与数据缺口。",
                                 "分别列出继续持有、减仓/退出的证据及反证，引用本次证据中的数值。",
                                 "给出支持/压力位等条件与下一次复核条件；不得把技术价位当收益保证。",
                                 "有数据阻断或账户歧义时先澄清，不输出确定性的卖出或持有指令。"]}}


def add_position(
    user_id: int,
    account_id: int,
    symbol: str,
    quantity: float,
    execution_price: float,
    idempotency_key: str,
) -> dict[str, Any]:
    """Record a completed buy/add operation, not a broker order or holding replacement."""
    user_id, account_id = _user_id(user_id), _account_id(account_id)
    if account_id is None:
        raise ValueError("新增持仓必须指定证券账户 account_id")
    normalized = _symbol(symbol)
    quantity = _positive_number(quantity, "quantity")
    price = _positive_number(execution_price, "execution_price")
    if not math.isfinite(quantity * price):
        raise ValueError("交易金额超出有效数字范围")
    payload = {"account_id": account_id, "symbol": normalized, "quantity": quantity, "execution_price": price}
    with SessionLocal() as db:
        def operation() -> dict[str, Any]:
            _require_account(db, user_id, account_id, lock=True)
            catalog = stock_tools.lookup_a_share_instrument(normalized)
            if not catalog.get("available"):
                raise ValueError("证券主数据暂不可用，无法验证代码，请稍后重试")
            instrument = catalog.get("instrument") or {}
            if not instrument or instrument.get("ts_code") != normalized:
                raise ValueError("证券代码未匹配到 A 股主数据，未写入持仓")
            saved = StockRepository(db).for_user(user_id).merge_position_holding(
                {"account_id": account_id, "symbol": normalized, "name": instrument.get("name"),
                 "position_quantity": quantity, "cost_price": price, "execution_price": price},
                action="buy", source_type="mcp", commit=False,
            )
            if saved.get("error"):
                raise ValueError(str(saved["error"]))
            return {"recorded": True, "broker_order_placed": False, "action": "add",
                    "added_quantity": quantity, "execution_price": price,
                    "position": saved, "idempotency_key": idempotency_key,
                    "note": "已记录这次买入/加仓并更新持仓账本；未向券商下单。"}
        return run_idempotent_mutation(db, user_id, "add_position", idempotency_key, payload, operation)
