"""技术面选股器：在全市场候选池中按基础/量能/估值/均线条件选股。

单一职责：只做筛选条件的查询与计算——不外呼数据源、不写任何表、不做推荐解释。
全市场数据由 market/market_sync.py 同步（选股页"同步全市场"按钮触发）。

数据边界（诚实声明）：
- 候选池 = stock_latest_prices 中最近一次全市场截面上仍在交易的非 ST 股票：
  最新价日期早于截面日（停牌/退市）或名称带 ST 标记的不进入候选池。返回体
  universe 为候选池大小、total 为真实命中数，items 只是排序后的前 limit 只。
- 基础条件（市场/类型/价格区间/涨跌幅区间/关键词）与量能估值条件（换手率/官方
  量比/总市值/PE）纯 SQL 完成；量能估值列来自 stock_daily_basic（最新一日快照），
  设了该类条件而快照缺失的股票会被排除。总市值入参单位为**亿元**（表内存万元）。
- 技术条件（站上均线）对全部命中股票逐批取 K 线在 Python 内计算，不预先截断；
  均线按复权因子换算到最新价口径，避免除权缺口把股票误判为跌破均线。
- 排序与综合分在服务端对全部命中股票计算（分位数相对分），不是只在返回的
  前 N 只里重排。
"""
from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import and_, desc, func, not_, or_, select

from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    DailyBar,
    Instrument,
    Position,
    ScreenerPromptHistory,
    ScreenerSavedStrategy,
    ScreenerTemplateRecommendation,
    StockDailyBasic,
    StockLatestPrice,
    TushareFactorValue,
)
from backend.app.market.market_sync import load_local_adj_factors
from backend.app.quant import tushare_factor_service

_BAR_BATCH = 500  # K 线按标的分批取，避免超长 IN 列表
_BAR_LOOKBACK_DAYS = 90  # 均线窗口最多 20 根，90 个自然日足以跨过长假
_ST_NAME_PREFIXES = ("ST", "*ST", "S*ST", "SST")
# 排序方式 -> (字段, 是否降序)；与选股页结果表的排序选项一一对应
_SORT_FIELDS = {
    "score_desc": ("composite_score", True),
    "pct_desc": ("pct_chg", True),
    "turnover_desc": ("turnover_rate", True),
    "mv_asc": ("total_mv_yi", False),
    "pe_asc": ("pe_ttm", False),
}
_DEFAULT_SCORE_WEIGHTS = {"value": 40.0, "momentum": 40.0, "liquidity": 20.0}
_MISSING_SCORE = 45.0  # 字段缺失的分项按略低于中位处理，不让缺数据的股票占优
_LIST_LIMIT_MAX = 100
_VOLUME_RATIO_WINDOW = 5  # 量比 = 当日成交量 / 前 5 日均量
_ALLOWED_MA = (5, 10, 20)
_WAN_PER_YI = 10000.0  # total_mv 存万元，入参亿元换算
_TEMPLATE_TARGET_MIN = 20
_TEMPLATE_TARGET_MAX = 120


def _market_snapshot_date(db) -> str | None:
    """最近一次全市场截面日：stock_latest_prices 中出现次数最多的 trade_date。

    全市场同步一次写入 5000+ 行同日价格；盘中按需刷新只会把少数标的推到更新的
    日期，停牌股则停在更早的日期，所以取众数而不是 MAX。
    """
    row_count = func.count()
    row = db.execute(
        select(StockLatestPrice.trade_date, row_count)
        .where(StockLatestPrice.trade_date.isnot(None))
        .group_by(StockLatestPrice.trade_date)
        .order_by(row_count.desc(), StockLatestPrice.trade_date.desc())
        .limit(1)
    ).first()
    return str(row[0]) if row and row[0] else None


def _tradable_pool(stmt, snapshot_date: str | None):
    """候选池规则：剔除停牌/退市（最新价早于截面日）与 ST 类标的。"""
    if snapshot_date:
        stmt = stmt.where(StockLatestPrice.trade_date >= snapshot_date)
    name = func.coalesce(StockLatestPrice.name, "")
    return stmt.where(not_(or_(*(name.like(f"{prefix}%") for prefix in _ST_NAME_PREFIXES))))


