"""技术信号的存取：算哪些股票、从 daily_bars 取 K 线、结果落 tech_signals。

现状：规则没通过全市场验证，调度器不调用这里，tech_signals 表不写入。
同一交易日重算只会覆盖同一行，可以反复跑；过去的日期也能补算，只用当天及以前的 K 线。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.models import DailyBar, Position, PositionTrade, TechSignalRecord, WatchlistItem
from backend.app.data.position_write_mixin import PositionWriteMixin
from backend.app.quant.tech_signal import FLAT, LONG, RULE_VERSION, UNKNOWN, Bar, TechSignal, compute_signal

# 卖掉的股票还要继续算：规则转红时要知道该买回哪些
SOLD_LOOKBACK_DAYS = 90
_SELL_ACTIONS = ("sell", "reduce", "close")
# 约 280 根 K 线，足够 MACD 稳定；再长对结果几乎没有影响
_LOOKBACK_CALENDAR_DAYS = 420
_PREFERRED_SOURCE = "tushare"


def signal_universe(db: Session, *, now: datetime | None = None) -> list[str]:
    """所有用户的持仓、自选和近 90 天卖出过的股票。信号只和股票有关，不分用户。"""
    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    held = select(Position.symbol).where(Position.status == "open", Position.quantity > 0)
    watched = select(WatchlistItem.symbol).where(WatchlistItem.status == "active")
    sold = select(PositionTrade.symbol).where(
        PositionTrade.action.in_(_SELL_ACTIONS),
        PositionTrade.created_at >= current - timedelta(days=SOLD_LOOKBACK_DAYS),
        PositionTrade.source_type.notin_(PositionWriteMixin.NON_TRADE_SOURCE_TYPES),
    )
    symbols: set[str] = set()
    for query in (held, watched, sold):
        symbols.update(db.scalars(query).all())
    return sorted(symbols)


def load_bars(db: Session, symbols: list[str], trade_date: str) -> dict[str, list[Bar]]:
    """取每只股票到 trade_date 为止的 K 线，按日期升序；同一天多个来源只留一条。"""
    if not symbols:
        return {}
    start = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=_LOOKBACK_CALENDAR_DAYS)).strftime("%Y%m%d")
    rows = db.execute(
        select(
            DailyBar.symbol,
            DailyBar.trade_date,
            DailyBar.high,
            DailyBar.low,
            DailyBar.close,
            DailyBar.pre_close,
            DailyBar.source,
        ).where(
            DailyBar.symbol.in_(symbols),
            DailyBar.trade_date >= start,
            DailyBar.trade_date <= trade_date,
            DailyBar.close.is_not(None),
            DailyBar.close > 0,
        )
    ).all()
    chosen: dict[tuple[str, str], Any] = {}
    for row in rows:
        key = (row.symbol, row.trade_date)
        if key not in chosen or row.source == _PREFERRED_SOURCE:
            chosen[key] = row
    bars: dict[str, list[Bar]] = {}
    for (symbol, _), row in sorted(chosen.items()):
        bars.setdefault(symbol, []).append(
            Bar(
                trade_date=row.trade_date,
                high=row.high if row.high is not None else row.close,
                low=row.low if row.low is not None else row.close,
                close=row.close,
                pre_close=row.pre_close,
            )
        )
    return bars


def _store(db: Session, symbol: str, signal: TechSignal) -> None:
    row = db.scalar(
        select(TechSignalRecord).where(
            TechSignalRecord.symbol == symbol,
            TechSignalRecord.trade_date == signal.trade_date,
            TechSignalRecord.rule_version == RULE_VERSION,
        )
    )
    if row is None:
        row = TechSignalRecord(symbol=symbol, trade_date=signal.trade_date, rule_version=RULE_VERSION)
        db.add(row)
    for field in (
        "direction",
        "close",
        "bars_used",
        "flipped",
        "flip_date",
        "days_in_state",
        "flip_price",
        "dif",
        "dea",
        "macd_hist",
        "ma5",
        "ma10",
        "ma20",
        "ma60",
        "rsi14",
        "atr14",
    ):
        setattr(row, field, getattr(signal, field))


def refresh_signals(
    db: Session,
    *,
    trade_date: str,
    symbols: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """算一个交易日的信号并落库，返回摘要。这天没有 K 线的股票（停牌等）不写，列在 missing 里。"""
    target = str(trade_date).replace("-", "")
    universe = signal_universe(db, now=now) if symbols is None else sorted(set(symbols))
    bars_by_symbol = load_bars(db, universe, target)
    counts = {LONG: 0, FLAT: 0, UNKNOWN: 0}
    flipped: list[dict[str, str]] = []
    missing: list[str] = []
    for symbol in universe:
        bars = bars_by_symbol.get(symbol)
        if not bars or bars[-1].trade_date != target:
            missing.append(symbol)
            continue
        signal = compute_signal(bars)
        _store(db, symbol, signal)
        counts[signal.direction] += 1
        if signal.flipped:
            flipped.append({"symbol": symbol, "direction": signal.direction})
    db.commit()
    return {
        "trade_date": target,
        "rule_version": RULE_VERSION,
        "stored": sum(counts.values()),
        "long": counts[LONG],
        "flat": counts[FLAT],
        "unknown": counts[UNKNOWN],
        "flipped": flipped,
        "missing": missing,
    }
