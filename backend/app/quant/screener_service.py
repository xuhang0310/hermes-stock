"""技术面选股器：在本地已缓存股票池中按价格/涨跌/量能/均线条件选股。

单一职责：只做筛选条件的查询与计算——不外呼数据源、不写任何表、不做推荐解释。

数据边界（诚实声明）：
- 本地行情按需缓存（stock_latest_prices / daily_bars），选股器只在"本地已有缓存的
  股票"里筛选，不拉取全市场；返回体带 universe（候选池大小）便于前端展示。
- 基础条件（市场/类型/价格区间/涨跌幅区间/关键词）纯 SQL 完成。
- 技术条件（量比、站上均线）需要 K 线：先在基础条件候选中按涨跌幅取前
  _TECH_POOL_MAX 名，再用窗口查询一次取回所需 K 线在 Python 内计算，控制成本。
"""
from __future__ import annotations

from sqlalchemy import desc, func, select

from backend.app.core.database import SessionLocal
from backend.app.core.models import DailyBar, Instrument, StockLatestPrice

_TECH_POOL_MAX = 300  # 技术条件二次筛选的候选上限，防止全表 K 线扫描
_LIST_LIMIT_MAX = 100
_VOLUME_RATIO_WINDOW = 5  # 量比 = 当日成交量 / 前 5 日均量
_ALLOWED_MA = (5, 10, 20)


def _base_query(filters: dict):
    """基础条件：最新价缓存 JOIN instruments，返回 (query, 排序后的行)。"""
    stmt = (
        select(StockLatestPrice, Instrument.market, Instrument.type)
        .join(Instrument, Instrument.symbol == StockLatestPrice.symbol)
        .where(StockLatestPrice.price.isnot(None))
    )
    if filters.get("stock_type"):
        stmt = stmt.where(Instrument.type == filters["stock_type"])
    if filters.get("market"):
        stmt = stmt.where(Instrument.market == filters["market"])
    if filters.get("price_min") is not None:
        stmt = stmt.where(StockLatestPrice.price >= filters["price_min"])
    if filters.get("price_max") is not None:
        stmt = stmt.where(StockLatestPrice.price <= filters["price_max"])
    if filters.get("pct_chg_min") is not None:
        stmt = stmt.where(StockLatestPrice.pct_chg >= filters["pct_chg_min"])
    if filters.get("pct_chg_max") is not None:
        stmt = stmt.where(StockLatestPrice.pct_chg <= filters["pct_chg_max"])
    if filters.get("keyword"):
        like = f"%{filters['keyword']}%"
        stmt = stmt.where(
            StockLatestPrice.symbol.like(like) | StockLatestPrice.name.like(like)
        )
    return stmt


def _fetch_recent_bars(db, symbols: list[str], bars_per_symbol: int) -> dict[str, list[dict]]:
    """一次窗口查询取回各标的最近 bars_per_symbol 根 K 线（按交易日去重，新源优先）。

    trade_date 存在 20260805 / 2026-08-05 混存，排序与去重都先归一化成纯数字。
    """
    if not symbols:
        return {}
    normalized = func.replace(DailyBar.trade_date, "-", "").label("norm_date")
    ranked = (
        select(
            DailyBar.symbol.label("symbol"),
            normalized.label("norm_date"),
            DailyBar.close.label("close"),
            DailyBar.volume.label("volume"),
            func.row_number()
            .over(
                partition_by=DailyBar.symbol,
                order_by=(desc(normalized), desc(DailyBar.id)),
            )
            .label("rank"),
        )
        .where(DailyBar.symbol.in_(symbols))
        .subquery()
    )
    rows = (
        db.execute(select(ranked).where(ranked.c.rank <= bars_per_symbol))
        .mappings()
        .all()
    )
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        symbol = str(row["symbol"])
        grouped.setdefault(symbol, []).append(
            {
                "norm_date": str(row["norm_date"]),
                "close": row["close"],
                "volume": row["volume"],
            }
        )
    # 按日期倒序 + 同日只留第一根（rank 小 = id 新）
    for symbol, bars in grouped.items():
        seen: set[str] = set()
        deduped = []
        for bar in sorted(bars, key=lambda item: item["norm_date"], reverse=True):
            if bar["norm_date"] in seen:
                continue
            seen.add(bar["norm_date"])
            deduped.append(bar)
        grouped[symbol] = deduped
    return grouped


