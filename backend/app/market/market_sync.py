"""全市场行情同步：把最近 N 个交易日的全市场 K 线、复权因子与最新 daily_basic 落到本地表。

单一职责：只负责"数据入库"，不做筛选/解读（那是 quant/screener_*）。

数据边界（诚实声明）：
- 走 TuShare 原始通道 ``_tushare_request``（不经 tushare_query 的 300 行截断，
  全市场单日约 5000+ 行）；每次调用间 sleep 防限频。
- daily_bars 为不复权价；同窗口的 adj_factor 落 stock_adj_factors，供下游换算
  后复权价。复权因子覆盖不足同样计入 selection 缓存完整性检查。
- 增量幂等：按日提交，只补本地缺失的交易日；重跑不会重复拉取。
- 批量写统一 ``INSERT … ON DUPLICATE KEY UPDATE`` + executemany（分块 1000），
  不做逐股票循环。本项目仅支持 MySQL（core/database.py 硬校验），故此处直接写
  MySQL 方言 SQL。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from math import ceil, isfinite
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from backend.app.core.database import SessionLocal, engine
from backend.app.core.models import (
    DailyBar,
    Instrument,
    MarketTradingCalendar,
    StockAdjFactor,
    StockDailyBasic,
    StockLatestPrice,
)
from backend.app.market.stock_tools import (
    _tushare_request,
    get_trade_cal,
    normalize_a_share_symbol,
)

logger = logging.getLogger(__name__)

_SYNC_DAYS_MIN = 5
_SYNC_DAYS_MAX = 260
_CAL_LOOKBACK_DAYS = 120  # 默认交易日历回看窗口，days > 60 时按 2 倍天数自动拉宽
_API_SLEEP_SECONDS = 0.25
_REQUEST_RETRIES = 3
_UPSERT_CHUNK = 1000
# 全市场单日约 5400+ 只；某交易日 K 线数低于该值视为"未同步全市场"（按需缓存的
# 零星历史也会写入这些日期，不能只按"日期存在"判断幂等）
_FULL_MARKET_BAR_MIN = 3000
# 北交所后缀 BJ 统一归一为 BSE（与前端市场过滤一致）
_MARKET_BY_SUFFIX = {"SH": "SH", "SZ": "SZ", "BJ": "BSE"}
_CATALOG_SYMBOL_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")

_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
_BASIC_FIELDS = "ts_code,trade_date,turnover_rate,turnover_rate_f,volume_ratio,pe_ttm,pb,total_mv,circ_mv"
_ADJ_FIELDS = "ts_code,trade_date,adj_factor"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
# TuShare 日线约 15:00–16:00、daily_basic 约 15:00–17:00 入库；此前当天的全市场截面
# 还不完整，盘中或刚收盘时去拉“今天”必然失败。
SELECTION_DATA_READY_TIME = dt_time(17, 0)


def selection_target_ceiling(now: datetime | None = None) -> str:
    """自动选股可用的最晚交易日上限（含）：北京时间 17:00 前只能用到前一个自然日。

    目标交易日 = 本地权威日历中不晚于该上限的最近开市日。"""
    current = now or datetime.now(_SHANGHAI)
    day = current.date()
    if current.time() < SELECTION_DATA_READY_TIME:
        day -= timedelta(days=1)
    return day.strftime("%Y%m%d")


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


def _catalog_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    return symbol if _CATALOG_SYMBOL_RE.fullmatch(symbol) else ""


def _stock_catalog_rows() -> list[dict]:
    """完整获取 L/P/D 后才允许调用方写库；不以缺席推断退市。

    不使用会吞错且缓存 24h 的 _stock_basic_rows。P 可以真实为空，L/D 空值、
    截断的 L 目录、状态冲突及任一通道失败都不可信，必须保留原缓存并报错。
    """
    catalog: dict[str, dict] = {}
    skipped_delisted_aliases: list[str] = []
    for status in ("L", "P", "D"):
        response = _tushare_request(
            "stock_basic",
            {"list_status": status},
            "ts_code,symbol,name,market,exchange,industry,list_date,list_status",
        )
        if "error" in response:
            raise RuntimeError(f"stock_basic({status}): {response['error']}")
        rows = response.get("rows")
        if not isinstance(rows, list) or (not rows and status != "P"):
            raise RuntimeError(f"stock_basic({status}) 目录为空或格式无效，拒绝覆盖本地目录")
        symbols: set[str] = set()
        for row in rows:
            raw_symbol = str(row.get("ts_code") or "").strip() if isinstance(row, dict) else ""
            symbol = _catalog_symbol(raw_symbol)
            if not symbol:
                if status == "D" and raw_symbol:
                    skipped_delisted_aliases.append(raw_symbol)
                    continue
                raise RuntimeError(f"stock_basic({status}) 含无效标的或状态，拒绝覆盖本地目录")
            if str(row.get("list_status") or status).upper() != status:
                raise RuntimeError(f"stock_basic({status}) 含无效标的或状态，拒绝覆盖本地目录")
            if symbol in catalog and catalog[symbol]["list_status"] != status:
                raise RuntimeError(f"stock_basic 目录状态冲突：{symbol}，拒绝覆盖本地目录")
            symbols.add(symbol)
            catalog[symbol] = {**row, "ts_code": symbol, "list_status": status}
        if status == "L" and len(symbols) < _FULL_MARKET_BAR_MIN:
            raise RuntimeError(f"stock_basic(L) 目录不完整：{len(symbols)} 只，拒绝覆盖本地目录")
        if status == "D" and not symbols:
            raise RuntimeError("stock_basic(D) 无标准六位股票代码，拒绝覆盖本地目录")
        time.sleep(_API_SLEEP_SECONDS)
    if skipped_delisted_aliases:
        logger.warning(
            "stock_basic(D) 跳过 %d 个非标准历史别名：%s",
            len(skipped_delisted_aliases),
            ", ".join(skipped_delisted_aliases[:10]),
        )
    return list(catalog.values())


def load_stock_catalog_snapshot() -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    for row in _stock_catalog_rows():
        symbol = normalize_a_share_symbol(str(row.get("ts_code") or ""))
        if "." not in symbol:
            continue
        snapshot.append(
            {
                "symbol": symbol,
                "name": str(row.get("name") or symbol),
                "market": row.get("market") or _market_of(symbol),
                "stock_type": "stock",
                "list_status": str(row.get("list_status") or "L").upper(),
                "list_date": row.get("list_date"),
                "industry": row.get("industry"),
                "source": "tushare.stock_basic",
            }
        )
    return sorted(snapshot, key=lambda item: item["symbol"])


def load_local_stock_catalog_snapshot() -> list[dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.execute(
            select(
                Instrument.symbol,
                Instrument.name,
                Instrument.market,
                Instrument.type,
                Instrument.list_status,
                Instrument.list_date,
                Instrument.industry,
            ).where(
                Instrument.type == "stock",
                Instrument.list_status.in_(("L", "P", "D")),
            )
        ).all()
    return sorted(
        [
            {
                "symbol": str(row.symbol),
                "name": str(row.name or row.symbol),
                "market": row.market,
                "stock_type": row.type,
                "list_status": str(row.list_status),
                "list_date": row.list_date,
                "industry": row.industry,
                "source": "tushare.stock_basic.cache",
            }
            for row in rows
        ],
        key=lambda item: item["symbol"],
    )


def _ensure_instruments_for_bars(rows: list[dict]) -> None:
    """FK 父表自愈：K 线里出现但 instruments 缺失的 symbol 补占位行（name=symbol）。

    即使完整目录仍可能漏掉历史标的；仅靠目录会让 daily_bars 整批 1000 行撞
    FK 1452，故以 K 线补父行，但不猜测 list_status（留空，不作为上市或退市证据）。
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
        "INSERT INTO instruments "
        "(symbol, name, market, type, status, list_date, list_status, industry) "
        "VALUES (:symbol, :name, :market, :type, :status, :list_date, :list_status, :industry) "
        "ON DUPLICATE KEY UPDATE "
        "name=VALUES(name), market=VALUES(market), list_date=VALUES(list_date), "
        "list_status=VALUES(list_status), industry=VALUES(industry)"
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
                        "list_status": str(row.get("list_status") or "").upper() or None,
                        "industry": str(row.get("industry") or "").strip()[:120] or None,
                    }
                )
            if params:
                conn.execute(stmt, params)
                count += len(params)
    return count


