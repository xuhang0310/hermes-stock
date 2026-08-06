"""技术面选股器：在全市场候选池中按基础/量能/估值/均线条件选股。

单一职责：只做筛选条件的查询与计算——不外呼数据源、不写任何表、不做推荐解释。
全市场数据由 market/market_sync.py 同步（选股页"同步全市场"按钮触发）。

数据边界（诚实声明）：
- 候选池 = stock_latest_prices（同步后为最近交易日的全市场股票），返回体带
  universe（候选池大小）便于前端如实展示。
- 基础条件（市场/类型/价格区间/涨跌幅区间/关键词）与量能估值条件（换手率/官方
  量比/总市值/PE）纯 SQL 完成；量能估值列来自 stock_daily_basic（最新一日快照），
  设了该类条件而快照缺失的股票会被排除。总市值入参单位为**亿元**（表内存万元）。
- 技术条件（站上均线）需要 K 线：先在基础条件候选中按涨跌幅取前 _TECH_POOL_MAX
  名，再用窗口查询一次取回所需 K 线在 Python 内计算，控制成本。
"""
from __future__ import annotations

from sqlalchemy import and_, desc, func, select

from backend.app.core.database import SessionLocal
from backend.app.core.models import DailyBar, Instrument, StockDailyBasic, StockLatestPrice

_TECH_POOL_MAX = 300  # 技术条件二次筛选的候选上限，防止全表 K 线扫描
_LIST_LIMIT_MAX = 100
_VOLUME_RATIO_WINDOW = 5  # 量比 = 当日成交量 / 前 5 日均量
_ALLOWED_MA = (5, 10, 20)
_WAN_PER_YI = 10000.0  # total_mv 存万元，入参亿元换算


def _base_query(filters: dict):
    """基础 + 量能估值条件：最新价 JOIN instruments LEFT JOIN daily_basic 最新快照。"""
    latest_basic_date = select(func.max(StockDailyBasic.trade_date)).scalar_subquery()
    stmt = (
        select(StockLatestPrice, Instrument.market, Instrument.type, StockDailyBasic)
        .join(Instrument, Instrument.symbol == StockLatestPrice.symbol)
        .outerjoin(
            StockDailyBasic,
            and_(
                StockDailyBasic.symbol == StockLatestPrice.symbol,
                StockDailyBasic.trade_date == latest_basic_date,
            ),
        )
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
    # 量能估值条件（NULL 比较自然排除无快照的股票）
    if filters.get("turnover_rate_min") is not None:
        stmt = stmt.where(StockDailyBasic.turnover_rate >= filters["turnover_rate_min"])
    if filters.get("turnover_rate_max") is not None:
        stmt = stmt.where(StockDailyBasic.turnover_rate <= filters["turnover_rate_max"])
    if filters.get("volume_ratio_min") is not None:
        stmt = stmt.where(StockDailyBasic.volume_ratio >= filters["volume_ratio_min"])
    if filters.get("total_mv_min") is not None:
        stmt = stmt.where(StockDailyBasic.total_mv >= filters["total_mv_min"] * _WAN_PER_YI)
    if filters.get("total_mv_max") is not None:
        stmt = stmt.where(StockDailyBasic.total_mv <= filters["total_mv_max"] * _WAN_PER_YI)
    if filters.get("pe_min") is not None:
        stmt = stmt.where(StockDailyBasic.pe_ttm >= filters["pe_min"])
    if filters.get("pe_max") is not None:
        stmt = stmt.where(StockDailyBasic.pe_ttm <= filters["pe_max"])
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
    """量比 = 最新一根成交量 / 前 5 根均量；数据不足或均量为 0 返回 None。

    仅作 daily_basic 官方量比缺失时的展示回退，筛选走 SQL 官方量比。
    """
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
    turnover_rate_min: float | None = None,
    turnover_rate_max: float | None = None,
    total_mv_min: float | None = None,
    total_mv_max: float | None = None,
    pe_min: float | None = None,
    pe_max: float | None = None,
    above_ma: int | None = None,
    limit: int = 50,
) -> dict:
    """执行选股，返回 {universe, total, items}。

    市值入参单位为亿元；above_ma 仅支持 5/10/20；技术条件以"满足则保留、
    数据不足则剔除"处理。
    """
    if above_ma is not None and above_ma not in _ALLOWED_MA:
        raise ValueError(f"均线周期只支持 {_ALLOWED_MA}")
    limit = max(1, min(int(limit or 50), _LIST_LIMIT_MAX))
    need_tech = above_ma is not None
    filters = {
        "market": market,
        "stock_type": stock_type,
        "price_min": price_min,
        "price_max": price_max,
        "pct_chg_min": pct_chg_min,
        "pct_chg_max": pct_chg_max,
        "keyword": (keyword or "").strip() or None,
        "volume_ratio_min": volume_ratio_min,
        "turnover_rate_min": turnover_rate_min,
        "turnover_rate_max": turnover_rate_max,
        "total_mv_min": total_mv_min,
        "total_mv_max": total_mv_max,
        "pe_min": pe_min,
        "pe_max": pe_max,
    }

    with SessionLocal() as db:
        stmt = _base_query(filters).order_by(
            desc(StockLatestPrice.pct_chg), StockLatestPrice.symbol
        )
        universe = db.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar() or 0
        rows = db.execute(stmt.limit(_TECH_POOL_MAX if need_tech else limit)).all()

        items = []
        for price, market_value, _type, basic in rows:
            total_mv = getattr(basic, "total_mv", None) if basic is not None else None
            items.append(
                {
                    "symbol": price.symbol,
                    "name": price.name,
                    "market": market_value,
                    "price": price.price,
                    "pct_chg": price.pct_chg,
                    "trade_date": price.trade_date,
                    "volume_ratio": basic.volume_ratio if basic is not None else None,
                    "turnover_rate": basic.turnover_rate if basic is not None else None,
                    "total_mv_yi": round(total_mv / _WAN_PER_YI, 1) if total_mv else None,
                    "pe_ttm": basic.pe_ttm if basic is not None else None,
                    "ma_value": None,
                }
            )

        if need_tech and items:
            bars_needed = max(_VOLUME_RATIO_WINDOW + 1, above_ma or 0)
            bars_by_symbol = _fetch_recent_bars(db, [item["symbol"] for item in items], bars_needed)
            kept = []
            for item in items:
                bars = bars_by_symbol.get(item["symbol"], [])
                if above_ma is not None:
                    ma = _ma_value(bars, above_ma)
                    latest_close = bars[0]["close"] if bars else None
                    if ma is None or latest_close is None or latest_close <= ma:
                        continue
                    item["ma_value"] = ma
                if item["volume_ratio"] is None:
                    item["volume_ratio"] = _volume_ratio(bars)  # 官方缺失时的展示回退
                kept.append(item)
            items = kept[:limit]

    return {"universe": int(universe), "total": len(items), "items": items}
