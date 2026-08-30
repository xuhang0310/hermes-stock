from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from backend.app.ai.llm_client import chat_completion
from backend.app.core.database import SessionLocal
from backend.app.core.llm_config import active_llm_config
from backend.app.core.models import DailyBar, Instrument, StockLatestPrice
from backend.app.market.stock_tools import get_tushare_daily, normalize_a_share_symbol
from backend.app.quant.factors.factor_analysis_core import compute_ic_series
from backend.app.quant.factors.registry import (
    RegistryError,
    SkipAlpha,
    get_default_registry,
)
from backend.app.quant.factor_signal_engine import _zscore_cross_section
from backend.app.quant.tushare_factor_service import (
    TUSHARE_FACTOR_ZOO,
    known_factor_names as tushare_known_factor_names,
    list_catalog as tushare_list_catalog,
    load_factor_panel as tushare_load_factor_panel,
    parse_tushare_factor_id,
    tushare_factor_id,
)

MAX_FACTOR_SYMBOLS = 50
MAX_BENCH_FACTORS = 20
MAX_FACTOR_LIST = 800
MAX_COMPOSITE_FACTORS = 10
MAX_COMPOSITE_ROWS = 200
MIN_CROSS_SECTION = 5
_VALID_ZOOS = frozenset({"alpha101", "gtja191", "qlib158", "academic", "fundamental", TUSHARE_FACTOR_ZOO})
_DATE_RE = re.compile(r"^\d{8}$")


@dataclass(frozen=True)
class PanelCoverage:
    symbols: tuple[str, ...]
    start_date: str | None
    end_date: str | None
    rows: int
    dates: int


def _compact_date(value: str | None, *, fallback: str | None = None) -> str | None:
    raw = str(value or fallback or "").strip().replace("-", "")
    if not raw:
        return None
    if not _DATE_RE.fullmatch(raw):
        raise ValueError(f"日期格式不正确：{value!r}，应为 YYYYMMDD 或 YYYY-MM-DD")
    return raw


