"""全市场行情同步：把最近 N 个交易日的全市场 K 线与最新 daily_basic 落到本地表。

单一职责：只负责"数据入库"，不做筛选/解读（那是 quant/screener_*）。

数据边界（诚实声明）：
- 走 TuShare 原始通道 ``_tushare_request``（不经 tushare_query 的 300 行截断，
  全市场单日约 5000+ 行）；每次调用间 sleep 防限频。
- 增量幂等：按日提交，只补本地缺失的交易日；重跑不会重复拉取。
- 批量写统一 ``INSERT … ON DUPLICATE KEY UPDATE`` + executemany（分块 1000），
  不做逐股票循环。本项目仅支持 MySQL（core/database.py 硬校验），故此处直接写
  MySQL 方言 SQL。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text

from backend.app.core.database import SessionLocal, engine
from backend.app.core.models import DailyBar, Instrument, StockDailyBasic, StockLatestPrice
from backend.app.market.stock_tools import (
    _stock_basic_rows,
    _tushare_request,
    get_trade_cal,
    normalize_a_share_symbol,
)

logger = logging.getLogger(__name__)

_SYNC_DAYS_MIN = 5
_SYNC_DAYS_MAX = 60
_CAL_LOOKBACK_DAYS = 120  # 交易日历回看窗口，足够覆盖 60 个交易日
_API_SLEEP_SECONDS = 0.25
_UPSERT_CHUNK = 1000
# 全市场单日约 5400+ 只；某交易日 K 线数低于该值视为"未同步全市场"（按需缓存的
# 零星历史也会写入这些日期，不能只按"日期存在"判断幂等）
_FULL_MARKET_BAR_MIN = 3000
# 北交所后缀 BJ 统一归一为 BSE（与前端市场过滤一致）
_MARKET_BY_SUFFIX = {"SH": "SH", "SZ": "SZ", "BJ": "BSE"}

_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
_BASIC_FIELDS = "ts_code,trade_date,turnover_rate,volume_ratio,pe_ttm,pb,total_mv,circ_mv"


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _market_of(ts_code: str) -> str:
    suffix = str(ts_code).rsplit(".", 1)[-1].upper()
    return _MARKET_BY_SUFFIX.get(suffix, suffix or "CN")


def _stock_catalog_rows() -> list[dict]:
    """A 股目录 = 上市(L) + 暂停上市(P)，按 ts_code 去重（L 优先）。

    只拉 L 会漏掉少数暂停上市但仍有成交的标的，导致 daily_bars 插入时撞
    instruments 外键（1452）。P 拉取失败时降级为仅 L，不阻断同步。
    """
    rows = list(_stock_basic_rows())  # 自带 24h 缓存的 L 目录
    suspended = _tushare_request(
        "stock_basic", {"list_status": "P"}, "ts_code,symbol,name,market,exchange,list_date"
    )
    if "error" in suspended:
        logger.warning("stock_basic(P) 拉取失败，目录仅含 L：%s", suspended["error"])
        return rows
    seen = {str(row.get("ts_code") or "") for row in rows}
    for row in suspended.get("rows") or []:
        ts_code = str(row.get("ts_code") or "")
        if ts_code and ts_code not in seen:
            seen.add(ts_code)
            rows.append(row)
    return rows


def _ensure_instruments_for_bars(rows: list[dict]) -> None:
    """FK 父表自愈：K 线里出现但 instruments 缺失的 symbol 补占位行（name=symbol）。

    stock_basic 目录即使 L+P 合并仍会漏掉少数标的（已退市 D 状态但历史日仍有
    成交等），仅靠目录会让 daily_bars 整批 1000 行撞 FK 1452 全毁，故以 K 线数据
    本身为准兜底补父行。
    """
    symbols = {
        normalize_a_share_symbol(str(row.get("ts_code") or ""))
        for row in rows
        if row.get("ts_code")
    }
    symbols = {s for s in symbols if "." in s}
    if not symbols:
        return
    with SessionLocal() as db:
        existing = set(
            db.execute(
                select(Instrument.symbol).where(Instrument.symbol.in_(list(symbols)))
            ).scalars()
        )
    missing = sorted(symbols - existing)
    if not missing:
        return
    stmt = text(
        "INSERT IGNORE INTO instruments (symbol, name, market, type, status) "
        "VALUES (:symbol, :name, :market, :type, :status)"
    )
    with engine.begin() as conn:
        for chunk in _chunked(
            [
                {
                    "symbol": s,
                    "name": s,
                    "market": _market_of(s),
                    "type": "stock",
                    "status": "active",
                }
                for s in missing
            ]
        ):
            conn.execute(stmt, chunk)
    logger.info("market sync self-heal instruments +%d: %s", len(missing), ", ".join(missing[:12]))


def _chunked(rows: list[dict], size: int = _UPSERT_CHUNK):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _upsert_instruments(basic_rows: list[dict]) -> int:
    """全量 upsert A 股目录（FK 父表，必须先于 daily_bars/daily_basic 写）。

    注意：VALUES 里不能有字面量（如 'stock'），否则 pymysql executemany 的多行
    INSERT 重写正则失配，退化为逐行往返远程库（1000 行 ≈ 几十秒）。
    """
    if not basic_rows:
        return 0
    stmt = text(
        "INSERT INTO instruments (symbol, name, market, type, status, list_date) "
        "VALUES (:symbol, :name, :market, :type, :status, :list_date) "
        "ON DUPLICATE KEY UPDATE name=VALUES(name), market=VALUES(market), list_date=VALUES(list_date)"
    )
    count = 0
    with engine.begin() as conn:
        for chunk in _chunked(basic_rows):
            params = []
            for row in chunk:
                symbol = normalize_a_share_symbol(str(row.get("ts_code") or ""))
                if "." not in symbol:
                    continue
                params.append(
                    {
                        "symbol": symbol,
                        "name": str(row.get("name") or symbol)[:120],
                        "market": _market_of(symbol),
                        "type": "stock",
                        "status": "active",
                        "list_date": str(row.get("list_date") or "") or None,
                    }
                )
            if params:
                conn.execute(stmt, params)
                count += len(params)
    return count


def _bar_date_counts(trade_dates: list[str]) -> dict[str, int]:
    """各目标交易日在本地 daily_bars（tushare）里的行数。

    注意不能只查"日期是否存在"：按需缓存的零星历史会让日期早早出现，但只有
    几十上百行，不代表全市场已同步。
    """
    if not trade_dates:
        return {}
    normalized = func.replace(DailyBar.trade_date, "-", "")
    with SessionLocal() as db:
        rows = db.execute(
            select(normalized.label("norm_date"), func.count())
            .where(DailyBar.source == "tushare", normalized.in_(trade_dates))
            .group_by(normalized)
        ).all()
    return {str(norm_date): int(count) for norm_date, count in rows}


def _upsert_daily_bars(rows: list[dict]) -> int:
    if not rows:
        return 0
    _ensure_instruments_for_bars(rows)
    stmt = text(
        "INSERT INTO daily_bars "
        "(symbol, trade_date, open, high, low, close, pre_close, change_pct, volume, amount, source) "
        "VALUES (:symbol, :trade_date, :open, :high, :low, :close, :pre_close, :change_pct, :volume, :amount, :source) "
        "ON DUPLICATE KEY UPDATE open=VALUES(open), high=VALUES(high), low=VALUES(low), close=VALUES(close), "
        "pre_close=VALUES(pre_close), change_pct=VALUES(change_pct), volume=VALUES(volume), amount=VALUES(amount)"
    )
    count = 0
    with engine.begin() as conn:
        for chunk in _chunked(rows):
            params = []
            for row in chunk:
                symbol = normalize_a_share_symbol(str(row.get("ts_code") or ""))
                if "." not in symbol:
                    continue
                params.append(
                    {
                        "symbol": symbol,
                        "trade_date": str(row.get("trade_date") or ""),
                        "open": _float(row.get("open")),
                        "high": _float(row.get("high")),
                        "low": _float(row.get("low")),
                        "close": _float(row.get("close")),
                        "pre_close": _float(row.get("pre_close")),
                        "change_pct": _float(row.get("pct_chg")),
                        "volume": _float(row.get("vol")),
                        "amount": _float(row.get("amount")),
                        "source": "tushare",
                    }
                )
            if params:
                conn.execute(stmt, params)
                count += len(params)
    return count


def _upsert_daily_basic(rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = text(
        "INSERT INTO stock_daily_basic "
        "(symbol, trade_date, turnover_rate, volume_ratio, pe_ttm, pb, total_mv, circ_mv) "
        "VALUES (:symbol, :trade_date, :turnover_rate, :volume_ratio, :pe_ttm, :pb, :total_mv, :circ_mv) "
        "ON DUPLICATE KEY UPDATE turnover_rate=VALUES(turnover_rate), volume_ratio=VALUES(volume_ratio), "
        "pe_ttm=VALUES(pe_ttm), pb=VALUES(pb), total_mv=VALUES(total_mv), circ_mv=VALUES(circ_mv)"
    )
    count = 0
    with engine.begin() as conn:
        for chunk in _chunked(rows):
            params = []
            for row in chunk:
                symbol = normalize_a_share_symbol(str(row.get("ts_code") or ""))
                if "." not in symbol:
                    continue
                params.append(
                    {
                        "symbol": symbol,
                        "trade_date": str(row.get("trade_date") or ""),
                        "turnover_rate": _float(row.get("turnover_rate")),
                        "volume_ratio": _float(row.get("volume_ratio")),
                        "pe_ttm": _float(row.get("pe_ttm")),
                        "pb": _float(row.get("pb")),
                        "total_mv": _float(row.get("total_mv")),
                        "circ_mv": _float(row.get("circ_mv")),
                    }
                )
            if params:
                conn.execute(stmt, params)
                count += len(params)
    return count


def _upsert_latest_prices(rows: list[dict], names: dict[str, str]) -> int:
    """全市场最新价批量刷新：选股器候选池（stock_latest_prices）由此扩到全市场。"""
    if not rows:
        return 0
    stmt = text(
        "INSERT INTO stock_latest_prices (symbol, name, price, pre_close, pct_chg, trade_date, source, fetched_at) "
        "VALUES (:symbol, :name, :price, :pre_close, :pct_chg, :trade_date, :source, :fetched_at) "
        "ON DUPLICATE KEY UPDATE name=VALUES(name), price=VALUES(price), pre_close=VALUES(pre_close), "
        "pct_chg=VALUES(pct_chg), trade_date=VALUES(trade_date), fetched_at=VALUES(fetched_at)"
    )
    now = datetime.now()
    count = 0
    with engine.begin() as conn:
        for chunk in _chunked(rows):
            params = []
            for row in chunk:
                symbol = normalize_a_share_symbol(str(row.get("ts_code") or ""))
                if "." not in symbol:
                    continue
                params.append(
                    {
                        "symbol": symbol,
                        "name": names.get(symbol) or symbol,
                        "price": _float(row.get("close")),
                        "pre_close": _float(row.get("pre_close")),
                        "pct_chg": _float(row.get("pct_chg")),
                        "trade_date": str(row.get("trade_date") or ""),
                        "source": "tushare",
                        "fetched_at": now,
                    }
                )
            if params:
                conn.execute(stmt, params)
                count += len(params)
    return count


def sync_market(days: int = 20) -> dict:
    """增量同步最近 ``days`` 个交易日的全市场行情（5-60 天）。

    流程：交易日历 → instruments 目录（FK 父表先行）→ 逐缺失日拉 daily →
    最新一日 daily_basic → 全市场最新价。按日提交，错误记入 errors；
    日历/目录失败是硬错误（RuntimeError，上游原文透出）。
    """
    if not _SYNC_DAYS_MIN <= int(days or 0) <= _SYNC_DAYS_MAX:
        raise ValueError(f"同步天数只支持 {_SYNC_DAYS_MIN}-{_SYNC_DAYS_MAX}")
    started = time.monotonic()
    stats: dict[str, Any] = {
        "latest_trade_date": None,
        "target_days": int(days),
        "dates_fetched": 0,
        "bars_upserted": 0,
        "basic_upserted": 0,
        "latest_prices_upserted": 0,
        "instruments_upserted": 0,
        "errors": [],
    }

    cal = get_trade_cal(
        start_date=(date.today() - timedelta(days=_CAL_LOOKBACK_DAYS)).strftime("%Y%m%d")
    )
    if "error" in cal:
        raise RuntimeError(cal["error"])
    open_dates = sorted(
        {
            str(row.get("cal_date"))
            for row in cal.get("rows") or []
            if str(row.get("is_open")) == "1" and row.get("cal_date")
        },
        reverse=True,
    )
    targets = open_dates[: int(days)]
    if not targets:
        raise RuntimeError("交易日历为空，无法确定同步目标交易日")
    latest = targets[0]
    stats["latest_trade_date"] = latest

    basic_rows = _stock_catalog_rows()
    if not basic_rows:
        raise RuntimeError("获取 A 股股票目录失败（TuShare stock_basic），无法同步")
    stats["instruments_upserted"] = _upsert_instruments(basic_rows)
    names = {
        normalize_a_share_symbol(str(row.get("ts_code") or "")): str(row.get("name") or "")
        for row in basic_rows
        if row.get("ts_code")
    }

    existing = _bar_date_counts(targets)
    missing = [
        d
        for d in sorted(targets)
        if existing.get(d, 0) < _FULL_MARKET_BAR_MIN
    ]  # 旧 → 新
    latest_rows: list[dict] | None = None
    for trade_date in missing:
        day_start = time.monotonic()
        result = _tushare_request("daily", {"trade_date": trade_date}, _DAILY_FIELDS)
        if "error" in result:
            stats["errors"].append(f"{trade_date}: {result['error']}")
            break  # 按日提交：已入库的保留，后续日期下次再补
        rows = result.get("rows") or []
        upserted = _upsert_daily_bars(rows)
        stats["bars_upserted"] += upserted
        stats["dates_fetched"] += 1
        logger.info(
            "market sync day %s: %d rows fetched, %d upserted, %.1fs",
            trade_date, len(rows), upserted, time.monotonic() - day_start,
        )
        if trade_date == latest:
            latest_rows = rows
        time.sleep(_API_SLEEP_SECONDS)

    result = _tushare_request("daily_basic", {"trade_date": latest}, _BASIC_FIELDS)
    if "error" in result:
        stats["errors"].append(f"daily_basic({latest}): {result['error']}")
    else:
        stats["basic_upserted"] = _upsert_daily_basic(result.get("rows") or [])
    time.sleep(_API_SLEEP_SECONDS)

    if latest_rows is None:
        # 最新交易日本地已有 K 线（本轮未拉），仍需一次调用刷新全市场最新价
        result = _tushare_request("daily", {"trade_date": latest}, _DAILY_FIELDS)
        if "error" in result:
            stats["errors"].append(f"latest({latest}): {result['error']}")
        else:
            latest_rows = result.get("rows") or []
    stats["latest_prices_upserted"] = _upsert_latest_prices(latest_rows or [], names)

    stats["elapsed_seconds"] = round(time.monotonic() - started, 1)
    logger.info(
        "market sync done: latest=%s dates_fetched=%d bars=%d basic=%d prices=%d elapsed=%.1fs",
        latest,
        stats["dates_fetched"],
        stats["bars_upserted"],
        stats["basic_upserted"],
        stats["latest_prices_upserted"],
        stats["elapsed_seconds"],
    )
    return stats


def sync_status() -> dict:
    """本地同步水位（纯本地查询，不外呼）：供前端展示"数据截至哪天、多少只"。"""
    normalized = func.replace(DailyBar.trade_date, "-", "")
    with SessionLocal() as db:
        latest_bar_date = db.execute(
            select(func.max(normalized)).where(DailyBar.source == "tushare")
        ).scalar()
        bar_symbols = 0
        if latest_bar_date:
            bar_symbols = int(
                db.execute(
                    select(func.count(func.distinct(DailyBar.symbol))).where(
                        DailyBar.source == "tushare", normalized == str(latest_bar_date)
                    )
                ).scalar()
                or 0
            )
        basic_date = db.execute(select(func.max(StockDailyBasic.trade_date))).scalar()
        basic_rows = 0
        if basic_date:
            basic_rows = int(
                db.execute(
                    select(func.count()).where(StockDailyBasic.trade_date == str(basic_date))
                ).scalar()
                or 0
            )
        price_rows = int(
            db.execute(select(func.count()).select_from(StockLatestPrice)).scalar() or 0
        )
        instrument_rows = int(
            db.execute(
                select(func.count()).select_from(Instrument).where(Instrument.type == "stock")
            ).scalar()
            or 0
        )
    return {
        "latest_bar_date": str(latest_bar_date or ""),
        "bar_symbols": bar_symbols,
        "basic_date": str(basic_date or ""),
        "basic_rows": basic_rows,
        "price_rows": price_rows,
        "instrument_rows": instrument_rows,
    }
