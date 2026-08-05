from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from backend.app.core.database import SessionLocal
from backend.app.core.models import DailyBar
from backend.app.market.stock_tools import get_tushare_daily, normalize_a_share_symbol
from backend.app.quant.factors.factor_analysis_core import compute_ic_series
from backend.app.quant.factors.registry import (
    RegistryError,
    SkipAlpha,
    get_default_registry,
)

MAX_FACTOR_SYMBOLS = 50
MAX_BENCH_FACTORS = 20
MAX_FACTOR_LIST = 200
MIN_CROSS_SECTION = 5
_VALID_ZOOS = frozenset({"alpha101", "gtja191", "qlib158", "academic", "fundamental"})
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


def factor_health() -> dict[str, Any]:
    registry = get_default_registry()
    health = registry.health()
    return {
        **health,
        "zoos": {zoo: len(registry.list(zoo=zoo)) for zoo in sorted(_VALID_ZOOS)},
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
    ids = registry.list(zoo=zoo, theme=theme, universe=universe)
    needle = str(query or "").strip().lower()
    if needle:
        ids = [
            alpha_id
            for alpha_id in ids
            if needle in alpha_id.lower()
            or needle in str(registry.get(alpha_id).meta.get("nickname") or "").lower()
        ]
    capped = max(1, min(int(limit), MAX_FACTOR_LIST))
    return {
        "total": len(ids),
        "returned": min(len(ids), capped),
        "items": [_alpha_summary(registry.get(alpha_id)) for alpha_id in ids[:capped]],
    }


def get_factor(alpha_id: str, *, include_source: bool = False) -> dict[str, Any]:
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
) -> dict[str, Any]:
    ids = list(dict.fromkeys(str(value or "").strip() for value in alpha_ids if str(value or "").strip()))
    if not ids:
        raise ValueError("至少选择一个因子")
    if len(ids) > MAX_BENCH_FACTORS:
        raise ValueError(f"单次最多评估 {MAX_BENCH_FACTORS} 个因子")

    registry = get_default_registry()
    for alpha_id in ids:
        registry.get(alpha_id)
    panel, coverage, refresh_failures = _load_panel(
        symbols,
        start_date=start_date,
        end_date=end_date,
        refresh=refresh,
    )
    forward_returns = panel["close"].pct_change(fill_method=None).shift(-1)
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    factor_panels: dict[str, pd.DataFrame] = {}
    for alpha_id in ids:
        try:
            factor = registry.compute(alpha_id, panel)
            factor_panels[alpha_id] = factor
            ic = compute_ic_series(factor, forward_returns)
            if ic.empty:
                raise ValueError("IC 序列为空，样本覆盖不足")
            ic_mean = float(ic.mean())
            ic_std = float(ic.std())
            ir = ic_mean / ic_std if ic_std > 0 else 0.0
            positive_ratio = float((ic > 0).mean())
            results.append(
                {
                    "id": alpha_id,
                    "zoo": registry.get(alpha_id).zoo,
                    "ic_mean": round(ic_mean, 6),
                    "ic_std": round(ic_std, 6),
                    "ir": round(ir, 4),
                    "ic_positive_ratio": round(positive_ratio, 4),
                    "ic_count": int(len(ic)),
                    "rank_stability": round(_rank_stability(factor), 4),
                    **_split_ic_stats(ic),
                    "category": _category(ic_mean, ic_std, len(ic), positive_ratio),
                }
            )
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
        "refresh_failures": refresh_failures,
        "methodology": (
            "按交易日计算因子横截面排名与下一交易日收益率的 Spearman IC；"
            "rank_stability 为相邻交易日横截面排名的 Spearman 相关均值（越接近 1 排序越稳、换手越低）；"
            "train_ic/test_ic 为按时间前 70%/后 30% 分窗的 IC，两者同向且同号才更可信。"
            "结果未计交易成本、停牌涨跌停、行业和市值中性化，仅用于研究筛选。"
        ),
    }

