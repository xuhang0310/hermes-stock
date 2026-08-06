"""模拟盘服务：虚拟账户、市价买卖、持仓与盈亏计算（精简闭环）。

单一职责：只负责模拟盘的记账与成交逻辑——不外呼行情（只读 stock_latest_prices
缓存）、不触碰真实持仓域、不产生任何真实交易接口。

设计要点：
- 与真实持仓完全隔离：独立的 paper_accounts / paper_positions / paper_orders 三表，
  symbol/user_id 都不建外键，删用户/删标的不会牵连历史流水。
- 市价单即时成交：成交价取 stock_latest_prices 缓存；无缓存直接拒绝（不拉外盘，
  避免模拟盘引入新的外部依赖与延迟）。
- 买入按 A 股一手=100 股整数倍校验；单边手续费 FEE_RATE。
- 账户行 ``with_for_update()`` 行锁，防止并发下单双花现金。
- 账户 get_or_create 幂等：并发首访只会创建一个账户（唯一约束 + IntegrityError 回读）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    PaperAccount,
    PaperOrder,
    PaperPosition,
    StockLatestPrice,
)
from backend.app.data._helpers import normalize_a_share_symbol

DEFAULT_INITIAL_CASH = 1_000_000.0
FEE_RATE = 0.00025  # 单边万 2.5，无最低佣金（模拟从简）
_LOT_SIZE = 100
_ORDER_LIMIT_MAX = 200


def _account_dict(account: PaperAccount) -> dict:
    return {
        "user_id": account.user_id,
        "initial_cash": round(account.initial_cash, 2),
        "cash": round(account.cash, 2),
        "created_at": account.created_at.isoformat() if account.created_at else None,
    }


def _position_dict(row: PaperPosition, price: float | None, name: str | None) -> dict:
    market_price = price if price is not None else row.avg_cost
    market_value = market_price * row.quantity
    pnl = (market_price - row.avg_cost) * row.quantity
    return {
        "symbol": row.symbol,
        "name": name,
        "quantity": row.quantity,
        "avg_cost": round(row.avg_cost, 4),
        "price": round(market_price, 4),
        "price_from_cache": price is not None,
        "market_value": round(market_value, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / (row.avg_cost * row.quantity) * 100, 2) if row.avg_cost else None,
    }


def get_or_create_account(db, user_id: int, initial_cash: float = DEFAULT_INITIAL_CASH) -> PaperAccount:
    """幂等获取账户；不存在则创建（并发竞争时靠唯一约束兜底回读）。"""
    account = db.scalar(select(PaperAccount).where(PaperAccount.user_id == user_id))
    if account is not None:
        return account
    db.add(PaperAccount(user_id=user_id, initial_cash=initial_cash, cash=initial_cash))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return db.scalar(select(PaperAccount).where(PaperAccount.user_id == user_id))


def account_summary(user_id: int) -> dict:
    """账户概览：现金 + 持仓市值 + 总收益。"""
    with SessionLocal() as db:
        account = get_or_create_account(db, user_id)
        rows = db.execute(
            select(PaperPosition, StockLatestPrice.price, StockLatestPrice.name)
            .outerjoin(StockLatestPrice, StockLatestPrice.symbol == PaperPosition.symbol)
            .where(PaperPosition.user_id == user_id)
            .order_by(PaperPosition.id)
        ).all()
        positions = [_position_dict(row, price, name) for row, price, name in rows]
        market_value = sum(item["market_value"] for item in positions)
        total_assets = account.cash + market_value
        return {
            "account": _account_dict(account),
            "positions": positions,
            "market_value": round(market_value, 2),
            "total_assets": round(total_assets, 2),
            "total_pnl": round(total_assets - account.initial_cash, 2),
        }


def _latest_price(db, symbol: str) -> float | None:
    row = db.scalar(select(StockLatestPrice).where(StockLatestPrice.symbol == symbol))
    return row.price if row is not None else None


def place_order(user_id: int, symbol: str, side: str, quantity: int) -> dict:
    """市价买入/卖出；校验失败抛 ValueError（中文信息直接给调用方）。"""
    symbol = normalize_a_share_symbol(symbol)
    side = (side or "").strip().lower()
    if side not in ("buy", "sell"):
        raise ValueError("交易方向只能是 buy 或 sell")
    quantity = int(quantity or 0)
    if quantity <= 0:
        raise ValueError("成交数量必须大于 0")
    if side == "buy" and quantity % _LOT_SIZE != 0:
        raise ValueError(f"买入数量必须是 {_LOT_SIZE} 股（一手）的整数倍")

    with SessionLocal() as db:
        account = get_or_create_account(db, user_id)
        account = db.scalar(
            select(PaperAccount).where(PaperAccount.id == account.id).with_for_update()
        )
        price = _latest_price(db, symbol)
        if price is None or price <= 0:
            raise ValueError("该标的暂无最新价缓存，请先在问股中查询该股票后再模拟交易")

        name = db.scalar(select(StockLatestPrice.name).where(StockLatestPrice.symbol == symbol))
        amount = round(price * quantity, 2)
        fee = round(amount * FEE_RATE, 2)
        realized_pnl: float | None = None

        if side == "buy":
            if amount + fee > account.cash + 1e-6:
                raise ValueError(
                    f"模拟账户现金不足：需要 {amount + fee:.2f}，可用 {account.cash:.2f}"
                )
            account.cash = round(account.cash - amount - fee, 2)
            position = db.scalar(
                select(PaperPosition)
                .where(PaperPosition.user_id == user_id, PaperPosition.symbol == symbol)
                .with_for_update()
            )
            if position is None:
                db.add(PaperPosition(user_id=user_id, symbol=symbol, quantity=quantity, avg_cost=price))
            else:
                total_cost = position.avg_cost * position.quantity + amount
                position.quantity += quantity
                position.avg_cost = total_cost / position.quantity
        else:
            position = db.scalar(
                select(PaperPosition)
                .where(PaperPosition.user_id == user_id, PaperPosition.symbol == symbol)
                .with_for_update()
            )
            if position is None or position.quantity < quantity:
                held = position.quantity if position else 0
                raise ValueError(f"可卖数量不足：持有 {held} 股，尝试卖出 {quantity} 股")
            realized_pnl = round((price - position.avg_cost) * quantity - fee, 2)
            position.quantity -= quantity
            if position.quantity == 0:
                db.delete(position)
            account.cash = round(account.cash + amount - fee, 2)

        order = PaperOrder(
            user_id=user_id,
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            amount=amount,
            fee=fee,
            realized_pnl=realized_pnl,
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        return {
            "order": {
                "id": order.id,
                "symbol": order.symbol,
                "name": name,
                "side": order.side,
                "price": order.price,
                "quantity": order.quantity,
                "amount": order.amount,
                "fee": order.fee,
                "realized_pnl": order.realized_pnl,
                "created_at": order.created_at.isoformat() if order.created_at else None,
            }
        }


def list_orders(user_id: int, limit: int = 50) -> list[dict]:
    """成交流水（倒序）。"""
    limit = max(1, min(int(limit or 50), _ORDER_LIMIT_MAX))
    with SessionLocal() as db:
        rows = db.execute(
            select(PaperOrder, StockLatestPrice.name)
            .outerjoin(StockLatestPrice, StockLatestPrice.symbol == PaperOrder.symbol)
            .where(PaperOrder.user_id == user_id)
            .order_by(PaperOrder.id.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": order.id,
                "symbol": order.symbol,
                "name": name,
                "side": order.side,
                "price": order.price,
                "quantity": order.quantity,
                "amount": order.amount,
                "fee": order.fee,
                "realized_pnl": order.realized_pnl,
                "created_at": order.created_at.isoformat() if order.created_at else None,
            }
            for order, name in rows
        ]


def reset_account(user_id: int, initial_cash: float = DEFAULT_INITIAL_CASH) -> dict:
    """重置模拟账户：清空持仓与流水、现金回到初始值（幂等，可重复调用）。"""
    with SessionLocal() as db:
        account = get_or_create_account(db, user_id, initial_cash=initial_cash)
        db.query(PaperPosition).filter(PaperPosition.user_id == user_id).delete(
            synchronize_session=False
        )
        db.query(PaperOrder).filter(PaperOrder.user_id == user_id).delete(
            synchronize_session=False
        )
        account.initial_cash = initial_cash
        account.cash = initial_cash
        db.commit()
        return _account_dict(account)
