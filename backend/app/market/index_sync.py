"""宽基指数日线同步：拉取沪深300（000300.SH）、中证1000（000852.SH）等宽基指数日线至 index_daily_bars。

供 L1 市场环境模块计算 60/200 日均线趋势与大小盘相对强弱，不依赖个股 instruments 外键。
增量幂等：按指数检查本地最新日期，仅拉取缺失区间；兼容 MySQL 与 SQLite（单测内存库）。
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.models import IndexDailyBar
from backend.app.market.stock_tools import _tushare_request

logger = logging.getLogger(__name__)

CORE_BENCHMARK_INDICES: tuple[str, ...] = (
    "000300.SH",  # 沪深300（大盘蓝筹温度计）
    "000852.SH",  # 中证1000（中小盘温度计）
    "000001.SH",  # 上证指数
    "399006.SZ",  # 创业板指
)
_INDEX_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount"
_DEFAULT_CALENDAR_LOOKBACK = 420  # 覆盖约 260+ 个交易日，满足 200 日均线计算
_API_SLEEP_SECONDS = 0.15


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip().replace("-", "")
    return raw if len(raw) == 8 and raw.isdigit() else ""


def upsert_index_bars(db: Session, rows: Sequence[dict[str, Any]], *, source: str = "tushare") -> int:
    """幂等写入指数日线（兼容 MySQL 与 SQLite）。"""
    valid: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or row.get("symbol") or "").strip().upper()
        trade_date = _compact_date(row.get("trade_date"))
        close = _finite_float(row.get("close"))
        if not symbol or not trade_date or close is None or close <= 0:
            continue
        valid[(symbol, trade_date)] = {
            "symbol": symbol,
            "trade_date": trade_date,
            "open": _finite_float(row.get("open")),
            "high": _finite_float(row.get("high")),
            "low": _finite_float(row.get("low")),
            "close": close,
            "pre_close": _finite_float(row.get("pre_close")),
            "pct_chg": _finite_float(row.get("pct_chg")),
            "vol": _finite_float(row.get("vol")),
            "amount": _finite_float(row.get("amount")),
            "source": source,
        }
    if not valid:
        return 0

    symbols = sorted({sym for sym, _ in valid})
    dates = sorted({day for _, day in valid})
    existing = {
        (item.symbol, item.trade_date): item
        for item in db.scalars(
            select(IndexDailyBar).where(
                IndexDailyBar.symbol.in_(symbols),
                IndexDailyBar.trade_date >= dates[0],
                IndexDailyBar.trade_date <= dates[-1],
            )
        ).all()
    }
    new_rows: list[dict[str, Any]] = []
    count = 0
    for key, payload in valid.items():
        current = existing.get(key)
        if current is None:
            new_rows.append(payload)
        else:
            for field in ("open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount", "source"):
                setattr(current, field, payload[field])
        count += 1
    for start in range(0, len(new_rows), 1000):
        db.execute(insert(IndexDailyBar), new_rows[start : start + 1000])
    return count


def sync_benchmark_indices(
    *,
    indices: Sequence[str] = CORE_BENCHMARK_INDICES,
    end_date: str | None = None,
    lookback_calendar_days: int = _DEFAULT_CALENDAR_LOOKBACK,
) -> dict[str, Any]:
    """增量同步核心宽基指数日线至本地表。"""
    target_end = _compact_date(end_date) or date.today().strftime("%Y%m%d")
    end_dt = datetime.strptime(target_end, "%Y%m%d").date()
    min_start = (end_dt - timedelta(days=max(60, int(lookback_calendar_days)))).strftime("%Y%m%d")

    upserted_total = 0
    per_index: dict[str, int] = {}
    errors: list[str] = []

    with SessionLocal() as db:
        local_bounds = {
            row.symbol: (row.min_date, row.max_date, int(row.cnt or 0))
            for row in db.execute(
                select(
                    IndexDailyBar.symbol,
                    func.min(IndexDailyBar.trade_date).label("min_date"),
                    func.max(IndexDailyBar.trade_date).label("max_date"),
                    func.count(IndexDailyBar.id).label("cnt"),
                )
                .where(IndexDailyBar.symbol.in_(list(indices)))
                .group_by(IndexDailyBar.symbol)
            ).all()
        }

    for idx_code in indices:
        symbol = str(idx_code).strip().upper()
        bounds = local_bounds.get(symbol)
        # 若本地历史不足 210 根或起点晚于目标回看窗口，则补齐完整长窗口；否则仅增量拉取最新缺口
        if bounds and bounds[2] >= 210 and bounds[1] and bounds[1] >= target_end:
            per_index[symbol] = 0
            continue
        start_date = min_start
        if bounds and bounds[2] >= 210 and bounds[1]:
            start_date = bounds[1]

        res = _tushare_request(
            "index_daily",
            {"ts_code": symbol, "start_date": start_date, "end_date": target_end},
            _INDEX_DAILY_FIELDS,
        )
        if "error" in res:
            errors.append(f"index_daily({symbol}): {str(res['error'])[:180]}")
            continue
        rows = res.get("rows") or []
        with SessionLocal() as db:
            written = upsert_index_bars(db, rows)
            db.commit()
        upserted_total += written
        per_index[symbol] = written
        time.sleep(_API_SLEEP_SECONDS)

    return {
        "end_date": target_end,
        "upserted": upserted_total,
        "per_index": per_index,
        "errors": errors,
        "ready": not errors,
    }


def load_index_closes(
    db: Session,
    symbol: str,
    *,
    as_of_date: str,
    limit: int = 250,
) -> list[tuple[str, float]]:
    """按日期升序读取不晚于 as_of_date 的最近 limit 根指数收盘价（严格 PIT，不读未来日）。"""
    compact = _compact_date(as_of_date)
    if not compact:
        return []
    rows = db.execute(
        select(IndexDailyBar.trade_date, IndexDailyBar.close)
        .where(
            IndexDailyBar.symbol == symbol.strip().upper(),
            IndexDailyBar.trade_date <= compact,
            IndexDailyBar.close.is_not(None),
            IndexDailyBar.close > 0,
        )
        .order_by(IndexDailyBar.trade_date.desc())
        .limit(limit)
    ).all()
    return [(str(day), float(close)) for day, close in reversed(rows)]