def _compact_date(value: Any) -> str:
    compact = str(value or "").replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        return ""
    try:
        date.fromisoformat(_iso_date(compact))
    except ValueError:
        return ""
    return compact


def _iso_date(compact: str) -> str:
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"


def _date_variants(trade_dates: list[str]) -> list[str]:
    compact = {_compact_date(day) for day in trade_dates} - {""}
    return sorted(compact | {_iso_date(day) for day in compact})


def _date_symbol_counts(model: Any, trade_dates: list[str], *, listed_only: bool = False) -> dict[str, int]:
    """兼容 compact/ISO 日期；先用原列 IN 缩小范围，再归一分组去重。"""
    if not trade_dates:
        return {}
    normalized = func.replace(model.trade_date, "-", "")
    query = select(normalized, func.count(func.distinct(model.symbol))).where(
        model.trade_date.in_(_date_variants(trade_dates))
    )
    if model is DailyBar:
        query = query.where(DailyBar.source == "tushare")
    if listed_only:
        query = query.join(Instrument, Instrument.symbol == model.symbol).where(
            Instrument.type == "stock", Instrument.list_status == "L",
        )
    with SessionLocal() as db:
        rows = db.execute(query.group_by(normalized)).all()
    return {str(norm_date): int(count) for norm_date, count in rows}