def _normalize_symbols(symbols: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in symbols:
        symbol = normalize_a_share_symbol(str(value or "").strip())
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
            raise ValueError(f"无法识别 A 股代码：{value}")
        if symbol not in seen:
            seen.add(symbol)
            normalized.append(symbol)
    if len(normalized) < MIN_CROSS_SECTION:
        raise ValueError(f"横截面因子至少需要 {MIN_CROSS_SECTION} 只股票")
    if len(normalized) > MAX_FACTOR_SYMBOLS:
        raise ValueError(f"单次最多计算 {MAX_FACTOR_SYMBOLS} 只股票")
    return normalized


def _refresh_daily_bars(symbols: list[str], start_date: str, end_date: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for symbol in symbols:
        result = get_tushare_daily(
            symbol,
            start_date=start_date,
            end_date=end_date,
            limit=600,
        )
        if result.get("error"):
            failures.append({"symbol": symbol, "error": str(result["error"])[:240]})
    return failures


def _load_panel(
    symbols: Iterable[str],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    refresh: bool = False,
) -> tuple[dict[str, pd.DataFrame], PanelCoverage, list[dict[str, Any]]]:
    normalized = _normalize_symbols(symbols)
    end = _compact_date(end_date, fallback=date.today().strftime("%Y%m%d"))
    start = _compact_date(
        start_date,
        fallback=(date.today() - timedelta(days=900)).strftime("%Y%m%d"),
    )
    if start is None or end is None or start > end:
        raise ValueError("开始日期不能晚于结束日期")

    refresh_failures = _refresh_daily_bars(normalized, start, end) if refresh else []
    normalized_trade_date = func.replace(DailyBar.trade_date, "-", "")
    with SessionLocal() as db:
        rows = db.scalars(
            select(DailyBar)
            .where(
                DailyBar.symbol.in_(normalized),
                normalized_trade_date >= start,
                normalized_trade_date <= end,
            )
            .order_by(normalized_trade_date, DailyBar.symbol, DailyBar.id)
        ).all()

    # Multiple sources can cache the same bar. Prefer TuShare, then the newest row.
    deduped: dict[tuple[str, str], DailyBar] = {}
    for row in rows:
        trade_date = str(row.trade_date or "").replace("-", "")
        key = (trade_date, row.symbol)
        previous = deduped.get(key)
        row_rank = (2 if row.source == "tushare" else 1, row.id)
        previous_rank = (
            (2 if previous.source == "tushare" else 1, previous.id)
            if previous is not None
            else (0, 0)
        )
        if row_rank >= previous_rank:
            deduped[key] = row

    records = [
        {
            "trade_date": pd.Timestamp(key[0]),
            "symbol": key[1],
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "amount": row.amount,
        }
        for key, row in deduped.items()
    ]
    if not records:
        raise ValueError("本地 daily_bars 没有覆盖所选股票和日期；请启用 refresh 拉取 TuShare 日线")

    frame = pd.DataFrame.from_records(records)
    panel: dict[str, pd.DataFrame] = {}
    for field in ("open", "high", "low", "close", "volume", "amount"):
        panel[field] = (
            frame.pivot(index="trade_date", columns="symbol", values=field)
            .sort_index()
            .reindex(columns=normalized)
            .astype(float)
        )

    typical_price = (panel["open"] + panel["high"] + panel["low"] + panel["close"]) / 4.0
    with np.errstate(divide="ignore", invalid="ignore"):
        # TuShare daily amount is thousand CNY and volume is lots (100 shares).
        implied_vwap = panel["amount"] * 10.0 / panel["volume"]
    plausible = (implied_vwap > panel["low"] * 0.5) & (implied_vwap < panel["high"] * 1.5)
    panel["vwap"] = implied_vwap.where(plausible, typical_price)

    close = panel["close"]
    coverage = PanelCoverage(
        symbols=tuple(normalized),
        start_date=close.index.min().strftime("%Y%m%d") if not close.empty else None,
        end_date=close.index.max().strftime("%Y%m%d") if not close.empty else None,
        rows=len(records),
        dates=int(close.shape[0]),
    )
    return panel, coverage, refresh_failures


def _alpha_summary(alpha: Any) -> dict[str, Any]:
    meta = dict(alpha.meta or {})
    return {
        "id": alpha.id,
        "zoo": alpha.zoo,
        "nickname": meta.get("nickname"),
        "theme": meta.get("theme", []),
        "formula_latex": meta.get("formula_latex", ""),
        "columns_required": meta.get("columns_required", []),
        "extras_required": meta.get("extras_required", []),
        "requires_sector": bool(meta.get("requires_sector")),
        "universe": meta.get("universe", []),
        "frequency": meta.get("frequency", []),
        "decay_horizon": meta.get("decay_horizon"),
        "min_warmup_bars": meta.get("min_warmup_bars"),
        "notes": meta.get("notes", ""),
    }


def _tushare_summary(meta: dict[str, Any]) -> dict[str, Any]:
    """TuShare 目录行 → 与 ``_alpha_summary`` 同构的因子卡片（附带缓存覆盖信息）。"""
    factor_type = str(meta.get("factor_type") or "").strip()
    return {
        "id": meta.get("id") or tushare_factor_id(str(meta.get("factor_name") or "")),
        "zoo": TUSHARE_FACTOR_ZOO,
        "nickname": meta.get("nickname"),
        "theme": [factor_type] if factor_type else [],
        "formula_latex": "",
        "columns_required": [],
        "extras_required": [],
        "requires_sector": False,
        "universe": ["equity_cn"],
        "frequency": ["daily"],
        "decay_horizon": None,
        "min_warmup_bars": None,
        "notes": meta.get("factor_desc") or "",
        "factor_name": meta.get("factor_name"),
        "factor_type": factor_type,
        "cached_rows": int(meta.get("cached_rows") or 0),
        "cached_min_date": meta.get("cached_min_date"),
        "cached_max_date": meta.get("cached_max_date"),
    }


def factor_health() -> dict[str, Any]:
    registry = get_default_registry()
    health = registry.health()
    zoos = {zoo: len(registry.list(zoo=zoo)) for zoo in sorted(_VALID_ZOOS) if zoo != TUSHARE_FACTOR_ZOO}
    try:
        zoos[TUSHARE_FACTOR_ZOO] = len(tushare_known_factor_names())
    except SQLAlchemyError:
        zoos[TUSHARE_FACTOR_ZOO] = 0  # 目录表尚未建立（未跑 create_all）时不影响主库健康信息
    return {
        **health,
        "zoos": zoos,
        "source": "Vibe-Trading factor zoo (vendored, MIT; Qlib attribution retained)",
    }


def list_factors(
    *,
    zoo: str | None = None,
    theme: str | None = None,
    universe: str | None = "equity_cn",
    query: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if zoo and zoo not in _VALID_ZOOS:
        raise ValueError(f"未知因子库：{zoo}")
    registry = get_default_registry()
    needle = str(query or "").strip().lower()
    items: list[dict[str, Any]] = []
    if zoo != TUSHARE_FACTOR_ZOO:
        ids = registry.list(zoo=zoo, theme=theme, universe=universe)
        if needle:
            ids = [
                alpha_id
                for alpha_id in ids
                if needle in alpha_id.lower()
                or needle in str(registry.get(alpha_id).meta.get("nickname") or "").lower()
            ]
        items.extend(_alpha_summary(registry.get(alpha_id)) for alpha_id in ids)
    # TuShare 官方因子库：目录存本地表，无 theme 元数据；按主题筛选时不参与
    if zoo in (None, TUSHARE_FACTOR_ZOO) and theme is None and universe in (None, "equity_cn"):
        try:
            items.extend(
                _tushare_summary(meta)
                for meta in tushare_list_catalog(query=needle or None)
            )
        except SQLAlchemyError:
            if zoo == TUSHARE_FACTOR_ZOO:
                raise ValueError("TuShare 因子目录表尚未就绪，请重启应用后先同步因子目录")
    capped = max(1, min(int(limit), MAX_FACTOR_LIST))
    return {
        "total": len(items),
        "returned": min(len(items), capped),
        "items": items[:capped],
    }


def get_factor(alpha_id: str, *, include_source: bool = False) -> dict[str, Any]:
    tushare_name = parse_tushare_factor_id(str(alpha_id or "").strip())
    if tushare_name is not None:
        match = next(
            (meta for meta in tushare_list_catalog() if meta.get("factor_name") == tushare_name),
            None,
        )
        if match is None:
            raise KeyError(f"因子不存在：{alpha_id}")
        return _tushare_summary(match)
    registry = get_default_registry()
    try:
        alpha = registry.get(str(alpha_id or "").strip())
    except KeyError as exc:
        raise KeyError(f"因子不存在：{alpha_id}") from exc
    result = _alpha_summary(alpha)
    if include_source:
        result["source_code"] = registry.get_source(alpha.id)
    return result


def score_factor(
    alpha_id: str,
    symbols: Iterable[str],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    tushare_name = parse_tushare_factor_id(str(alpha_id or "").strip())
    if tushare_name is not None:
        return _score_tushare_factor(
            tushare_name,
            symbols,
            start_date=start_date,
            end_date=end_date,
        )
    registry = get_default_registry()
    alpha = registry.get(alpha_id)
    panel, coverage, refresh_failures = _load_panel(
        symbols,
        start_date=start_date,
        end_date=end_date,
        refresh=refresh,
    )
    try:
        factor = registry.compute(alpha_id, panel)
    except (SkipAlpha, RegistryError) as exc:
        raise ValueError(str(exc)) from exc

    valid_counts = factor.notna().sum(axis=1)
    valid_dates = valid_counts[valid_counts >= MIN_CROSS_SECTION].index
    if valid_dates.empty:
        warmup = int(alpha.meta.get("min_warmup_bars") or 0)
        raise ValueError(
            f"因子没有形成有效横截面；当前 {coverage.dates} 个交易日，"
            f"该因子建议至少预热 {warmup} 个交易日"
        )
    as_of = valid_dates[-1]
    raw = factor.loc[as_of].dropna()
    percentile = raw.rank(method="average", pct=True)
    scores = [
        {
            "symbol": symbol,
            "raw_score": round(float(raw[symbol]), 8),
            "percentile": round(float(percentile[symbol]), 6),
        }
        for symbol in raw.sort_values(ascending=False).index
    ]
    return {
        "alpha": _alpha_summary(alpha),
        "as_of_date": as_of.strftime("%Y%m%d"),
        "scores": scores,
        "coverage": coverage.__dict__,
        "refresh_failures": refresh_failures,
        "interpretation": (
            "percentile 仅表示因子横截面排序；因子方向必须先用样本外 IC 验证，"
            "不能直接把高分解释为买入建议。"
        ),
    }


def _score_tushare_factor(
    factor_name: str,
    symbols: Iterable[str],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """TuShare 因子打分：读本地缓存面板，取最近一个有效截面排序。"""
    alpha_id = tushare_factor_id(factor_name)
    alpha_meta = get_factor(alpha_id)  # 目录不存在时抛 KeyError
    normalized = _normalize_symbols(symbols)
    end = _compact_date(end_date, fallback=date.today().strftime("%Y%m%d"))
    start = _compact_date(
        start_date,
        fallback=(date.today() - timedelta(days=365)).strftime("%Y%m%d"),
    )
    if start is None or end is None or start > end:
        raise ValueError("开始日期不能晚于结束日期")
    factor = tushare_load_factor_panel(factor_name, normalized, start, end)
    if factor is None or factor.dropna(how="all").empty:
        raise ValueError("TuShare 因子值尚未摄入，请先在因子分析页同步该因子的因子值")
    valid_counts = factor.notna().sum(axis=1)
    valid_dates = valid_counts[valid_counts >= MIN_CROSS_SECTION].index
    if valid_dates.empty:
        raise ValueError(
            f"因子没有形成有效横截面；当前缓存 {int(factor.shape[0])} 个交易日，"
            f"至少需要 {MIN_CROSS_SECTION} 只股票同日有值"
        )
    as_of = valid_dates[-1]
    raw = factor.loc[as_of].dropna()
    percentile = raw.rank(method="average", pct=True)
    scores = [
        {
            "symbol": symbol,
            "raw_score": round(float(raw[symbol]), 8),
            "percentile": round(float(percentile[symbol]), 6),
        }
        for symbol in raw.sort_values(ascending=False).index
    ]
    return {
        "alpha": alpha_meta,
        "as_of_date": as_of.strftime("%Y%m%d"),
        "scores": scores,
        "coverage": {
            "symbols": tuple(normalized),
            "start_date": factor.index.min().strftime("%Y%m%d"),
            "end_date": factor.index.max().strftime("%Y%m%d"),
            "rows": int(factor.notna().sum().sum()),
            "dates": int(factor.shape[0]),
        },
        "refresh_failures": [],
        "interpretation": (
            "percentile 仅表示因子横截面排序；因子方向必须先用样本外 IC 验证，"
            "不能直接把高分解释为买入建议。"
        ),
    }


# ------------------------------------------------------------------ 多因子综合打分


def _latest_price_map(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """批量取最新价/名称/涨跌幅，缺缓存的 symbol 不出现在结果里（容忍为 None）。"""
    if not symbols:
        return {}
    with SessionLocal() as db:
        rows = db.execute(
            select(
                StockLatestPrice.symbol,
                StockLatestPrice.name,
                StockLatestPrice.price,
                StockLatestPrice.pct_chg,
                Instrument.market,
            )
            .join(Instrument, Instrument.symbol == StockLatestPrice.symbol)
            .where(StockLatestPrice.symbol.in_(symbols))
        ).all()
    return {
        row[0]: {"name": row[1], "price": row[2], "pct_chg": row[3], "market": row[4]}
        for row in rows
    }


def composite_score_factors(
    factor_ids: Iterable[str],
    *,
    weights: dict[str, float] | None = None,
    symbols: Iterable[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """多因子综合打分：各因子取最新有效横截面 → z-score → 加权求和 → 排名。

    - ``tushare:`` 前缀因子读本地缓存面板；``symbols`` 缺省时用缓存的全市场截面。
    - 本地预置因子（alpha101/gtja191/qlib158 等）必须提供 ``symbols``（≤50 只）。
    - 无法形成有效横截面的因子进 ``skipped``；全部失败才抛 ValueError。
    - 权重缺省等权，可负（做空方向）；按 Σ|w| 归一化。
    """
    ids: list[str] = []
    for raw in factor_ids:
        alpha_id = str(raw or "").strip()
        if alpha_id and alpha_id not in ids:
            ids.append(alpha_id)
    if not ids:
        raise ValueError("至少选择一个因子")
    if len(ids) > MAX_COMPOSITE_FACTORS:
        raise ValueError(f"综合打分最多支持 {MAX_COMPOSITE_FACTORS} 个因子")
    limit = max(1, min(int(limit or 50), MAX_COMPOSITE_ROWS))

    normalized = _normalize_symbols(symbols) if symbols else None
    has_local = any(parse_tushare_factor_id(alpha_id) is None for alpha_id in ids)
    if has_local and normalized is None:
        raise ValueError("包含本地预置因子时必须提供股票池（5-50 只）")

    end = _compact_date(end_date, fallback=date.today().strftime("%Y%m%d"))
    start = _compact_date(
        start_date,
        fallback=(date.today() - timedelta(days=365)).strftime("%Y%m%d"),
    )
    if start is None or end is None or start > end:
        raise ValueError("开始日期不能晚于结束日期")

    registry = get_default_registry()
    local_panel = None
    if has_local:
        local_panel, _, _ = _load_panel(normalized, start_date=start, end_date=end)

    sections: dict[str, pd.Series] = {}
    as_ofs: dict[str, str] = {}
    zoos: dict[str, str] = {}
    skipped: list[dict[str, str]] = []
    for alpha_id in ids:
        tushare_name = parse_tushare_factor_id(alpha_id)
        try:
            if tushare_name is not None:
                get_factor(alpha_id)  # 目录不存在时抛 KeyError
                factor = tushare_load_factor_panel(tushare_name, normalized, start, end)
                if factor is None or factor.dropna(how="all").empty:
                    raise ValueError("TuShare 因子值尚未摄入，请先在因子分析页同步该因子")
                zoos[alpha_id] = TUSHARE_FACTOR_ZOO
            else:
                alpha = registry.get(alpha_id)
                factor = registry.compute(alpha_id, local_panel)
                zoos[alpha_id] = alpha.zoo
            valid_counts = factor.notna().sum(axis=1)
            valid_dates = valid_counts[valid_counts >= MIN_CROSS_SECTION].index
            if valid_dates.empty:
                raise ValueError(
                    f"没有形成有效横截面（至少需要 {MIN_CROSS_SECTION} 只股票同日有值）"
                )
            as_of = valid_dates[-1]
            sections[alpha_id] = factor.loc[as_of].dropna()
            as_ofs[alpha_id] = as_of.strftime("%Y%m%d")
        except (KeyError, ValueError, SkipAlpha, RegistryError) as exc:
            skipped.append({"id": alpha_id, "reason": str(exc)})

    if not sections:
        reasons = "；".join(f"{item['id']}: {item['reason']}" for item in skipped)
        raise ValueError(f"没有任何因子能形成有效横截面。{reasons}")

    common = sorted(set.intersection(*(set(series.index) for series in sections.values())))
    if len(common) < MIN_CROSS_SECTION:
        raise ValueError(
            f"各因子共同覆盖的股票只有 {len(common)} 只，少于 {MIN_CROSS_SECTION} 只，"
            "无法形成有效横截面；请检查因子缓存范围或更换股票池"
        )

    used = list(sections)
    raw_weights = []
    for alpha_id in used:
        try:
            weight = float((weights or {}).get(alpha_id, 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        raw_weights.append(weight)
    norm = sum(abs(weight) for weight in raw_weights)
    if norm <= 1e-12:
        raise ValueError("因子权重不能全为 0")
    norm_weights = [weight / norm for weight in raw_weights]

    raw_frame = pd.DataFrame({alpha_id: sections[alpha_id].reindex(common) for alpha_id in used})
    z_frame = _zscore_cross_section(raw_frame.T).T  # 行=因子逐行标准化后转回 行=股票
    composite = sum(z_frame[alpha_id] * weight for alpha_id, weight in zip(used, norm_weights))
    composite = composite.dropna().sort_values(ascending=False)
    if composite.empty:
        raise ValueError("所有股票的因子值均无效（标准差为 0），无法打分")
    percentile = composite.rank(method="average", pct=True)
    price_map = _latest_price_map(common)

    rows: list[dict[str, Any]] = []
    for rank, symbol in enumerate(composite.index[:limit], start=1):
        quote = price_map.get(symbol) or {}
        rows.append(
            {
                "rank": rank,
                "symbol": symbol,
                "name": quote.get("name"),
                "market": quote.get("market"),
                "price": quote.get("price"),
                "pct_chg": quote.get("pct_chg"),
                "composite_score": round(float(composite[symbol]), 6),
                "percentile": round(float(percentile[symbol]), 6),
                "factor_scores": {
                    alpha_id: {
                        "raw": round(float(sections[alpha_id][symbol]), 8),
                        "z": round(float(z_frame.loc[symbol, alpha_id]), 6),
                    }
                    for alpha_id in used
                },
            }
        )

    return {
        "universe": int(len(composite)),
        "total": len(rows),
        "rows": rows,
        "factors_used": [
            {
                "id": alpha_id,
                "zoo": zoos[alpha_id],
                "as_of_date": as_ofs[alpha_id],
                "weight": round(weight, 6),
            }
            for alpha_id, weight in zip(used, norm_weights)
        ],
        "skipped": skipped,
        "interpretation": (
            "综合得分仅为因子横截面加权排序，不代表预期收益；因子方向必须先用样本外 IC 验证，"
            "不能直接把高分解释为买入建议。"
        ),
    }


def _category(ic_mean: float, ic_std: float, ic_count: int, positive_ratio: float) -> str:
    t_stat = ic_mean / (ic_std / math.sqrt(ic_count)) if ic_count > 0 and ic_std > 0 else 0.0
    if ic_mean > 0.02 and positive_ratio >= 0.55 and abs(t_stat) > 2:
        return "alive"
    if ic_mean < -0.02 and abs(t_stat) > 2:
        return "reversed"
    return "dead"


def _rank_stability(factor_df: pd.DataFrame) -> float:
    """RRE 秩稳定性：相邻交易日横截面排名的 Spearman 相关均值（0~1）。

    排序越稳定，调仓换手越低、噪声越小。参考 AlphaEval RRE 角度。
    """
    ranks = factor_df.rank(axis=1, method="average")
    prev = ranks.shift(1)
    pair = ranks.notna() & prev.notna()
    daily: list[float] = []
    for stamp in ranks.index:
        valid = pair.loc[stamp]
        if int(valid.sum()) < MIN_CROSS_SECTION:
            continue
        value = ranks.loc[stamp, valid].corr(prev.loc[stamp, valid])
        if value == value:  # 跳过 NaN
            daily.append(float(value))
    return float(np.mean(daily)) if daily else 0.0


def _split_ic_stats(ic: pd.Series, train_frac: float = 0.7) -> dict[str, Any]:
    """按时间前 70% / 后 30% 分窗口的 IC 统计，用于样本外一致性判断。"""
    total = int(len(ic))
    if total < 2:
        return {
            "train_ic": round(float(ic.mean()), 6) if total == 1 else None,
            "test_ic": None,
            "train_ic_count": total,
            "test_ic_count": 0,
        }
    split_at = max(1, int(total * train_frac))
    train = ic.iloc[:split_at]
    test = ic.iloc[split_at:]
    return {
        "train_ic": round(float(train.mean()), 6),
        "test_ic": round(float(test.mean()), 6) if not test.empty else None,
        "train_ic_count": int(len(train)),
        "test_ic_count": int(len(test)),
    }


def _factor_signal(factor: pd.DataFrame) -> pd.Series:
    """因子横截面排名拉平为 (date, symbol) → rank 的序列，用于跨因子相关性。"""
    return factor.rank(axis=1, method="average").stack()


def _greedy_dedup(
    results: list[dict[str, Any]],
    factor_panels: dict[str, pd.DataFrame],
    *,
    threshold: float,
) -> dict[str, Any]:
    """按 IR 降序贪心去重：因子与已选集的截面排名最大相关 < threshold 才保留。

    参考 AlphaEval 多样性筛选：低相关因子组成的池子信息冗余少、组合增益高。
    """
    signals = {item["id"]: _factor_signal(factor_panels[item["id"]]) for item in results}
    kept: list[str] = []
    dropped: list[dict[str, Any]] = []
    for item in results:
        alpha_id = item["id"]
        if not kept:
            kept.append(alpha_id)
            continue
        max_corr = 0.0
        for other_id in kept:
            corr = signals[alpha_id].reindex(signals[other_id].index).corr(signals[other_id])
            if corr != corr:  # 样本无交集 → 当作不相关
                continue
            max_corr = max(max_corr, float(abs(corr)))
        if max_corr < threshold:
            kept.append(alpha_id)
        else:
            dropped.append({"id": alpha_id, "max_corr_with_kept": round(max_corr, 4)})
    return {
        "threshold": threshold,
        "kept_ids": kept,
        "dropped": dropped,
        "note": "按 IR 降序贪心：与已选集最大截面相关 < 阈值才保留；低相关因子池信息冗余更少。",
    }


def benchmark_factors(
    alpha_ids: Iterable[str],
    symbols: Iterable[str],
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    refresh: bool = False,
    dedup_threshold: float | None = None,
    include_ic_series: bool = False,
) -> dict[str, Any]:
    ids = list(dict.fromkeys(str(value or "").strip() for value in alpha_ids if str(value or "").strip()))
    if not ids:
        raise ValueError("至少选择一个因子")
    if len(ids) > MAX_BENCH_FACTORS:
        raise ValueError(f"单次最多评估 {MAX_BENCH_FACTORS} 个因子")

    registry = get_default_registry()
    tushare_names: dict[str, str] = {}
    for alpha_id in ids:
        tushare_name = parse_tushare_factor_id(alpha_id)
        if tushare_name is not None:
            tushare_names[alpha_id] = tushare_name
        else:
            registry.get(alpha_id)
    if tushare_names:
        try:
            tushare_catalog = tushare_known_factor_names()
        except SQLAlchemyError:
            tushare_catalog = set()  # 目录表未就绪 → 按"目录未同步"跳过，不拖垮其余因子
    else:
        tushare_catalog = set()
    panel, coverage, refresh_failures = _load_panel(
        symbols,
        start_date=start_date,
        end_date=end_date,
        refresh=refresh,
    )
    forward_returns = panel["close"].pct_change(fill_method=None).shift(-1)
    bars_per_symbol = panel["close"].notna().sum()
    pair_counts = (panel["close"].notna() & forward_returns.notna()).sum(axis=1)
    symbol_coverage = {
        "total": len(coverage.symbols),
        "with_data": int((bars_per_symbol > 0).sum()),
        "empty_symbols": [str(s) for s in coverage.symbols if not bars_per_symbol.get(s, 0)],
        "usable_dates": int((pair_counts >= MIN_CROSS_SECTION).sum()),
    }
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    factor_panels: dict[str, pd.DataFrame] = {}
    for alpha_id in ids:
        try:
            if alpha_id in tushare_names:
                factor_name = tushare_names[alpha_id]
                if not tushare_catalog:
                    raise ValueError("TuShare 因子目录尚未同步，请先在因子分析页同步因子目录")
                if factor_name not in tushare_catalog:
                    raise ValueError(f"TuShare 因子库中不存在该因子：{factor_name}")
                factor = tushare_load_factor_panel(
                    factor_name,
                    coverage.symbols,
                    coverage.start_date,
                    coverage.end_date,
                )
                if factor is None:
                    raise ValueError("TuShare 因子值尚未摄入，请先在因子分析页同步该因子的因子值")
                factor = factor.reindex(index=panel["close"].index)
                zoo_label = TUSHARE_FACTOR_ZOO
            else:
                factor = registry.compute(alpha_id, panel)
                zoo_label = registry.get(alpha_id).zoo
            factor_panels[alpha_id] = factor
            ic = compute_ic_series(factor, forward_returns)
            if ic.empty:
                raise ValueError("IC 序列为空，样本覆盖不足")
            ic_mean = float(ic.mean())
            ic_std = float(ic.std())
            ir = ic_mean / ic_std if ic_std > 0 else 0.0
            positive_ratio = float((ic > 0).mean())
            result: dict[str, Any] = {
                "id": alpha_id,
                "zoo": zoo_label,
                "ic_mean": round(ic_mean, 6),
                "ic_std": round(ic_std, 6),
                "ir": round(ir, 4),
                "ic_positive_ratio": round(positive_ratio, 4),
                "ic_count": int(len(ic)),
                "rank_stability": round(_rank_stability(factor), 4),
                **_split_ic_stats(ic),
                "category": _category(ic_mean, ic_std, len(ic), positive_ratio),
            }
            if include_ic_series:
                result["ic_series"] = [
                    {"time": stamp.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
                    for stamp, v in ic.items()
                    if math.isfinite(v)
                ]
            results.append(result)
        except (SkipAlpha, RegistryError, ValueError) as exc:
            skipped.append({"id": alpha_id, "reason": str(exc)[:300]})
    results.sort(key=lambda item: item["ir"], reverse=True)
    dedup: dict[str, Any] | None = None
    if dedup_threshold is not None and factor_panels:
        dedup = _greedy_dedup(results, factor_panels, threshold=dedup_threshold)
    return {
        "results": results,
        "skipped": skipped,
        "dedup": dedup,
        "coverage": coverage.__dict__,
        "symbol_coverage": symbol_coverage,
        "refresh_failures": refresh_failures,
        "methodology": (
            "按交易日计算因子横截面排名与下一交易日收益率的 Spearman IC；"
            "rank_stability 为相邻交易日横截面排名的 Spearman 相关均值（越接近 1 排序越稳、换手越低）；"
            "train_ic/test_ic 为按时间前 70%/后 30% 分窗的 IC，两者同向且同号才更可信。"
            "结果未计交易成本、停牌涨跌停、行业和市值中性化，仅用于研究筛选。"
        ),
    }


_INTERPRET_SYSTEM_PROMPT = """你是一位擅长把量化研究结果讲给普通人听的助手。用户是完全不懂量化指标的普通投资者。
你会收到一份 A 股因子评估结果（JSON），请写一份"人话版"解读报告。

要求：
1. 面向零基础读者：不要堆砌术语。必须提到 IC、ICIR、正值比、秩稳定性、train/test 等词时，各用一句生活化比喻解释它到底在衡量什么。
2. 结构（用这些二级标题）：
   ## 一句话结论
   ## 这些指标都是什么意思
   ## 这批因子怎么样（逐个或分组点评：哪些像是有效的、哪些没用、为什么；遇到"反转"类因子要说明它反着用才可能有效）
   ## 为什么有些因子被剔除（仅当存在去重数据时写这一节）
   ## 要提醒你的话
3. 只引用给定数据里的数字，严禁编造；引用时保持原精度。
4. 如果样本覆盖不全（symbol_coverage 显示有股票没数据，或可用交易日很少），必须明确说结论可信度打折。
5. 语气平实坦率，像懂行的朋友给不懂的朋友讲解；可以通俗，不要油滑。
6. 必须写清楚：这只是对历史数据的统计研究，不代表未来走势，不构成任何投资建议。
7. 用 Markdown 输出，全文控制在 900 字以内。"""


def _interpretation_facts(benchmark: dict[str, Any]) -> dict[str, Any]:
    """把 benchmark 结果瘦身成喂给 LLM 的事实：去掉逐日 ic_series 大数组，只留指标数字。"""
    metric_keys = (
        "id", "zoo", "ic_mean", "ic_std", "ir", "ic_positive_ratio", "ic_count",
        "rank_stability", "train_ic", "test_ic", "train_ic_count", "test_ic_count", "category",
    )
    results = [
        {key: item.get(key) for key in metric_keys}
        for item in (benchmark.get("results") or [])
        if isinstance(item, dict)
    ]
    dedup = benchmark.get("dedup") or None
    return {
        "results": results,
        "skipped": [
            {"id": item.get("id"), "reason": str(item.get("reason") or "")[:200]}
            for item in (benchmark.get("skipped") or [])
            if isinstance(item, dict)
        ],
        "dedup": (
            {
                "threshold": dedup.get("threshold"),
                "kept_ids": dedup.get("kept_ids"),
                "dropped": dedup.get("dropped"),
            }
            if isinstance(dedup, dict)
            else None
        ),
        "coverage": benchmark.get("coverage"),
        "symbol_coverage": benchmark.get("symbol_coverage"),
        "methodology": benchmark.get("methodology"),
    }


def interpret_benchmark(benchmark: dict[str, Any]) -> dict[str, Any]:
    """用当前活跃 LLM 把因子评估结果翻译成零基础读者能看懂的大白话报告。"""
    results = benchmark.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("没有可解读的评估结果，请先运行因子评估")
    facts = _interpretation_facts(benchmark)
    config = active_llm_config()
    messages = [
        {"role": "system", "content": _INTERPRET_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"以下是因子评估结果 JSON：\n{json.dumps(facts, ensure_ascii=False)}",
        },
    ]
    data = chat_completion(messages, config, temperature=0.3, timeout=180.0, log_module="factors")
    try:
        content = str((data.get("choices") or [{}])[0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        content = ""
    content = content.strip()
    if not content:
        raise RuntimeError("模型未返回解读内容，请重试或更换模型")
    return {"interpretation": content, "model": config.get("display_name")}

