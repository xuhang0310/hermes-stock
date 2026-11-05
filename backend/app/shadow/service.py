"""影子账户：真实账户开始时的持仓和现金复制一份，之后完全由代码挂单，和真实账户比净值。

没有任何真实交易通道：成交是按当天 K 线模拟的。每个交易日收盘数据入库后跑一轮（调度器 shadow_settle）：

1. 第一次遇到某个账户时，复制它当前的持仓和现金。
2. 结算上一轮挂的单：碰到价才成交，一字板算没成交，当天没成交的单作废。
3. 除权除息调整持股。
4. 记两边净值。
5. 算下一个交易日要挂的单。

同一天重跑不会重复，所以调度器可以每 30 分钟试一次，等到收盘数据为止。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.applog import get_logger
from backend.app.core.database import SessionLocal
from backend.app.core.models import DailyBar, Instrument, MarketTradingCalendar, PositionAccount, ShadowAccount, ShadowNav, ShadowOrder, ShadowPosition, StockLatestPrice
from backend.app.plans.rules import limit_ratio
from backend.app.plans.service import open_holdings
from backend.app.quant.order_engine import load_table, plan_orders
from backend.app.quant.tech_signal_service import load_bars
from backend.app.shadow import rules
from backend.app.shadow.rank_signal import latest_ranks

logger = get_logger("shadow_account")

PENDING, FILLED, EXPIRED = "pending", "filled", "expired"
RISK, RISK_RANK = "risk", "risk_rank"
ACTIVE_STRATEGIES = (RISK, RISK_RANK)
STRATEGY_LABELS = {RISK: "影子 A", RISK_RANK: "影子 B"}
SHARE_SOURCE = "shadow_compare"


def _day_bars(db: Session, symbols: set[str], trade_date: str) -> dict[str, Any]:
    if not symbols:
        return {}
    rows = db.execute(
        select(DailyBar.symbol, DailyBar.open, DailyBar.high, DailyBar.low, DailyBar.close, DailyBar.pre_close, DailyBar.source).where(
            DailyBar.symbol.in_(symbols), DailyBar.trade_date == trade_date, DailyBar.close.is_not(None)
        )
    ).all()
    chosen: dict[str, Any] = {}
    for row in rows:
        if row.symbol not in chosen or row.source == "tushare":
            chosen[row.symbol] = row
    return chosen


def ensure_accounts(db: Session, trade_date: str) -> int:
    """给还没有影子账户、又有持仓的真实账户开一个，内容是此刻的持仓和现金。"""
    created = 0
    accounts = db.scalars(select(PositionAccount).where(PositionAccount.status == "active", PositionAccount.owner_user_id.is_not(None))).all()
    existing = {(row.real_account_id, row.strategy) for row in db.execute(select(ShadowAccount.real_account_id, ShadowAccount.strategy))}
    for account in accounts:
        missing = [strategy for strategy in ACTIVE_STRATEGIES if (account.id, strategy) not in existing]
        if not missing:
            continue
        holdings = {symbol: row for (account_id, symbol), row in open_holdings(db, account.owner_user_id).items() if account_id == account.id}
        if not holdings:
            continue
        for strategy in missing:
            shadow = ShadowAccount(
                owner_user_id=account.owner_user_id,
                real_account_id=account.id,
                strategy=strategy,
                account_label=f"{account.broker_name} 账户{account.id}",
                started_on=trade_date,
                idle_cash=float(account.available_cash or 0),
                pool=0.0,
            )
            db.add(shadow)
            db.flush()
            for symbol, row in holdings.items():
                db.add(ShadowPosition(shadow_account_id=shadow.id, owner_user_id=account.owner_user_id, symbol=symbol, quantity=float(row.position_quantity)))
            created += 1
    db.flush()
    return created


def _positions(db: Session, account: ShadowAccount) -> dict[str, ShadowPosition]:
    return {row.symbol: row for row in db.scalars(select(ShadowPosition).where(ShadowPosition.shadow_account_id == account.id)).all()}


def settle_orders(
    db: Session, account: ShadowAccount, trade_date: str, bars: dict[str, Any], positions: dict[str, ShadowPosition],
) -> int:
    """结算之前挂的单。每张单只活一个交易日。先卖后买，当天卖出的钱当天可以买回。"""
    orders = db.scalars(
        select(ShadowOrder).where(ShadowOrder.shadow_account_id == account.id, ShadowOrder.status == PENDING, ShadowOrder.basis_date < trade_date)
    ).all()
    previous_trade_date = db.scalar(
        select(MarketTradingCalendar.pretrade_date)
        .where(MarketTradingCalendar.exchange == "SSE", MarketTradingCalendar.cal_date == trade_date)
    ) if orders else None
    filled = 0
    for order in sorted(orders, key=lambda item: item.side != "sell"):
        bar = bars.get(order.symbol)
        price = None
        still_valid = previous_trade_date is None or order.basis_date == previous_trade_date
        if still_valid and bar is not None and None not in (bar.open, bar.high, bar.low):
            price = rules.fill_price(order.side, order.price, bar.open, bar.high, bar.low)
        order.settled_on = trade_date
        if price is None:
            order.status = EXPIRED
            continue
        position = positions.get(order.symbol)
        if order.side == "sell":
            if position is None or position.quantity < order.quantity:
                order.status = EXPIRED
                continue
            position.quantity -= order.quantity
            account.pool += rules.sell_proceeds(order.quantity, price)
        else:
            cost = rules.buy_cost(order.quantity, price)
            if cost > account.pool:
                order.status = EXPIRED
                continue
            account.pool -= cost
            if position is None:
                position = ShadowPosition(shadow_account_id=account.id, owner_user_id=account.owner_user_id, symbol=order.symbol, quantity=0.0)
                db.add(position)
                positions[order.symbol] = position
            position.quantity += order.quantity
        order.status = FILLED
        order.fill_price = price
        filled += 1
    return filled


def apply_ex_rights(positions: dict[str, ShadowPosition], bars: dict[str, Any], previous_close: dict[str, float]) -> None:
    for symbol, position in positions.items():
        bar = bars.get(symbol)
        if bar is not None:
            position.quantity *= rules.ex_rights_ratio(previous_close.get(symbol), bar.pre_close)


def _quoted_prices(db: Session, symbols: set[str]) -> dict[str, float]:
    """ETF 这类不在全市场 K 线里的持仓用最新价估值。规则管不到它们：不挂单，也不算进风险预算的基数。"""
    if not symbols:
        return {}
    rows = db.execute(select(StockLatestPrice.symbol, StockLatestPrice.price).where(StockLatestPrice.symbol.in_(symbols), StockLatestPrice.price > 0)).all()
    return {row.symbol: row.price for row in rows}


def _full_day_suspensions(trade_date: str) -> set[str]:
    from backend.app.market.stock_tools import _tushare_request

    result = _tushare_request(
        "suspend_d", {"trade_date": trade_date, "suspend_type": "S"},
        "ts_code,trade_date,suspend_type,suspend_timing",
    )
    return {
        row["ts_code"] for row in result.get("rows", [])
        if row.get("trade_date") == trade_date and row.get("suspend_type") == "S"
        and "suspend_timing" in row and not row["suspend_timing"] and row.get("ts_code")
    }


def _market_value(quantities: dict[str, float], closes: dict[str, float]) -> float | None:
    """有任何一只取不到价格就返回 None，不拿缺数据的净值去比较。"""
    total = 0.0
    for symbol, quantity in quantities.items():
        close = closes.get(symbol)
        if close is None:
            return None
        total += quantity * close
    return total


def create_orders(db: Session, account: ShadowAccount, trade_date: str, bars_by_symbol: dict[str, list], positions: dict[str, ShadowPosition], closes: dict[str, float], names: dict[str, str], table: dict[str, Any], ranks: dict[str, float]) -> int:
    invested = account.pool + sum(position.quantity * closes.get(symbol, 0.0) for symbol, position in positions.items())
    created = 0
    pool_left = account.pool
    for symbol, position in sorted(positions.items()):
        series = bars_by_symbol.get(symbol)
        if not series or series[-1].trade_date != trade_date:
            continue  # 今天停牌，明天不挂
        levels = plan_orders(series, table, limit_ratio=limit_ratio(symbol, names.get(symbol)))
        if levels is None:
            continue
        tilt = rules.rank_tilt(ranks.get(symbol)) if account.strategy == RISK_RANK else 1.0
        planned = rules.plan_order(symbol, position.quantity, levels, invested, pool_left, tilt=tilt)
        if planned is None:
            continue
        if planned.side == "buy":
            pool_left -= rules.buy_cost(planned.quantity, planned.price)
        db.add(
            ShadowOrder(
                shadow_account_id=account.id,
                owner_user_id=account.owner_user_id,
                basis_date=trade_date,
                symbol=symbol,
                stock_name=(names.get(symbol) or symbol).strip(),
                side=planned.side,
                price=planned.price,
                quantity=planned.quantity,
                basis=planned.basis,
                status=PENDING,
            )
        )
        created += 1
    return created


def _real_snapshot(db: Session, account: ShadowAccount) -> tuple[dict[str, float], float]:
    holdings = {
        symbol: float(row.position_quantity)
        for (account_id, symbol), row in open_holdings(db, account.owner_user_id).items()
        if account_id == account.real_account_id
    }
    real = db.get(PositionAccount, account.real_account_id)
    return holdings, float((real.available_cash if real is not None else 0) or 0)


def run_account(db: Session, account: ShadowAccount, trade_date: str, table: dict[str, Any], ranks: dict[str, float] | None = None) -> dict[str, Any] | None:
    """跑一个账户的一天。今天的收盘数据不全、或者已经跑过就返回 None。"""
    done = db.scalar(select(ShadowNav.id).where(ShadowNav.shadow_account_id == account.id, ShadowNav.trade_date == trade_date))
    if done is not None:
        return None
    positions = _positions(db, account)
    real_quantities, real_cash = _real_snapshot(db, account)
    pending = set(db.scalars(select(ShadowOrder.symbol).where(ShadowOrder.shadow_account_id == account.id, ShadowOrder.status == PENDING)).all())
    symbols = set(positions) | set(real_quantities) | pending
    bars_by_symbol = load_bars(db, sorted(symbols), trade_date)
    if not any(series and series[-1].trade_date == trade_date for series in bars_by_symbol.values()):
        return None  # 今天的数据还没到
    today = _day_bars(db, symbols, trade_date)
    instruments = db.execute(select(Instrument.symbol, Instrument.name, Instrument.type).where(Instrument.symbol.in_(symbols))).all()
    stock_symbols = {symbol for symbol, _, kind in instruments if kind == "stock"}
    complete = {
        symbol for symbol, bar in today.items()
        if all(price is not None and price > 0 for price in (bar.open, bar.high, bar.low, bar.close))
    }
    missing = (stock_symbols | pending) - complete
    if missing and not missing.issubset(_full_day_suspensions(trade_date)):
        return None  # 等数据补齐再结算，保留待处理订单
    closes = {symbol: series[-1].close for symbol, series in bars_by_symbol.items() if series}
    previous = {symbol: series[-2].close for symbol, series in bars_by_symbol.items() if len(series) >= 2 and series[-1].trade_date == trade_date}
    apply_ex_rights(positions, today, previous)
    filled = settle_orders(db, account, trade_date, today, positions)
    marks = {**_quoted_prices(db, symbols - set(closes)), **closes}
    shadow_stock = _market_value({symbol: position.quantity for symbol, position in positions.items()}, marks)
    real_stock = _market_value(real_quantities, marks)
    if shadow_stock is None or real_stock is None:
        logger.warning("影子账户 %s 在 %s 缺价格，今天不记净值", account.id, trade_date)
        db.rollback()
        return None
    db.add(
        ShadowNav(
            shadow_account_id=account.id,
            owner_user_id=account.owner_user_id,
            trade_date=trade_date,
            shadow_value=shadow_stock + account.pool + account.idle_cash,
            real_value=real_stock + real_cash,
        )
    )
    names = {symbol: name for symbol, name, _ in instruments}
    created = create_orders(db, account, trade_date, bars_by_symbol, positions, closes, names, table, ranks or {})
    return {"filled": filled, "created": created}


def nav_series(db: Session, account_id: int) -> list[ShadowNav]:
    return list(db.scalars(select(ShadowNav).where(ShadowNav.shadow_account_id == account_id).order_by(ShadowNav.trade_date)).all())


def compare(navs: dict[str, list[ShadowNav]]) -> dict[str, Any]:
    """按策略对齐到共同的交易日。A、B 开始日不同时，从晚开始的那天算起才公平。"""
    days_by_strategy = [{row.trade_date for row in rows} for rows in navs.values()]
    if not days_by_strategy:
        return {"days": [], "series": {}, "real": []}
    days = sorted(set.intersection(*days_by_strategy))
    series = {}
    real: list[float] = []
    for strategy, rows in navs.items():
        by_day = {row.trade_date: row for row in rows}
        series[strategy] = [by_day[day].shadow_value for day in days]
        if not real:
            real = [by_day[day].real_value for day in days]
    return {"days": days, "series": series, "real": real}


def owner_summary(db: Session, owner: int) -> dict[str, Any]:
    """这位用户所有账户合起来的对比。"""
    accounts = list(db.scalars(select(ShadowAccount).where(ShadowAccount.owner_user_id == owner).order_by(ShadowAccount.id)).all())
    by_strategy: dict[str, dict[str, SimpleNamespace]] = {}
    counts: dict[str, int] = {}
    for account in accounts:
        counts[account.strategy] = counts.get(account.strategy, 0) + 1
        bucket = by_strategy.setdefault(account.strategy, {})
        for row in nav_series(db, account.id):
            day = bucket.setdefault(row.trade_date, SimpleNamespace(trade_date=row.trade_date, shadow_value=0.0, real_value=0.0, n=0))
            day.shadow_value += row.shadow_value
            day.real_value += row.real_value
            day.n += 1
    navs = {
        strategy: [day for _, day in sorted(bucket.items()) if day.n == counts[strategy]]
        for strategy, bucket in by_strategy.items()
    }
    return {"accounts": accounts, **compare(navs)}


def _ranks_for_today(session_factory: Callable[[], Session], trade_date: str) -> dict[str, float]:
    """今天还有影子 B 没结算才算。这一步要读全市场 K 线，而任务每 30 分钟试一次。"""
    try:
        with session_factory() as db:
            settled = select(ShadowNav.shadow_account_id).where(ShadowNav.trade_date == trade_date)
            waiting = db.scalar(select(ShadowAccount.id).where(ShadowAccount.strategy == RISK_RANK, ShadowAccount.id.not_in(settled)).limit(1))
            return latest_ranks(db, trade_date) if waiting is not None else {}
    except Exception as exc:  # noqa: BLE001 - 排名失败只推迟影子 B 的结算
        logger.warning("模型排名计算失败: %s", str(exc)[:200])
        return {}


def run_shadow_day(now: datetime, *, session_factory: Callable[[], Session] = SessionLocal) -> str:
    """后台结算入口，now 是北京时间。对比页面由晚间日报提供。"""
    trade_date = now.strftime("%Y%m%d")
    table = load_table()
    if table is None:
        return "没有量化统计表，跳过"
    try:
        with session_factory() as db:
            ensure_accounts(db, trade_date)
            db.commit()
            ids = list(db.scalars(select(ShadowAccount.id)).all())
        ranks = _ranks_for_today(session_factory, trade_date)
        settled = 0
        filled = 0
        for account_id in ids:
            with session_factory() as db:
                account = db.get(ShadowAccount, account_id)
                if account.strategy == RISK_RANK and not ranks:
                    continue
                result = run_account(db, account, trade_date, table, ranks)
                if result is None:
                    continue
                db.commit()
                settled += 1
                filled += result["filled"]
        if not settled:
            return f"{trade_date} 没有新结算（数据没到，或者今天已经跑过）"
        return f"{trade_date} 结算完成：{settled} 个影子账户，模拟成交 {filled} 笔"
    except Exception as exc:  # noqa: BLE001 - 影子账户失败不能影响其他任务
        logger.error("影子账户运行失败: %s", str(exc)[:300])
        return f"影子账户运行失败: {str(exc)[:200]}"