def _bar_date_counts(trade_dates: list[str], *, listed_only: bool = False) -> dict[str, int]:
    """各目标日不同标的数；同一 symbol 的两种日期格式不能重复计数。"""
    return _date_symbol_counts(DailyBar, trade_dates, listed_only=listed_only)


def _adj_factor_date_counts(trade_dates: list[str], *, listed_only: bool = False) -> dict[str, int]:
    """各目标日已落库复权因子的不同标的数。"""
    return _date_symbol_counts(StockAdjFactor, trade_dates, listed_only=listed_only)


def _latest_local_date(db: Any, model: Any, through: str) -> str | None:
    """分别走两种原始日期格式的范围，避免 REPLACE(date) 全表谓词扫描。"""
    dates = []
    for start, end in (("19000101", through), ("1900-01-01", _iso_date(through))):
        query = select(func.max(model.trade_date)).where(
            model.trade_date.between(start, end),
            func.length(model.trade_date) == len(start),
        )
        if model is DailyBar:
            query = query.where(DailyBar.source == "tushare")
        day = _compact_date(db.scalar(query))
        if day:
            dates.append(day)
    return max(dates) if dates else None


def _selection_minimum_counts(catalog_count: int) -> dict[str, int]:
    # 延迟引用同一份质量门槛，避免复制数值，普通行情同步的模块导入保持轻量。
    from backend.app.quant.automatic_selection_scoring import DEFAULT_STRATEGY_CONFIG

    quality = DEFAULT_STRATEGY_CONFIG["quality"]
    return {
        kind: max(_FULL_MARKET_BAR_MIN, ceil(catalog_count * float(quality[f"min_{kind}_coverage"])))
        for kind in ("bar", "basic", "history")
    }


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
        "(symbol, trade_date, turnover_rate, turnover_rate_f, volume_ratio, pe_ttm, pb, total_mv, circ_mv) "
        "VALUES (:symbol, :trade_date, :turnover_rate, :turnover_rate_f, :volume_ratio, :pe_ttm, :pb, :total_mv, :circ_mv) "
        "ON DUPLICATE KEY UPDATE turnover_rate=VALUES(turnover_rate), "
        "turnover_rate_f=COALESCE(VALUES(turnover_rate_f), turnover_rate_f), volume_ratio=VALUES(volume_ratio), "
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
                        "turnover_rate_f": _float(row.get("turnover_rate_f")),
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


def _upsert_adj_factors(rows: list[dict]) -> int:
    """复权因子按 (symbol, trade_date) 幂等写入；非正或非有限值不落库，按缺失处理。"""
    if not rows:
        return 0
    stmt = text(
        "INSERT INTO stock_adj_factors (symbol, trade_date, adj_factor) "
        "VALUES (:symbol, :trade_date, :adj_factor) "
        "ON DUPLICATE KEY UPDATE adj_factor=VALUES(adj_factor)"
    )
    count = 0
    with engine.begin() as conn:
        for chunk in _chunked(rows):
            params = []
            for row in chunk:
                symbol = normalize_a_share_symbol(str(row.get("ts_code") or ""))
                trade_date = _compact_date(row.get("trade_date"))
                adj_factor = _float(row.get("adj_factor"))
                if "." not in symbol or not trade_date or adj_factor is None:
                    continue
                if not isfinite(adj_factor) or adj_factor <= 0:
                    continue
                params.append({"symbol": symbol, "trade_date": trade_date, "adj_factor": adj_factor})
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