def _base_query(filters: dict, snapshot_date: str | None, *, tradable_only: bool = True):
    """基础 + 量能估值条件：最新价 JOIN instruments LEFT JOIN daily_basic 最新快照。

    设了 ``factor_name`` 时再 INNER JOIN 该因子最新缓存截面（无缓存值的股票被剔除），
    并把 ``factor_value`` 追加进 select 列（行变为 5 元组）。``tradable_only`` 为假时
    不套候选池规则，仅用于统计被剔除的标的数。
    """
    has_factor = bool(filters.get("factor_name"))
    columns = [StockLatestPrice, Instrument.market, Instrument.type, StockDailyBasic]
    if has_factor:
        columns.append(TushareFactorValue.factor_value)
    latest_basic_date = select(func.max(StockDailyBasic.trade_date)).scalar_subquery()
    stmt = (
        select(*columns)
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
    if tradable_only:
        stmt = _tradable_pool(stmt, snapshot_date)
    if has_factor:
        latest_factor_date = (
            select(func.max(TushareFactorValue.trade_date))
            .where(TushareFactorValue.factor_name == filters["factor_name"])
            .scalar_subquery()
        )
        stmt = stmt.join(
            TushareFactorValue,
            and_(
                TushareFactorValue.symbol == StockLatestPrice.symbol,
                TushareFactorValue.factor_name == filters["factor_name"],
                TushareFactorValue.trade_date == latest_factor_date,
            ),
        )
        if filters.get("factor_min") is not None:
            stmt = stmt.where(TushareFactorValue.factor_value >= filters["factor_min"])
        if filters.get("factor_max") is not None:
            stmt = stmt.where(TushareFactorValue.factor_value <= filters["factor_max"])
    if filters.get("stock_type"):
        stmt = stmt.where(Instrument.type == filters["stock_type"])
    if filters.get("market"):
        stmt = stmt.where(Instrument.market == filters["market"])
    if filters.get("industry"):
        stmt = stmt.where(func.trim(Instrument.industry) == filters["industry"])
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


def _fetch_recent_bars(
    db, symbols: list[str], bars_per_symbol: int, *, since: str | None = None,
) -> dict[str, list[dict]]:
    """分批窗口查询取回各标的最近 bars_per_symbol 根 K 线（按交易日去重，新源优先）。

    trade_date 存在 20260805 / 2026-08-05 混存，排序与去重都先归一化成纯数字。
    ``since``（YYYYMMDD）限定回看起点，避免对长历史标的整段开窗。每根 K 线附带
    同日复权因子 ``adj_factor``（本地缺失为 None）。
    """
    if not symbols:
        return {}
    normalized = func.replace(DailyBar.trade_date, "-", "").label("norm_date")
    rows = []
    for start in range(0, len(symbols), _BAR_BATCH):
        query = select(
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
        ).where(DailyBar.symbol.in_(symbols[start : start + _BAR_BATCH]))
        if since:
            query = query.where(func.replace(DailyBar.trade_date, "-", "") >= since)
        ranked = query.subquery()
        rows.extend(
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
    factors = load_local_adj_factors(
        db, list(grouped), {bar["norm_date"] for bars in grouped.values() for bar in bars},
    )
    for symbol, bars in grouped.items():
        for bar in bars:
            bar["adj_factor"] = factors.get((symbol, bar["norm_date"]))
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


def _ma_adjusted(bars: list[dict], n: int) -> bool:
    """最近 n 根 K 线是否都有复权因子（均线可换算到同一价格口径）。"""
    window = bars[:n]
    return len(window) == n and all(bar.get("adj_factor") for bar in window)


def _ma_value(bars: list[dict], n: int) -> float | None:
    """最近 n 根收盘价均值（含当日），按最新一根的价格口径；不足 n 根返回 None。

    窗口内都有复权因子时用 close × 因子 / 最新因子（前复权）计算，避免送转除权后
    的旧高价把均线抬高；缺因子时退回不复权收盘价（由 _ma_adjusted 标明）。
    """
    window = bars[:n]
    closes = [bar["close"] for bar in window]
    if len(closes) < n or any(close is None for close in closes):
        return None
    if _ma_adjusted(bars, n):
        latest_factor = window[0]["adj_factor"]
        closes = [close * bar["adj_factor"] / latest_factor for close, bar in zip(closes, window)]
    return round(sum(closes) / n, 3)


def _validate_factor_filter(db, filters: dict) -> str | None:
    """校验因子条件并返回因子快照日期；因子不存在/未缓存时抛 ValueError。"""
    factor_name = filters.get("factor_name")
    if not factor_name:
        return None
    if factor_name not in tushare_factor_service.known_factor_names():
        raise ValueError(f"TuShare 因子库中不存在因子：{factor_name}")
    factor_as_of = db.scalar(
        select(func.max(TushareFactorValue.trade_date)).where(
            TushareFactorValue.factor_name == factor_name
        )
    )
    if not factor_as_of:
        raise ValueError(f"因子 {factor_name} 的值尚未同步，请先在因子分析页同步该因子的因子值")
    return str(factor_as_of)


def run_screener(
    *,
    market: str | None = None,
    industry: str | None = None,
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
    factor_name: str | None = None,
    factor_min: float | None = None,
    factor_max: float | None = None,
    limit: int = 50,
    sort_by: str = "score_desc",
    score_weights: dict[str, float] | None = None,
) -> dict:
    """执行选股，返回 {universe, total, items, factor_as_of, as_of, excluded_count}。

    市值入参单位为亿元；above_ma 仅支持 5/10/20；技术条件以"满足则保留、
    数据不足则剔除"处理。因子条件作用于 TuShare 因子库最新缓存截面。
    全部命中股票先算综合分再按 ``sort_by`` 排序，items 取前 limit 只。
    """
    if above_ma is not None and above_ma not in _ALLOWED_MA:
        raise ValueError(f"均线周期只支持 {_ALLOWED_MA}")
    if sort_by not in _SORT_FIELDS:
        raise ValueError(f"排序方式只支持 {tuple(_SORT_FIELDS)}")
    weights = _normalized_score_weights(score_weights)
    limit = max(1, min(int(limit or 50), _LIST_LIMIT_MAX))
    need_tech = above_ma is not None
    has_factor = bool((factor_name or "").strip())
    filters = {
        "market": market,
        "industry": (industry or "").strip() or None,
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
        "factor_name": (factor_name or "").strip() or None,
        "factor_min": factor_min,
        "factor_max": factor_max,
    }

    with SessionLocal() as db:
        factor_as_of = _validate_factor_filter(db, filters)
        snapshot_date = _market_snapshot_date(db)
        pool_filters = {"stock_type": stock_type}
        universe = _count(db, _base_query(pool_filters, snapshot_date))
        excluded_count = _count(db, _base_query(pool_filters, snapshot_date, tradable_only=False)) - universe
        rows = db.execute(_base_query(filters, snapshot_date)).all()

        items = []
        for row in rows:
            price, market_value, _type, basic = row[0], row[1], row[2], row[3]
            total_mv = getattr(basic, "total_mv", None) if basic is not None else None
            item = {
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
            if has_factor:
                item["factor_value"] = row[4]
            items.append(item)

        if need_tech and items:
            bars_needed = max(_VOLUME_RATIO_WINDOW + 1, above_ma or 0)
            bars_by_symbol = _fetch_recent_bars(
                db, [item["symbol"] for item in items], bars_needed,
                since=_lookback_start(snapshot_date),
            )
            kept = []
            for item in items:
                bars = bars_by_symbol.get(item["symbol"], [])
                if above_ma is not None:
                    ma = _ma_value(bars, above_ma)
                    latest_close = bars[0]["close"] if bars else None
                    if ma is None or latest_close is None or latest_close <= ma:
                        continue
                    item["ma_value"] = ma
                    item["ma_adjusted"] = _ma_adjusted(bars, above_ma)
                if item["volume_ratio"] is None:
                    item["volume_ratio"] = _volume_ratio(bars)  # 官方缺失时的展示回退
                kept.append(item)
            items = kept

    _score_items(items, weights)
    _sort_items(items, sort_by)
    return {
        "universe": universe,
        "total": len(items),
        "items": items[:limit],
        "factor_as_of": factor_as_of,
        "as_of": snapshot_date,
        "excluded_count": excluded_count,
    }


def _count(db, stmt) -> int:
    return int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0)


def _lookback_start(snapshot_date: str | None) -> str | None:
    try:
        anchor = datetime.strptime(str(snapshot_date), "%Y%m%d")
    except ValueError:
        return None
    return (anchor - timedelta(days=_BAR_LOOKBACK_DAYS)).strftime("%Y%m%d")


def _normalized_score_weights(score_weights: dict[str, float] | None) -> dict[str, float]:
    weights = dict(_DEFAULT_SCORE_WEIGHTS)
    for key, value in (score_weights or {}).items():
        if key not in weights:
            raise ValueError(f"未知评分维度：{key}")
        number = float(value)
        if not 0 <= number <= 100:
            raise ValueError("评分权重必须介于 0 和 100")
        weights[key] = number
    return weights


def _percentile_scores(values: list[float | None], *, inverse: bool = False) -> list[float]:
    """全部命中股票内的分位相对分（0-100，并列取平均名次）；缺失记 _MISSING_SCORE。

    用分位而不是 min-max：全市场 PE/市值的极端值会把 min-max 分数压成一团。
    """
    present = sorted(value for value in values if value is not None)
    span = len(present) - 1
    scores = []
    for value in values:
        if value is None:
            scores.append(_MISSING_SCORE)
        elif span == 0 or present[0] == present[-1]:
            scores.append(75.0)
        else:
            rank = (bisect_left(present, value) + bisect_right(present, value) - 1) / 2
            score = rank / span * 100
            scores.append(100 - score if inverse else score)
    return scores


def _score_items(items: list[dict], weights: dict[str, float]) -> None:
    """估值规模 / 动量 / 流动性三维相对分与加权综合分（口径与选股页评分卡一致）。"""

    def column(key: str, *, positive_only: bool = False) -> list[float | None]:
        values = []
        for item in items:
            value = item.get(key)
            values.append(None if value is None or (positive_only and value <= 0) else float(value))
        return values

    pe = _percentile_scores(column("pe_ttm", positive_only=True), inverse=True)
    size = _percentile_scores(column("total_mv_yi", positive_only=True), inverse=True)
    momentum = _percentile_scores(column("pct_chg"))
    volume = _percentile_scores(column("volume_ratio"))
    turnover = _percentile_scores(column("turnover_rate"))
    total_weight = sum(weights.values()) or 100.0
    for index, item in enumerate(items):
        value_score = pe[index] * 0.55 + size[index] * 0.45
        liquidity_score = volume[index] * 0.45 + turnover[index] * 0.55
        composite = (
            value_score * weights["value"]
            + momentum[index] * weights["momentum"]
            + liquidity_score * weights["liquidity"]
        ) / total_weight
        item["value_score"] = round(value_score, 1)
        item["momentum_score"] = round(momentum[index], 1)
        item["liquidity_score"] = round(liquidity_score, 1)
        item["composite_score"] = round(composite, 1)


def _sort_items(items: list[dict], sort_by: str) -> None:
    """缺值排在最后；PE 升序时亏损股（PE≤0）同样视为缺值；并列按代码。"""
    field, descending = _SORT_FIELDS[sort_by]

    def key(item: dict) -> tuple:
        value = item.get(field)
        if value is None or (sort_by == "pe_asc" and value <= 0):
            return (1, 0.0, item["symbol"])
        return (0, -float(value) if descending else float(value), item["symbol"])

    items.sort(key=key)


def count_screener_candidates(query: dict) -> int:
    """Count the SQL-filtered candidate pool for a template.

    ``above_ma`` is intentionally excluded here. The returned value is an upper
    bound before the moving-average check, so a template kept below the target
    can never expand beyond it after technical filtering. Factor conditions are
    pure SQL, so they ARE included, and so are the ST / suspension pool rules.
    """
    filters = {
        "market": query.get("market"),
        "industry": (query.get("industry") or "").strip() or None,
        "stock_type": query.get("stock_type") or "stock",
        "price_min": query.get("price_min"),
        "price_max": query.get("price_max"),
        "pct_chg_min": query.get("pct_chg_min"),
        "pct_chg_max": query.get("pct_chg_max"),
        "keyword": (query.get("keyword") or "").strip() or None,
        "volume_ratio_min": query.get("volume_ratio_min"),
        "turnover_rate_min": query.get("turnover_rate_min"),
        "turnover_rate_max": query.get("turnover_rate_max"),
        "total_mv_min": query.get("total_mv_min"),
        "total_mv_max": query.get("total_mv_max"),
        "pe_min": query.get("pe_min"),
        "pe_max": query.get("pe_max"),
        "factor_name": (query.get("factor_name") or "").strip() or None,
        "factor_min": query.get("factor_min"),
        "factor_max": query.get("factor_max"),
    }
    with SessionLocal() as db:
        _validate_factor_filter(db, filters)
        return _count(db, _base_query(filters, _market_snapshot_date(db)))


def _band(values: list) -> dict | None:
    """min/median/max 分布带，供 LLM 参照持仓实际分布设计条件区间。"""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return {"min": round(min(vals), 2), "median": round(median(vals), 2), "max": round(max(vals), 2)}


def _stat_median(facts: dict, key: str, fallback: float) -> float:
    try:
        value = float(((facts.get("stats") or {}).get(key) or {}).get("median"))
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _range(low: float, high: float, *, floor: float = 0.0) -> tuple[float, float]:
    low = max(floor, float(low))
    high = max(low + 0.01, float(high))
    return round(low, 2), round(high, 2)


def position_profile_summary(facts: dict) -> str:
    """Build a factual, reproducible holding profile without an LLM."""
    count = max(0, int(facts.get("position_count") or 0))
    stats = facts.get("stats") or {}
    mv = _stat_median(facts, "total_mv_yi", 0)
    turnover = _stat_median(facts, "turnover_rate", 0)
    pe = _stat_median(facts, "pe_ttm", 0)
    above = max(0, int(stats.get("above_ma20_count") or 0))
    parts = []
    if mv:
        parts.append(f"持仓中位市值约 {mv:g} 亿元")
    if turnover:
        parts.append(f"中位换手率约 {turnover:g}%")
    if pe:
        parts.append(f"可用 PE 中位数约 {pe:g} 倍")
    if count:
        parts.append(f"{above}/{count} 只站上 20 日线")
    facts_line = "、".join(parts) or "已读取当前持仓与可用行情"
    return (
        f"{facts_line}。下方从风格延续、趋势强化、估值优化、风险平衡、弹性扩展等十个方向给出可切换的执行策略。"
    )


def build_progression_templates(facts: dict) -> list[dict]:
    """Turn holding medians into ten distinct, bounded next-step strategies."""
    price = _stat_median(facts, "price", 20.0)
    mv = _stat_median(facts, "total_mv_yi", 100.0)
    turnover = _stat_median(facts, "turnover_rate", 2.0)
    pe = _stat_median(facts, "pe_ttm", 35.0)
    pct = _stat_median(facts, "pct_chg", 0.5)

    def query(
        *,
        price_band: tuple[float, float],
        mv_band: tuple[float, float],
        turnover_band: tuple[float, float],
        pct_band: tuple[float, float],
        pe_band: tuple[float, float],
        above_ma: int = 20,
    ) -> dict:
        return {
            "price_min": price_band[0],
            "price_max": price_band[1],
            "total_mv_min": mv_band[0],
            "total_mv_max": mv_band[1],
            "turnover_rate_min": turnover_band[0],
            "turnover_rate_max": turnover_band[1],
            "pct_chg_min": pct_band[0],
            "pct_chg_max": pct_band[1],
            "pe_min": pe_band[0],
            "pe_max": pe_band[1],
            "above_ma": above_ma,
        }

    common = {
        "target_min": _TEMPLATE_TARGET_MIN,
        "target_max": _TEMPLATE_TARGET_MAX,
    }
    templates = [
        {
            **common,
            "stage": "风格延续",
            "name": "核心风格精选",
            "objective": "沿用当前持仓的市值、活跃度和趋势特征，只保留更接近核心画像的标的。",
            "rationale": f"围绕持仓中位市值 {mv:g} 亿和中位换手率 {turnover:g}% 收紧，不再做宽泛相似匹配。",
            "query": query(
                price_band=_range(price * 0.4, price * 1.5, floor=1),
                mv_band=_range(mv * 0.55, mv * 1.35, floor=10),
                turnover_band=_range(turnover * 0.4, turnover * 1.2, floor=0.2),
                pct_band=_range(pct - 1.25, pct + 1.75, floor=-10),
                pe_band=_range(pe * 0.25, pe * 1.05, floor=1),
            ),
        },
        {
            **common,
            "stage": "趋势加强",
            "name": "量价趋势确认",
            "objective": "在相近盘子里继续提高换手与涨幅要求，寻找趋势和交易活跃度同时确认的标的。",
            "rationale": f"当前 {int((facts.get('stats') or {}).get('above_ma20_count') or 0)} 只持仓站上 20 日线，进一步要求温和上涨而非追涨停。",
            "query": query(
                price_band=_range(price * 0.5, price * 2.0, floor=1),
                mv_band=_range(mv * 0.4, mv * 1.35, floor=10),
                turnover_band=_range(turnover * 0.7, turnover * 1.9, floor=0.5),
                pct_band=_range(max(0.3, pct - 0.25), pct + 2.25, floor=-10),
                pe_band=_range(pe * 0.25, pe * 1.35, floor=1),
            ),
        },
        {
            **common,
            "stage": "估值优化",
            "name": "同盘估值收敛",
            "objective": "保留相近市值和交易特征，同时主动降低估值上限，补充更有安全边际的候选。",
            "rationale": f"持仓可用 PE 中位数约 {pe:g} 倍，本策略把估值区间压到持仓中位数以下。",
            "query": query(
                price_band=_range(price * 0.3, price * 1.5, floor=1),
                mv_band=_range(mv * 0.67, mv * 2.0, floor=10),
                turnover_band=_range(turnover * 0.25, turnover * 0.95, floor=0.2),
                pct_band=_range(pct - 1.8, pct + 0.7, floor=-10),
                pe_band=_range(max(5, pe * 0.15), max(20, pe * 0.45), floor=1),
            ),
        },
        {
            **common,
            "stage": "风险分散",
            "name": "稳健大盘补位",
            "objective": "用更大市值、更低换手和更低估值的标的，平衡当前偏中小盘、偏活跃的持仓结构。",
            "rationale": "这不是复制现有持仓，而是补充与当前风格相关性更低的观察方向。",
            "query": query(
                price_band=_range(price * 0.25, price * 2.0, floor=1),
                mv_band=_range(max(100, mv * 3.35), max(300, mv * 10.7), floor=100),
                turnover_band=_range(turnover * 0.2, turnover * 0.72, floor=0.2),
                pct_band=_range(-1.0, 1.5, floor=-10),
                pe_band=_range(5, min(40, max(20, pe * 0.45)), floor=1),
            ),
        },
        {
            **common,
            "stage": "回调观察",
            "name": "趋势回踩观察",
            "objective": "寻找仍在 20 日线上、但当日涨幅有限的标的，避免只追逐已经加速上涨的股票。",
            "rationale": "保留趋势条件，同时把当日涨跌幅压到低位区间，作为后续观察池而不是追高池。",
            "query": query(
                price_band=_range(price * 0.4, price * 1.5, floor=1),
                mv_band=_range(mv * 0.47, mv * 1.35, floor=10),
                turnover_band=_range(turnover * 0.5, turnover * 1.43, floor=0.2),
                pct_band=_range(pct - 1.8, pct - 0.25, floor=-10),
                pe_band=_range(pe * 0.25, pe * 1.05, floor=1),
            ),
        },
        {
            **common,
            "stage": "弹性扩展",
            "name": "小盘弹性增强",
            "objective": "把市值带下移，在更小盘的股票中寻找换手充分、涨幅受控的弹性候选。",
            "rationale": "与核心风格相比更强调小市值和交易弹性，但用涨幅上限与估值区间避免无边界追高。",
            "query": query(
                price_band=_range(price * 0.25, price * 1.2, floor=1),
                mv_band=_range(mv * 0.25, mv * 0.75, floor=10),
                turnover_band=_range(turnover * 0.8, turnover * 2.5, floor=0.5),
                pct_band=_range(max(0.5, pct - 2.25), pct + 1.75, floor=-10),
                pe_band=_range(pe * 0.2, pe * 1.2, floor=1),
                above_ma=10,
            ),
        },
        {
            **common,
            "stage": "规模升级",
            "name": "中盘趋势接力",
            "objective": "把市值带上移到中盘区间，寻找趋势仍强、流动性适中且估值没有失控的接力标的。",
            "rationale": "用于降低组合对小盘风格的单一依赖，同时保留当前持仓偏好的趋势与活跃特征。",
            "query": query(
                price_band=_range(price * 0.5, price * 2.2, floor=1),
                mv_band=_range(mv * 1.2, mv * 4.0, floor=50),
                turnover_band=_range(turnover * 0.5, turnover * 1.6, floor=0.3),
                pct_band=_range(max(0.5, pct - 1.75), pct + 1.75, floor=-10),
                pe_band=_range(pe * 0.2, pe * 0.9, floor=1),
            ),
        },
        {
            **common,
            "stage": "波动缓冲",
            "name": "低波动防守层",
            "objective": "用更大市值、较低换手和窄幅涨跌的标的，为当前偏活跃组合增加防守层。",
            "rationale": "该方向主动牺牲部分弹性，换取更低交易热度与更可控估值，适合作为组合平衡候选。",
            "query": query(
                price_band=_range(price * 0.4, price * 2.5, floor=1),
                mv_band=_range(mv * 2.0, mv * 8.0, floor=100),
                turnover_band=_range(turnover * 0.15, turnover * 0.6, floor=0.2),
                pct_band=_range(-0.8, 1.2, floor=-10),
                pe_band=_range(5, min(35, max(20, pe * 0.55)), floor=1),
            ),
        },
        {
            **common,
            "stage": "热度回落",
            "name": "高换手降温观察",
            "objective": "保留较高换手，但把当日涨幅限制在低位，寻找活跃资金尚在、价格暂未加速的标的。",
            "rationale": "区别于强势追涨，这一组更关注活跃后的横盘与降温阶段，适合进入观察池继续确认。",
            "query": query(
                price_band=_range(price * 0.3, price * 1.7, floor=1),
                mv_band=_range(mv * 0.5, mv * 2.0, floor=10),
                turnover_band=_range(turnover, turnover * 2.5, floor=0.5),
                pct_band=_range(-0.5, 1.5, floor=-10),
                pe_band=_range(pe * 0.2, pe * 1.2, floor=1),
                above_ma=10,
            ),
        },
        {
            **common,
            "stage": "突破预备",
            "name": "短线突破预备",
            "objective": "筛选站上 5 日线、温和上涨且换手开始抬升的标的，作为短线突破前的预备观察池。",
            "rationale": "比趋势确认更早介入，但仍设置涨幅、估值和市值边界，避免把已大涨标的混入预备池。",
            "query": query(
                price_band=_range(price * 0.4, price * 1.8, floor=1),
                mv_band=_range(mv * 0.4, mv * 1.5, floor=10),
                turnover_band=_range(turnover * 0.7, turnover * 2.0, floor=0.5),
                pct_band=_range(0, 2.0, floor=-10),
                pe_band=_range(pe * 0.2, pe * 1.1, floor=1),
                above_ma=5,
            ),
        },
    ]
    return templates


def _scale_query_ranges(query: dict, scale: float) -> dict:
    adjusted = dict(query)
    lower_floors = {
        "price_min": 0.01,
        "turnover_rate_min": 0.0,
        "total_mv_min": 0.01,
        "pe_min": 1.0,
        "pct_chg_min": -20.0,
    }
    for low_key, high_key in (
        ("total_mv_min", "total_mv_max"),
        ("price_min", "price_max"),
        ("turnover_rate_min", "turnover_rate_max"),
        ("pct_chg_min", "pct_chg_max"),
        ("pe_min", "pe_max"),
    ):
        low = adjusted.get(low_key)
        high = adjusted.get(high_key)
        if low is None or high is None:
            continue
        center = (float(low) + float(high)) / 2
        half = max(0.01, (float(high) - float(low)) * scale / 2)
        adjusted[low_key] = round(max(lower_floors[low_key], center - half), 2)
        adjusted[high_key] = round(center + half, 2)
    return adjusted


def refine_progression_template(
    template: dict,
    count_fn: Callable[[dict], int],
    *,
    target_min: int = _TEMPLATE_TARGET_MIN,
    target_max: int = _TEMPLATE_TARGET_MAX,
) -> dict:
    """Run candidate counts and choose the tightest useful bounded variant."""
    base_query = dict(template.get("query") or {})
    variants: list[tuple[dict, int, float]] = []
    base_count = max(0, int(count_fn(base_query) or 0))
    variants.append((base_query, base_count, 1.0))
    if base_count > target_max:
        for scale in (0.85, 0.7, 0.55, 0.4):
            candidate = _scale_query_ranges(base_query, scale)
            variants.append((candidate, max(0, int(count_fn(candidate) or 0)), scale))
    elif 0 < base_count < target_min:
        for scale in (1.2, 1.45, 1.75, 2.1):
            candidate = _scale_query_ranges(base_query, scale)
            variants.append((candidate, max(0, int(count_fn(candidate) or 0)), scale))

    preferred = (target_min + target_max) / 2

    def score(item: tuple[dict, int, float]) -> tuple[float, float]:
        _query, count, _scale = item
        if target_min <= count <= target_max:
            return (0, abs(count - preferred))
        if count > target_max:
            return (1, count - target_max)
        if count > 0:
            return (2, target_min - count)
        return (3, float("inf"))

    selected_query, universe, scale = min(variants, key=score)
    out = dict(template)
    out["query"] = selected_query
    out["universe"] = universe
    out["target_min"] = target_min
    out["target_max"] = target_max
    out["condition_count"] = len(
        [value for key, value in selected_query.items() if key != "limit" and value is not None]
    )
    if scale < 1:
        out["tightening_notes"] = ["候选过多，已自动收紧市值、价格、换手、涨跌幅和估值区间"]
    elif scale > 1:
        out["tightening_notes"] = ["候选过少，已适度放宽数值区间并保留核心方向"]
    else:
        out["tightening_notes"] = []
    return out


def position_facts(user_id: int) -> dict:
    """汇总用户持仓风格事实：逐只特征 + 聚合分布（纯本地读取，不外呼）。

    无持仓抛 ValueError（API 层转 400）。特征尽量复用已有数据：
    最新价/涨跌幅取 stock_latest_prices，换手率/量比/市值/PE 取最新 daily_basic
    快照，是否站上 20 日线用本地 K 线计算。
    """
    with SessionLocal() as db:
        pos_rows = db.execute(
            select(
                Position.symbol,
                Instrument.name,
                func.max(Position.position_pct),
                func.count(),
            )
            .join(Instrument, Instrument.symbol == Position.symbol)
            .where(Position.owner_user_id == int(user_id), Position.status == "open")
            .group_by(Position.symbol, Instrument.name)
        ).all()
        if not pos_rows:
            raise ValueError("暂无持仓，请先在持仓页录入持仓后再生成智能模板")
        symbols = [row[0] for row in pos_rows]
        name_by_symbol = {row[0]: (row[1] or row[0]) for row in pos_rows}
        pct_by_symbol = {row[0]: float(row[2] or 0.0) for row in pos_rows}

        latest_basic_date = select(func.max(StockDailyBasic.trade_date)).scalar_subquery()
        feat_rows = db.execute(
            select(StockLatestPrice, StockDailyBasic)
            .outerjoin(
                StockDailyBasic,
                and_(
                    StockDailyBasic.symbol == StockLatestPrice.symbol,
                    StockDailyBasic.trade_date == latest_basic_date,
                ),
            )
            .where(StockLatestPrice.symbol.in_(symbols))
        ).all()
        bars_by_symbol = _fetch_recent_bars(db, symbols, 20)

    feat_by_symbol: dict[str, dict] = {}
    for price, basic in feat_rows:
        total_mv = getattr(basic, "total_mv", None) if basic is not None else None
        bars = bars_by_symbol.get(price.symbol, [])
        ma20 = _ma_value(bars, 20)
        latest_close = bars[0]["close"] if bars else price.price
        feat_by_symbol[price.symbol] = {
            "symbol": price.symbol,
            "name": name_by_symbol.get(price.symbol) or price.name or price.symbol,
            "position_pct": round(pct_by_symbol.get(price.symbol, 0.0), 2),
            "price": price.price,
            "pct_chg": price.pct_chg,
            "turnover_rate": basic.turnover_rate if basic is not None else None,
            "volume_ratio": basic.volume_ratio if basic is not None else None,
            "total_mv_yi": round(total_mv / _WAN_PER_YI, 1) if total_mv else None,
            "pe_ttm": basic.pe_ttm if basic is not None else None,
            "above_ma20": bool(ma20 is not None and latest_close is not None and latest_close > ma20),
        }

    positions = [feat_by_symbol[symbol] for symbol in symbols if symbol in feat_by_symbol]
    above_ma20_count = sum(1 for p in positions if p["above_ma20"])
    stats = {
        "price": _band([p["price"] for p in positions]),
        "total_mv_yi": _band([p["total_mv_yi"] for p in positions]),
        "turnover_rate": _band([p["turnover_rate"] for p in positions]),
        "pe_ttm": _band([p["pe_ttm"] for p in positions]),
        "pct_chg": _band([p["pct_chg"] for p in positions]),
        "above_ma20_count": above_ma20_count,
    }
    return {"position_count": len(positions), "positions": positions, "stats": stats}


def load_template_recommendation(user_id: int) -> dict | None:
    """读取该用户最近一次智能模板推荐；无记录返回 None。"""
    with SessionLocal() as db:
        row = db.execute(
            select(ScreenerTemplateRecommendation).where(
                ScreenerTemplateRecommendation.user_id == int(user_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        try:
            templates = json.loads(row.templates_json or "[]")
        except json.JSONDecodeError:
            templates = []
        return {
            "templates": templates if isinstance(templates, list) else [],
            "basis": row.basis or "",
            "position_count": row.position_count or 0,
            "model": row.model,
            "generated_at": row.updated_at.strftime("%Y-%m-%d %H:%M") if row.updated_at else None,
        }


def save_template_recommendation(user_id: int, payload: dict) -> str:
    """整行覆盖式保存（每用户一行），返回生成时间显示串。"""
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    with SessionLocal() as db:
        row = db.execute(
            select(ScreenerTemplateRecommendation).where(
                ScreenerTemplateRecommendation.user_id == int(user_id)
            )
        ).scalar_one_or_none()
        if row is None:
            row = ScreenerTemplateRecommendation(user_id=int(user_id), templates_json="[]")
            db.add(row)
        row.templates_json = json.dumps(payload.get("templates") or [], ensure_ascii=False)
        row.basis = str(payload.get("basis") or "")
        row.position_count = int(payload.get("position_count") or 0)
        row.model = payload.get("model")
        db.commit()
    return generated_at


def _normalize_prompt(text: str) -> str:
    """提示词去重口径：去首尾空白并压缩连续空白，保留原始中文标点。"""
    return " ".join(str(text or "").strip().split())[:500]


def _prompt_history_dict(row: ScreenerPromptHistory) -> dict:
    try:
        parsed_query = json.loads(row.parsed_query_json or "{}")
    except json.JSONDecodeError:
        parsed_query = {}
    return {
        "id": row.id,
        "text": row.prompt_text,
        "parsed_query": parsed_query if isinstance(parsed_query, dict) else {},
        "parse_summary": row.parse_summary or "",
        "last_result_count": row.last_result_count,
        "use_count": row.use_count or 0,
        "is_favorite": bool(row.is_favorite),
        "created_at": row.created_at.isoformat(timespec="minutes") if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat(timespec="minutes") if row.last_used_at else None,
    }


def record_prompt_history(
    user_id: int,
    text: str,
    *,
    parsed_query: dict | None = None,
    parse_summary: str | None = None,
) -> dict:
    """自动保存一次提示词使用记录；相同提示词只递增次数并刷新最近使用时间。"""
    normalized = _normalize_prompt(text)
    if not normalized:
        raise ValueError("提示词不能为空")
    prompt_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    now = datetime.now()
    with SessionLocal() as db:
        row = db.execute(
            select(ScreenerPromptHistory).where(
                ScreenerPromptHistory.user_id == int(user_id),
                ScreenerPromptHistory.prompt_hash == prompt_hash,
            )
        ).scalar_one_or_none()
        if row is None:
            row = ScreenerPromptHistory(
                user_id=int(user_id),
                prompt_hash=prompt_hash,
                prompt_text=normalized,
                use_count=1,
                last_used_at=now,
            )
            db.add(row)
        else:
            row.prompt_text = normalized
            row.use_count = int(row.use_count or 0) + 1
            row.last_used_at = now
        if parsed_query is not None:
            row.parsed_query_json = json.dumps(parsed_query, ensure_ascii=False)
        if parse_summary is not None:
            row.parse_summary = str(parse_summary)[:1000]
        db.commit()
        db.refresh(row)
        return _prompt_history_dict(row)


def list_prompt_history(
    user_id: int,
    *,
    limit: int = 50,
    search: str | None = None,
    favorite_only: bool = False,
) -> dict:
    """读取当前用户最近使用的提示词；支持服务端搜索与只看收藏。"""
    limit = max(1, min(int(limit or 50), 100))
    with SessionLocal() as db:
        stmt = select(ScreenerPromptHistory).where(
            ScreenerPromptHistory.user_id == int(user_id)
        )
        keyword = _normalize_prompt(search or "")
        if keyword:
            stmt = stmt.where(ScreenerPromptHistory.prompt_text.like(f"%{keyword}%"))
        if favorite_only:
            stmt = stmt.where(ScreenerPromptHistory.is_favorite.is_(True))
        rows = db.execute(
            stmt.order_by(
                ScreenerPromptHistory.last_used_at.desc(),
                ScreenerPromptHistory.id.desc(),
            ).limit(limit)
        ).scalars().all()
        total = db.execute(
            select(func.count()).select_from(
                select(ScreenerPromptHistory.id).where(
                    ScreenerPromptHistory.user_id == int(user_id)
                ).subquery()
            )
        ).scalar() or 0
    return {"items": [_prompt_history_dict(row) for row in rows], "total": int(total)}


def set_prompt_favorite(user_id: int, history_id: int, is_favorite: bool) -> dict:
    with SessionLocal() as db:
        row = db.execute(
            select(ScreenerPromptHistory).where(
                ScreenerPromptHistory.id == int(history_id),
                ScreenerPromptHistory.user_id == int(user_id),
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError("提示词历史不存在")
        row.is_favorite = bool(is_favorite)
        db.commit()
        db.refresh(row)
        return _prompt_history_dict(row)


def update_prompt_result_count(user_id: int, history_id: int, result_count: int) -> dict:
    with SessionLocal() as db:
        row = db.execute(
            select(ScreenerPromptHistory).where(
                ScreenerPromptHistory.id == int(history_id),
                ScreenerPromptHistory.user_id == int(user_id),
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError("提示词历史不存在")
        row.last_result_count = max(0, int(result_count))
        db.commit()
        db.refresh(row)
        return _prompt_history_dict(row)


def _saved_strategy_dict(row: ScreenerSavedStrategy) -> dict:
    try:
        query = json.loads(row.query_json or "{}")
    except json.JSONDecodeError:
        query = {}
    return {
        "id": row.id,
        "name": row.name,
        "query": query if isinstance(query, dict) else {},
        "last_result_count": row.last_result_count,
        "created_at": row.created_at.isoformat(timespec="minutes") if row.created_at else None,
        "updated_at": row.updated_at.isoformat(timespec="minutes") if row.updated_at else None,
    }


def list_saved_strategies(user_id: int) -> list[dict]:
    with SessionLocal() as db:
        rows = db.execute(
            select(ScreenerSavedStrategy)
            .where(ScreenerSavedStrategy.user_id == int(user_id))
            .order_by(ScreenerSavedStrategy.updated_at.desc(), ScreenerSavedStrategy.id.desc())
        ).scalars().all()
        return [_saved_strategy_dict(row) for row in rows]


def save_strategy(
    user_id: int,
    name: str,
    query: dict,
    *,
    last_result_count: int | None = None,
) -> dict:
    clean_name = " ".join(str(name or "").strip().split())[:120]
    if not clean_name:
        raise ValueError("策略名称不能为空")
    if not isinstance(query, dict) or not query:
        raise ValueError("请至少设置一项筛选条件")
    now = datetime.now()
    with SessionLocal() as db:
        row = db.execute(
            select(ScreenerSavedStrategy).where(
                ScreenerSavedStrategy.user_id == int(user_id),
                ScreenerSavedStrategy.name == clean_name,
            )
        ).scalar_one_or_none()
        if row is None:
            row = ScreenerSavedStrategy(user_id=int(user_id), name=clean_name, query_json="{}")
            db.add(row)
        row.query_json = json.dumps(query, ensure_ascii=False, sort_keys=True)
        row.updated_at = now
        if last_result_count is not None:
            row.last_result_count = max(0, int(last_result_count))
        db.commit()
        db.refresh(row)
        return _saved_strategy_dict(row)


def update_saved_strategy_result(user_id: int, strategy_id: int, result_count: int) -> dict:
    with SessionLocal() as db:
        row = db.execute(
            select(ScreenerSavedStrategy).where(
                ScreenerSavedStrategy.id == int(strategy_id),
                ScreenerSavedStrategy.user_id == int(user_id),
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError("保存的策略不存在")
        row.last_result_count = max(0, int(result_count))
        row.updated_at = datetime.now()
        db.commit()
        db.refresh(row)
        return _saved_strategy_dict(row)


def delete_saved_strategy(user_id: int, strategy_id: int) -> None:
    with SessionLocal() as db:
        row = db.execute(
            select(ScreenerSavedStrategy).where(
                ScreenerSavedStrategy.id == int(strategy_id),
                ScreenerSavedStrategy.user_id == int(user_id),
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError("保存的策略不存在")
        db.delete(row)
        db.commit()
