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

import hashlib
import json
from datetime import datetime
from statistics import median

from sqlalchemy import and_, desc, func, select

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
)

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


def _band(values: list) -> dict | None:
    """min/median/max 分布带，供 LLM 参照持仓实际分布设计条件区间。"""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return {"min": round(min(vals), 2), "median": round(median(vals), 2), "max": round(max(vals), 2)}


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