def _upsert_trading_calendar(rows: list[dict[str, Any]]) -> int:
    stmt = text(
        "INSERT INTO market_trading_calendar "
        "(exchange, cal_date, is_open, pretrade_date, source, fetched_at) "
        "VALUES (:exchange, :cal_date, :is_open, :pretrade_date, :source, :fetched_at) "
        "ON DUPLICATE KEY UPDATE "
        "is_open=VALUES(is_open), pretrade_date=VALUES(pretrade_date), "
        "source=VALUES(source), fetched_at=VALUES(fetched_at)"
    )
    fetched_at = datetime.now()
    params = []
    for row in rows:
        cal_date = str(row.get("cal_date") or "").replace("-", "")
        if len(cal_date) != 8 or not cal_date.isdigit():
            continue
        params.append(
            {
                "exchange": str(row.get("exchange") or "SSE").upper()[:16],
                "cal_date": cal_date,
                "is_open": str(row.get("is_open") or "0").strip().lower()
                in {"1", "true", "yes"},
                "pretrade_date": (
                    str(row.get("pretrade_date") or "").replace("-", "") or None
                ),
                "source": "tushare",
                "fetched_at": fetched_at,
            }
        )
    if not params:
        return 0
    with engine.begin() as conn:
        for chunk in _chunked(params):
            conn.execute(stmt, chunk)
    return len(params)


def load_local_open_trade_dates(
    *, start_exclusive: str, limit: int = 60, exchange: str = "SSE",
) -> list[str]:
    cap = max(1, min(int(limit), 250))
    start = _compact_date(start_exclusive)
    if not start:
        raise ValueError("前瞻评估起始交易日无效")
    today = date.today().strftime("%Y%m%d")
    with SessionLocal() as db:
        dates = list(db.scalars(
            select(MarketTradingCalendar.cal_date)
            .where(
                MarketTradingCalendar.exchange == exchange.upper(),
                MarketTradingCalendar.source == "tushare",
                MarketTradingCalendar.is_open.is_(True),
                MarketTradingCalendar.cal_date > start,
                MarketTradingCalendar.cal_date <= today,
            )
            .order_by(MarketTradingCalendar.cal_date)
            .limit(cap)
        ).all())
        end = dates[-1] if len(dates) == cap else today
        calendar_dates = set(db.scalars(
            select(MarketTradingCalendar.cal_date).where(
                MarketTradingCalendar.exchange == exchange.upper(),
                MarketTradingCalendar.source == "tushare",
                MarketTradingCalendar.cal_date.between(start, end),
            )
        ).all())
    if _calendar_gaps(calendar_dates, start, end):
        raise RuntimeError("前瞻评估交易日历不连续，不能把后续交易日冒充下一交易日")
    return dates


def load_local_history_trade_dates(
    *, end_inclusive: str, limit: int = 21, exchange: str = "SSE",
) -> list[str]:
    """权威本地日历中截至目标日最近 limit 个交易日，按升序返回，不推测工作日。"""
    end = _compact_date(end_inclusive)
    if not end:
        raise ValueError("历史交易日截止日期无效")
    cap = max(1, min(int(limit), 250))
    with SessionLocal() as db:
        dates = db.scalars(
            select(MarketTradingCalendar.cal_date)
            .where(
                MarketTradingCalendar.exchange == exchange.upper(),
                MarketTradingCalendar.source == "tushare",
                MarketTradingCalendar.is_open.is_(True),
                MarketTradingCalendar.cal_date <= end,
            )
            .order_by(MarketTradingCalendar.cal_date.desc())
            .limit(cap)
        ).all()
    return sorted(str(day) for day in dates)