def _volume_ratio(bars: list[dict]) -> float | None:
    """量比 = 最新一根成交量 / 前 5 根均量；数据不足或均量为 0 返回 None。"""
    if len(bars) < _VOLUME_RATIO_WINDOW + 1:
        return None
    latest = bars[0]["volume"]
    history = [bar["volume"] for bar in bars[1 : _VOLUME_RATIO_WINDOW + 1]]
    if latest is None or any(volume is None for volume in history):
        return None
    avg = sum(history) / len(history)
    if avg <= 0:
        return None
    return round(latest / avg, 3)


def _ma_value(bars: list[dict], n: int) -> float | None:
    """最近 n 根收盘价均值（含当日）；不足 n 根返回 None。"""
    closes = [bar["close"] for bar in bars[:n]]
    if len(closes) < n or any(close is None for close in closes):
        return None
    return round(sum(closes) / n, 3)


def run_screener(
    *,
    market: str | None = None,
    stock_type: str | None = "stock",
    price_min: float | None = None,
    price_max: float | None = None,
    pct_chg_min: float | None = None,
    pct_chg_max: float | None = None,
    keyword: str | None = None,
    volume_ratio_min: float | None = None,
    above_ma: int | None = None,
    limit: int = 50,
) -> dict:
    """执行选股，返回 {universe, total, items}。

    above_ma 仅支持 5/10/20；技术条件以"满足则保留、数据不足则剔除"处理。
    """
    if above_ma is not None and above_ma not in _ALLOWED_MA:
        raise ValueError(f"均线周期只支持 {_ALLOWED_MA}")
    limit = max(1, min(int(limit or 50), _LIST_LIMIT_MAX))
    need_tech = volume_ratio_min is not None or above_ma is not None
    filters = {
        "market": market,
        "stock_type": stock_type,
        "price_min": price_min,
        "price_max": price_max,
        "pct_chg_min": pct_chg_min,
        "pct_chg_max": pct_chg_max,
        "keyword": (keyword or "").strip() or None,
    }

    with SessionLocal() as db:
        stmt = _base_query(filters).order_by(
            desc(StockLatestPrice.pct_chg), StockLatestPrice.symbol
        )
        universe = db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar() or 0
        rows = db.execute(stmt.limit(_TECH_POOL_MAX if need_tech else limit)).all()

        items = [
            {
                "symbol": price.symbol,
                "name": price.name,
                "market": market_value,
                "price": price.price,
                "pct_chg": price.pct_chg,
                "trade_date": price.trade_date,
                "volume_ratio": None,
                "ma_value": None,
            }
            for price, market_value, _type in rows
        ]

        if need_tech and items:
            bars_needed = max(_VOLUME_RATIO_WINDOW + 1, (above_ma or 0))
            bars_by_symbol = _fetch_recent_bars(db, [item["symbol"] for item in items], bars_needed)
            kept = []
            for item in items:
                bars = bars_by_symbol.get(item["symbol"], [])
                if volume_ratio_min is not None:
                    ratio = _volume_ratio(bars)
                    if ratio is None or ratio < volume_ratio_min:
                        continue
                    item["volume_ratio"] = ratio
                if above_ma is not None:
                    ma = _ma_value(bars, above_ma)
                    latest_close = bars[0]["close"] if bars else None
                    if (
                        ma is None
                        or latest_close is None
                        or latest_close <= ma
                    ):
                        continue
                    item["ma_value"] = ma
                kept.append(item)
            items = kept[:limit]

    return {"universe": int(universe), "total": len(items), "items": items}