def load_local_adj_factors(
    db: Any, symbols: Iterable[str], trade_dates: Iterable[str],
) -> dict[tuple[str, str], float]:
    """本地复权因子 {(symbol, YYYYMMDD): adj_factor}；缺失即缺失，不用前值填充。"""
    symbol_list = sorted({str(symbol) for symbol in symbols if symbol})
    dates = sorted({_compact_date(day) for day in trade_dates} - {""})
    factors: dict[tuple[str, str], float] = {}
    if not symbol_list or not dates:
        return factors
    for start in range(0, len(symbol_list), 500):
        rows = db.execute(
            select(StockAdjFactor.symbol, StockAdjFactor.trade_date, StockAdjFactor.adj_factor).where(
                StockAdjFactor.symbol.in_(symbol_list[start : start + 500]),
                StockAdjFactor.trade_date.in_(dates),
            )
        ).all()
        for symbol, trade_date, adj_factor in rows:
            value = _float(adj_factor)
            if value is not None and isfinite(value) and value > 0:
                factors[(str(symbol), str(trade_date))] = value
    return factors


def _calendar_gaps(dates: set[str], start: str, end: str) -> list[str]:
    missing = []
    day = date.fromisoformat(_iso_date(start))
    stop = date.fromisoformat(_iso_date(end))
    while day <= stop:
        compact = day.strftime("%Y%m%d")
        if compact not in dates:
            missing.append(compact)
        day += timedelta(days=1)
    return missing


def local_selection_cache_status(
    history_days: int = 20, *, target_ceiling: str | None = None,
) -> dict[str, Any]:
    """无外呼的完整性检查，目标日只来自覆盖今日的 SSE 权威日历。

    目标日 = 不晚于 ``target_ceiling``（默认见 selection_target_ceiling）的最近开市日，
    当天数据发布前不会把“今天”当成可排名的交易日。

    latest_trade_date 是日线与 basic 实际水位的较小值，不冒充日历目标；
    覆盖率分母仅 L，P/D 保留在目录中作为排除证据，不参与覆盖率计数。
    每个预期历史日都要有全市场数量，单只股票或重复日期格式不能通过。
    """
    history_days = int(history_days)
    if not 1 <= history_days < 250:
        raise ValueError("历史窗口只支持 1-249 个交易日")
    today = date.today().strftime("%Y%m%d")
    ceiling = min(target_ceiling or selection_target_ceiling(), today)
    with SessionLocal() as db:
        calendar_today = db.scalar(
            select(MarketTradingCalendar.cal_date).where(
                MarketTradingCalendar.exchange == "SSE",
                MarketTradingCalendar.source == "tushare",
                MarketTradingCalendar.cal_date == today,
            )
        )
        calendar_date = db.scalar(
            select(func.max(MarketTradingCalendar.cal_date)).where(
                MarketTradingCalendar.exchange == "SSE",
                MarketTradingCalendar.source == "tushare",
                MarketTradingCalendar.is_open.is_(True),
                MarketTradingCalendar.cal_date <= ceiling,
            )
        )
        # 水位只看上限以内：盘中零星写入的当天行情不能让“前一交易日”看起来未对齐。
        bar_date = _latest_local_date(db, DailyBar, ceiling)
        basic_date = _latest_local_date(db, StockDailyBasic, ceiling)
        catalog_count = int(
            db.scalar(
                select(func.count()).select_from(Instrument).where(
                    Instrument.type == "stock", Instrument.list_status == "L",
                )
            ) or 0
        )
    expected_date = str(calendar_date) if calendar_today and calendar_date else None
    history_dates = (
        load_local_history_trade_dates(end_inclusive=expected_date, limit=history_days + 1)
        if expected_date else []
    )
    errors: list[str] = []
    if not calendar_today:
        errors.append(f"本地 SSE 权威交易日历未覆盖今日 {today}，无法确定目标交易日")
    if not calendar_date:
        errors.append("本地 SSE 权威交易日历无已开市交易日")
    calendar_gaps: list[str] = []
    if history_dates:
        with SessionLocal() as db:
            cached_dates = set(db.scalars(
                select(MarketTradingCalendar.cal_date).where(
                    MarketTradingCalendar.exchange == "SSE",
                    MarketTradingCalendar.source == "tushare",
                    MarketTradingCalendar.cal_date.between(history_dates[0], today),
                )
            ).all())
        calendar_gaps = _calendar_gaps(cached_dates, history_dates[0], today)
        if calendar_gaps:
            errors.append("本地 SSE 权威交易日历缺失日期：" + ",".join(calendar_gaps))
            # 尤其防止目标日至今日之间缺了一个开市日，旧目标不能冒充最新目标。
            if any(day > str(calendar_date) for day in calendar_gaps):
                expected_date = None
    if len(history_dates) != history_days + 1:
        errors.append(f"本地历史交易日不足：{len(history_dates)}/{history_days + 1}")
    if not bar_date:
        errors.append("本地全市场日线为空")
    if not basic_date:
        errors.append("本地 daily_basic 为空")
    if expected_date and bar_date != expected_date:
        errors.append(f"日线水位 {bar_date or '空'} 未对齐交易日 {expected_date}")
    if expected_date and basic_date != expected_date:
        errors.append(f"daily_basic 水位 {basic_date or '空'} 未对齐交易日 {expected_date}")
    if catalog_count < _FULL_MARKET_BAR_MIN:
        errors.append(f"本地上市 A 股目录不完整：{catalog_count} 只，至少需 {_FULL_MARKET_BAR_MIN}")

    bar_counts = _bar_date_counts(history_dates, listed_only=True)
    basic_counts = _date_symbol_counts(StockDailyBasic, [expected_date], listed_only=True) if expected_date else {}
    bar_count = bar_counts.get(expected_date, 0)
    basic_count = basic_counts.get(expected_date, 0)
    minimum = _selection_minimum_counts(catalog_count)
    if bar_count < minimum["bar"]:
        errors.append(f"目标日日线覆盖不足：{bar_count}/{catalog_count}，至少需 {minimum['bar']}")
    if basic_count < minimum["basic"]:
        errors.append(f"目标日 daily_basic 覆盖不足：{basic_count}/{catalog_count}，至少需 {minimum['basic']}")
    history_counts = {day: bar_counts.get(day, 0) for day in history_dates}
    for day, count in history_counts.items():
        if count < minimum["history"]:
            errors.append(f"历史日线 {day} 覆盖不足：{count}/{catalog_count}，至少需 {minimum['history']}")
    # 动量/波动按后复权价计算，窗口内任一日缺复权因子都会让除权缺口混进特征。
    adj_counts = _adj_factor_date_counts(history_dates, listed_only=True)
    adj_factor_counts = {day: adj_counts.get(day, 0) for day in history_dates}
    for day, count in adj_factor_counts.items():
        if count < minimum["history"]:
            errors.append(f"复权因子 {day} 覆盖不足：{count}/{catalog_count}，至少需 {minimum['history']}")
    return {
        "ready": not errors,
        "expected_trade_date": expected_date,
        "target_ceiling": ceiling,
        "latest_trade_date": min(bar_date, basic_date) if bar_date and basic_date else None,
        "calendar_date": str(calendar_date) if calendar_date else None,
        "calendar_through_today": bool(calendar_today),
        "calendar_missing_dates": calendar_gaps,
        "bar_date": bar_date,
        "basic_date": basic_date,
        "catalog_count": catalog_count,
        "bar_count": bar_count,
        "basic_count": basic_count,
        "bar_coverage": bar_count / catalog_count if catalog_count else 0.0,
        "basic_coverage": basic_count / catalog_count if catalog_count else 0.0,
        "history_days": history_days,
        "expected_history_dates": history_dates,
        "history_date_counts": history_counts,
        "history_coverage_by_date": {
            day: count / catalog_count if catalog_count else 0.0
            for day, count in history_counts.items()
        },
        "adj_factor_date_counts": adj_factor_counts,
        "minimum_counts": minimum,
        "errors": errors,
    }


def _validated_market_rows(
    response: dict, *, label: str, trade_date: str, minimum: int, listed_symbols: set[str],
) -> list[dict]:
    if "error" in response:
        raise RuntimeError(f"{label}: {response['error']}")
    rows = response.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError(f"{label}: 行情响应格式无效")
    symbols: set[str] = set()
    for row in rows:
        symbol = _catalog_symbol(row.get("ts_code")) if isinstance(row, dict) else ""
        if not symbol or _compact_date(row.get("trade_date")) != trade_date:
            raise RuntimeError(f"{label}: 含无效标的或非目标日行情")
        symbols.add(symbol)
    count = len(symbols & listed_symbols)
    if count < minimum:
        raise RuntimeError(f"{label}: 全市场响应不完整，上市标的 {count}/{len(listed_symbols)}，至少需 {minimum}")
    return rows


def _fetch_market_rows(
    api_name: str, fields: str, *, trade_date: str, minimum: int, listed_symbols: set[str],
) -> tuple[list[dict] | None, str | None]:
    """按交易日拉全市场截面并校验完整性；重试耗尽后返回最后一次校验错误原文。"""
    error: str | None = None
    for attempt in range(_REQUEST_RETRIES):
        response = _tushare_request(api_name, {"trade_date": trade_date}, fields)
        try:
            rows = _validated_market_rows(
                response, label=f"{api_name}({trade_date})", trade_date=trade_date,
                minimum=minimum, listed_symbols=listed_symbols,
            )
        except RuntimeError as exc:
            error = str(exc)
            time.sleep(_API_SLEEP_SECONDS * (attempt + 1))
            continue
        return rows, None
    return None, error or f"{api_name}({trade_date}): 行情响应格式无效"


def sync_market(days: int = 20, *, target_ceiling: str | None = None) -> dict:
    """增量同步最近 ``days`` 个交易日的全市场行情（5-60 天）。

    ``target_ceiling``（YYYYMMDD）限制同步窗口的最新交易日；自动选股传入
    selection_target_ceiling()，避免在当天数据发布前去拉“今天”。默认到今天为止。

    流程：交易日历 → instruments 目录（FK 父表先行）→ 逐缺失日拉 daily →
    逐缺失日拉 adj_factor → 最新一日 daily_basic → 全市场最新价。按日提交，错误记入 errors；
    日历/目录失败是硬错误（RuntimeError，上游原文透出）。
    """
    if not _SYNC_DAYS_MIN <= int(days or 0) <= _SYNC_DAYS_MAX:
        raise ValueError(f"同步天数只支持 {_SYNC_DAYS_MIN}-{_SYNC_DAYS_MAX}")
    started = time.monotonic()
    stats: dict[str, Any] = {
        "latest_trade_date": None,
        "target_days": int(days),
        "calendar_rows_upserted": 0,
        "dates_fetched": 0,
        "bars_upserted": 0,
        "adj_dates_fetched": 0,
        "adj_factors_upserted": 0,
        "basic_upserted": 0,
        "latest_prices_upserted": 0,
        "instruments_upserted": 0,
        "errors": [],
    }

    today = date.today().strftime("%Y%m%d")
    cal_lookback = max(_CAL_LOOKBACK_DAYS, int(days) * 2)
    cal = get_trade_cal(
        start_date=(date.today() - timedelta(days=cal_lookback)).strftime("%Y%m%d"),
        end_date=today,
        exchange="SSE",
    )
    if "error" in cal:
        raise RuntimeError(cal["error"])
    calendar_by_date: dict[str, dict] = {}
    for row in cal.get("rows") or []:
        if str(row.get("exchange") or "").upper() != "SSE":
            continue
        day = _compact_date(row.get("cal_date"))
        is_open = str(row.get("is_open")).lower()
        if not day or is_open not in {"0", "1", "false", "true"}:
            raise RuntimeError("SSE 交易日历含无效日期或开市标记")
        if day > today:
            continue
        normalized = {**row, "cal_date": day, "is_open": int(is_open in {"1", "true"})}
        if day in calendar_by_date and calendar_by_date[day]["is_open"] != normalized["is_open"]:
            raise RuntimeError(f"SSE 交易日历开市标记冲突：{day}")
        calendar_by_date[day] = normalized
    if today not in calendar_by_date:
        raise RuntimeError(f"SSE 交易日历未覆盖今日 {today}，无法确定同步目标交易日")
    ceiling = min(target_ceiling or today, today)
    open_dates = sorted(
        (day for day, row in calendar_by_date.items() if row["is_open"] and day <= ceiling),
        reverse=True,
    )
    targets = open_dates[: int(days)]
    if len(targets) != int(days):
        raise RuntimeError(f"SSE 交易日历不足 {days} 个交易日，无法确定完整同步窗口")
    calendar_gaps = _calendar_gaps(set(calendar_by_date), targets[-1], today)
    if calendar_gaps:
        raise RuntimeError("SSE 交易日历缺失日期：" + ",".join(calendar_gaps))
    latest = targets[0]
    stats["latest_trade_date"] = latest

    # 三类目录全部可信后再开始任何写入；失败时连日历水位也不推进。
    basic_rows = _stock_catalog_rows()
    if not basic_rows:
        raise RuntimeError("获取 A 股股票目录失败（TuShare stock_basic），无法同步")
    listed_symbols = {row["ts_code"] for row in basic_rows if row["list_status"] == "L"}
    minimum = _selection_minimum_counts(len(listed_symbols))
    stats["calendar_rows_upserted"] = _upsert_trading_calendar(list(calendar_by_date.values()))
    stats["instruments_upserted"] = _upsert_instruments(basic_rows)
    names = {
        normalize_a_share_symbol(str(row.get("ts_code") or "")): str(row.get("name") or "")
        for row in basic_rows
        if row.get("ts_code")
    }

    existing = _bar_date_counts(targets, listed_only=True)
    missing = [
        d
        for d in sorted(targets)
        if existing.get(d, 0) < minimum["bar" if d == latest else "history"]
    ]  # 旧 → 新

    latest_rows: list[dict] | None = None
    for trade_date in missing:
        day_start = time.monotonic()
        rows, error = _fetch_market_rows(
            "daily", _DAILY_FIELDS, trade_date=trade_date,
            minimum=minimum["bar" if trade_date == latest else "history"],
            listed_symbols=listed_symbols,
        )
        if rows is None:
            stats["errors"].append(error)
            break
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

    # 复权因子与 K 线同窗口、同完整性门槛；只补本地覆盖不足的交易日。
    existing_adj = _adj_factor_date_counts(targets, listed_only=True)
    for trade_date in sorted(targets):
        if existing_adj.get(trade_date, 0) >= minimum["history"]:
            continue
        rows, error = _fetch_market_rows(
            "adj_factor", _ADJ_FIELDS, trade_date=trade_date,
            minimum=minimum["history"], listed_symbols=listed_symbols,
        )
        if rows is None:
            stats["errors"].append(error)
            break
        stats["adj_factors_upserted"] += _upsert_adj_factors(rows)
        stats["adj_dates_fetched"] += 1
        time.sleep(_API_SLEEP_SECONDS)

    result = _tushare_request("daily_basic", {"trade_date": latest}, _BASIC_FIELDS)
    try:
        rows = _validated_market_rows(
            result, label=f"daily_basic({latest})", trade_date=latest,
            minimum=minimum["basic"], listed_symbols=listed_symbols,
        )
    except RuntimeError as exc:
        stats["errors"].append(str(exc))
    else:
        stats["basic_upserted"] = _upsert_daily_basic(rows)
    time.sleep(_API_SLEEP_SECONDS)

    if latest_rows is None:
        # 最新交易日本地已有 K 线（本轮未拉），仍需一次调用刷新全市场最新价
        result = _tushare_request("daily", {"trade_date": latest}, _DAILY_FIELDS)
        last_exc: RuntimeError | None = None
        for _attempt in range(_REQUEST_RETRIES):
            try:
                latest_rows = _validated_market_rows(
                    result, label=f"latest({latest})", trade_date=latest,
                    minimum=minimum["bar"], listed_symbols=listed_symbols,
                )
                break
            except RuntimeError as exc:
                last_exc = exc
                time.sleep(_API_SLEEP_SECONDS * (_attempt + 1))
                result = _tushare_request("daily", {"trade_date": latest}, _DAILY_FIELDS)
        else:
            if last_exc is not None:
                stats["errors"].append(str(last_exc))
    stats["latest_prices_upserted"] = _upsert_latest_prices(latest_rows or [], names)

    stats["ready"] = not stats["errors"]
    stats["elapsed_seconds"] = round(time.monotonic() - started, 1)
    logger.info(
        "market sync done: latest=%s dates_fetched=%d bars=%d adj=%d basic=%d prices=%d elapsed=%.1fs",
        latest,
        stats["dates_fetched"],
        stats["bars_upserted"],
        stats["adj_factors_upserted"],
        stats["basic_upserted"],
        stats["latest_prices_upserted"],
        stats["elapsed_seconds"],
    )
    return stats


def sync_status() -> dict:
    """本地同步水位（纯本地查询，不外呼）：供前端展示"数据截至哪天、多少只"。"""
    with SessionLocal() as db:
        latest_bar_date = _latest_local_date(db, DailyBar, date.today().strftime("%Y%m%d"))
        bar_symbols = 0
        if latest_bar_date:
            bar_symbols = int(
                db.execute(
                    select(func.count(func.distinct(DailyBar.symbol))).where(
                        DailyBar.source == "tushare",
                        DailyBar.trade_date.in_(_date_variants([latest_bar_date])),
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
