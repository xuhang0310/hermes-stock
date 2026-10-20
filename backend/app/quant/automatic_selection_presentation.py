"""自动选股结果的白话呈现层。

只读已落库的运行、候选与评估证据，派生给普通用户看的内容：各维度 0–100 分、入选理由、
风险提示、选出后表现、历史战绩和数据异常说明。这里的输出都可以由落库证据重算，
不回写排名，也不进入 config_hash / 数据指纹。
"""
from __future__ import annotations

import bisect
import json
import logging
import math
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import func, select

from backend.app.core.database import SessionLocal
from backend.app.core.models import DailyBar, JobRun, ScheduledJob
from backend.app.market.market_sync import load_local_adj_factors, load_local_open_trade_dates
from backend.app.quant import automatic_selection_repository as repository
from backend.app.quant.automatic_selection_evaluation import EVALUATION_HORIZONS
from backend.app.quant.automatic_selection_scoring import (
    DEFAULT_STRATEGY_CONFIG,
    RESEARCH_LIMITATIONS,
    _normalized_weights,
    validate_config,
)

logger = logging.getLogger(__name__)

DISCLAIMER = "这是按固定规则打分排序的研究结果，不是对未来涨跌的预测，也不构成投资建议。"
SCHEDULE_NOTE = "每个交易日 17:30 自动同步收盘数据（失败每 30 分钟重试到 22:00），成功后自动选股。"

# (key, 名称, 包含的特征, 计分方向说明)
DIMENSIONS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("value", "估值", ("value_pe", "value_pb"), "市盈率、市净率越低越加分"),
    ("trend", "走势", ("momentum_20",), "近 20 个交易日涨得越多越加分（已复权）"),
    ("activity", "活跃度", ("liquidity_20",), "近 20 个交易日日均成交额越大越加分"),
    ("stability", "稳定性", ("low_volatility_20",), "近 20 个交易日每天涨跌越平稳越加分"),
)
_EXTRA_DIMENSION = ("extra", "附加因子", "策略额外配置的 TuShare 因子，按配置方向计分")
_SHORT_PHRASES = {
    "value": "估值比 {score}% 的股票便宜",
    "trend": "走势强于 {score}% 的股票",
    "activity": "成交比 {score}% 的股票活跃",
    "stability": "波动比 {score}% 的股票小",
    "extra": "附加因子好于 {score}% 的股票",
}
_GROUP_PHRASES = {
    "value": "整体估值偏低",
    "trend": "近期走势偏强",
    "activity": "成交活跃",
    "stability": "波动偏小",
    "extra": "附加因子表现好",
}
CHANGE_LABELS = {"added": "新进", "retained": "保留", "removed": "移出", "unchanged": "—"}
MARKET_CAP_LABELS = {"small": "小盘", "mid": "中盘", "large": "大盘"}
STATUS_LABELS = {
    "completed": "已出结果",
    "blocked": "数据不全，暂停",
    "failed": "计算出错",
    "running": "计算中",
}
EVALUATION_STATUS_LABELS = {
    "pending": "观察中",
    "completed": "已到期",
    "insufficient_data": "数据不足",
}

# 风险提示阈值：只做提示，不参与排名。
_RALLY_RETURN = 0.20
_SLUMP_RETURN = -0.10
_HIGH_VOLATILITY = 0.035
_THIN_AMOUNT_THOUSAND = 100_000.0  # daily_bars.amount 单位为千元，即 1 亿元
_HIGH_PE = 60.0
_WEAK_SCORE = 25
_STRONG_SCORE = 70
_HEADLINE_SCORE = 60

_POOL_CACHE: dict[tuple, dict[str, Any]] = {}
_POOL_CACHE_LIMIT = 8
_SINCE_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_SINCE_CACHE_TTL_SECONDS = 300.0
_SINCE_CACHE_LIMIT = 32
_cache_lock = threading.Lock()


def clear_caches() -> None:
    with _cache_lock:
        _POOL_CACHE.clear()
        _SINCE_CACHE.clear()


# ---------------------------------------------------------------- 公共入口


def daily_view(*, trade_date: str | None = None, strategy_id: int | None = None) -> dict[str, Any]:
    """某个交易日（默认最新）的选股结果，全部字段面向普通用户。"""
    strategy = repository.display_strategy(strategy_id)
    if strategy is None:
        if strategy_id is not None:
            raise LookupError("自动选股策略不存在")
        return _empty_view(None, [], None)
    runs = repository.strategy_run_index(int(strategy["id"]), limit=120)
    # 没能确定交易日的失败运行（数据未就绪）不进入日期列表，只在没有任何结果时展示原因。
    index = [entry for entry in runs if entry["trade_date"]]
    last_completed = next((entry["trade_date"] for entry in index if entry["status"] == "completed"), None)
    if trade_date:
        entry = next((item for item in index if item["trade_date"] == trade_date), None)
        if entry is None:
            raise LookupError("这一天没有选股结果")
    else:
        entry = index[0] if index else next(iter(runs), None)
    if entry is None:
        return _empty_view(strategy, index, last_completed)

    detail = repository.run_detail(entry["run_id"], candidate_scope="top", candidate_limit=200)
    if detail is None:
        raise LookupError("自动选股运行不存在")
    config = _run_config(detail)
    picks: list[dict[str, Any]] = []
    since: dict[str, Any] | None = None
    if detail.get("status") == "completed":
        pool = _pool_distribution(detail, _dimension_specs(_normalized_weights(config)))
        picks = present_picks(detail, detail.get("candidates") or [], pool=pool)
        since = since_selection(detail, [pick["symbol"] for pick in picks], pool["symbols"])
        _attach_performance(picks, since, detail.get("evaluations") or [])
    removed = repository.removed_candidates(detail["run_id"]) if picks else []
    return {
        "strategy": _strategy_brief(strategy),
        "available_dates": [
            {"trade_date": item["trade_date"], "status": item["status"], "status_label": STATUS_LABELS.get(item["status"], item["status"])}
            for item in index
        ],
        "trade_date": detail["target_trade_date"] or None,
        "is_latest": not index or entry is index[0],
        "last_completed_trade_date": last_completed,
        "run": _run_brief(detail),
        "status": status_view(detail, last_completed_trade_date=last_completed),
        "summary": _summary(detail, picks, removed),
        "picks": picks,
        "removed": removed,
        "funnel": (
            funnel_view(detail, repository.constraint_skipped_count(detail["run_id"]) if picks else 0)
            if detail.get("universe_count")
            else None
        ),
        "method": method_view(config),
        "performance": _run_performance(detail, since),
        "track_brief": _track_brief(int(strategy["id"])),
        "data_sync": _safe_data_sync_view(),
        "disclaimer": DISCLAIMER,
        "limitations": RESEARCH_LIMITATIONS,
    }


def track_record(*, strategy_id: int | None = None, limit: int = 60) -> dict[str, Any]:
    """历史各期 5/20/60 日表现与汇总。"""
    strategy = repository.display_strategy(strategy_id)
    if strategy is None:
        if strategy_id is not None:
            raise LookupError("自动选股策略不存在")
        return {"strategy": None, "periods": [], "summary": [], "notes": _track_notes(0), "disclaimer": DISCLAIMER}
    rows = repository.track_record_rows(int(strategy["id"]), limit=limit)
    summary = [_horizon_summary(horizon, rows) for horizon in EVALUATION_HORIZONS]
    periods = [
        {
            "run_id": row["run_id"],
            "trade_date": row["target_trade_date"],
            "selected_count": row["selected_count"],
            "horizons": {
                str(horizon): _period_cell(row["evaluations"].get(horizon))
                for horizon in EVALUATION_HORIZONS
            },
        }
        for row in rows
    ]
    return {
        "strategy": _strategy_brief(strategy),
        "periods": periods,
        "summary": summary,
        "notes": _track_notes(summary[0]["completed"] if summary else 0),
        "disclaimer": DISCLAIMER,
    }


def explanation_evidence(run: dict[str, Any], candidates: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """给模型写白话点评用的证据：分位、原始指标和规则风险，不含 z 值和哈希。"""
    picks = present_picks(run, candidates)
    return {
        "target_trade_date": run.get("target_trade_date"),
        "universe_count": run.get("universe_count"),
        "eligible_count": run.get("eligible_count"),
        "selected_count": run.get("selected_count"),
        "summary": _summary(run, picks, [])["headline"],
        "picks": [
            {
                "rank": pick["rank"],
                "name": pick["name"],
                "symbol": pick["symbol"],
                "industry": pick["industry"],
                "change": pick["change_label"],
                "reason": pick["headline"],
                "dimension_scores": {dim["label"]: dim["score"] for dim in pick["dimensions"]},
                "pe_ttm": pick["pe_ttm"],
                "pb": pick["pb"],
                "momentum_20_percent": _round(_scaled(pick["momentum_20"], 100), 2),
                "total_mv_yi": pick["total_mv_yi"],
                "risks": [risk["text"] for risk in pick["risks"]],
            }
            for pick in picks
        ],
        "method": "全市场先剔除 ST、次新、成交太少、亏损和数据不全的股票，再按估值、走势、活跃度、稳定性加权打分，取前若干只并限制单一行业数量。",
        "limitations": RESEARCH_LIMITATIONS,
    }


# ---------------------------------------------------------------- 入选股票


def present_picks(
    run: dict[str, Any],
    candidates: Iterable[dict[str, Any]],
    *,
    pool: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    config = _run_config(run)
    specs = _dimension_specs(_normalized_weights(config))
    distributions = (pool or _pool_distribution(run, specs))["distributions"]
    eligible_count = int(run.get("eligible_count") or 0)
    target_trade_date = str(run.get("target_trade_date") or "")
    picks: list[dict[str, Any]] = []
    for item in sorted(
        (value for value in candidates if value.get("selected")),
        key=lambda value: (value.get("rank") or 10**9, str(value.get("symbol"))),
    ):
        dimensions = [_dimension_view(spec, item, distributions.get(spec["key"]) or []) for spec in specs]
        rank = item.get("rank")
        picks.append(
            {
                "symbol": item.get("symbol"),
                "name": item.get("name") or item.get("symbol"),
                "industry": item.get("industry"),
                "rank": rank,
                "eligible_count": eligible_count,
                "top_percent": _round(rank / eligible_count * 100, 1) if rank and eligible_count else None,
                "change_state": item.get("change_state"),
                "change_label": CHANGE_LABELS.get(str(item.get("change_state")), "—"),
                "market_cap_label": MARKET_CAP_LABELS.get(str(item.get("market_cap_bucket")), None),
                "total_mv_yi": _round(_scaled(item.get("total_mv"), 1 / 10_000), 1),
                "close_price": item.get("close_price"),
                "pct_chg": item.get("pct_chg"),
                "pe_ttm": _round(item.get("pe_ttm"), 2),
                "pb": _round(item.get("pb"), 2),
                "avg_amount_yi": _round(_scaled(item.get("avg_amount_20"), 1 / 100_000), 2),
                "momentum_20": item.get("momentum_20"),
                "volatility_20": item.get("volatility_20"),
                "headline": _headline(dimensions),
                "dimensions": dimensions,
                "risks": _risks(item, dimensions, target_trade_date),
                "price_path": _price_path(item),
                "performance": {"since_selection": None, "horizons": []},
            }
        )
    _flag_industry_concentration(picks, config)
    return picks


def _dimension_specs(weights: dict[str, float]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for key, label, features, description in DIMENSIONS:
        used = {feature: weights[feature] for feature in features if feature in weights}
        if used:
            specs.append({"key": key, "label": label, "weights": used, "description": description})
    extra = {feature: weight for feature, weight in weights.items() if feature.startswith("tushare:")}
    if extra:
        key, label, description = _EXTRA_DIMENSION
        specs.append({"key": key, "label": label, "weights": extra, "description": description})
    return specs


def _strength(z_scores: dict[str, Any], spec: dict[str, Any]) -> float | None:
    total = sum(abs(weight) for weight in spec["weights"].values())
    if total <= 0:
        return None
    acc = 0.0
    for feature, weight in spec["weights"].items():
        value = _finite(z_scores.get(feature))
        if value is None:
            return None
        acc += weight * value
    return acc / total


def _pool_distribution(run: dict[str, Any], specs: list[dict[str, Any]]) -> dict[str, Any]:
    # 运行落库后不可变，按运行身份缓存合格池分布即可。
    key = (
        run.get("run_id"),
        run.get("data_fingerprint"),
        run.get("created_at"),
        tuple((spec["key"], tuple(sorted(spec["weights"].items()))) for spec in specs),
    )
    with _cache_lock:
        cached = _POOL_CACHE.get(key)
    if cached is not None:
        return cached
    pool = repository.eligible_score_pool(str(run["run_id"]))
    value = {
        "symbols": sorted(item["symbol"] for item in pool),
        "distributions": {
            spec["key"]: sorted(
                strength
                for strength in (_strength(item["z_scores"], spec) for item in pool)
                if strength is not None
            )
            for spec in specs
        },
    }
    with _cache_lock:
        while len(_POOL_CACHE) >= _POOL_CACHE_LIMIT:
            _POOL_CACHE.pop(next(iter(_POOL_CACHE)))
        _POOL_CACHE[key] = value
    return value


def _beats_percent(distribution: list[float], value: float | None) -> int | None:
    """这只股票超过了合格池里百分之多少的股票（0–100）。"""
    if value is None or not distribution:
        return None
    if len(distribution) == 1:
        return 100
    below = bisect.bisect_left(distribution, value)
    return int(round(min(below, len(distribution) - 1) / (len(distribution) - 1) * 100))


def _dimension_view(spec: dict[str, Any], item: dict[str, Any], distribution: list[float]) -> dict[str, Any]:
    z_scores = item.get("z_scores") or {}
    score = _beats_percent(distribution, _strength(z_scores, spec))
    contribution = sum(weight * (_finite(z_scores.get(feature)) or 0.0) for feature, weight in spec["weights"].items())
    reverse = sum(spec["weights"].values()) < 0
    return {
        "key": spec["key"],
        "label": spec["label"],
        "score": score,
        "level": _level(score),
        "weight_percent": int(round(sum(abs(weight) for weight in spec["weights"].values()) * 100)),
        "contribution": _round(contribution, 4),
        "reverse": reverse,
        "description": spec["description"] + ("（本策略反向使用：数值越低越加分）" if reverse else ""),
        "detail": _dimension_detail(spec["key"], item, score, reverse),
    }


def _level(score: int | None) -> str:
    if score is None:
        return "unknown"
    if score >= _STRONG_SCORE:
        return "strong"
    if score < _WEAK_SCORE + 5:
        return "weak"
    return "medium"


def _dimension_detail(key: str, item: dict[str, Any], score: int | None, reverse: bool) -> str:
    beats = "—" if score is None else f"{score}%"
    if reverse:
        return f"本策略对这一项反向计分，得分超过 {beats} 的合格股票"
    if key == "value":
        parts = []
        pe_ttm, pb = _finite(item.get("pe_ttm")), _finite(item.get("pb"))
        if pe_ttm is not None:
            parts.append(f"市盈率 {pe_ttm:.1f} 倍")
        if pb is not None:
            parts.append(f"市净率 {pb:.2f} 倍")
        prefix = "、".join(parts)
        return f"{prefix}，比 {beats} 的合格股票便宜" if prefix else f"估值比 {beats} 的合格股票便宜"
    if key == "trend":
        momentum = _finite(item.get("momentum_20"))
        if momentum is None:
            return f"走势强于 {beats} 的合格股票"
        direction = "上涨" if momentum >= 0 else "下跌"
        return f"近 20 个交易日{direction} {abs(momentum) * 100:.1f}%（已复权），强于 {beats} 的合格股票"
    if key == "activity":
        amount = _finite(item.get("avg_amount_20"))
        if amount is None:
            return f"成交比 {beats} 的合格股票活跃"
        return f"近 20 个交易日日均成交 {_money_from_thousand(amount)}，比 {beats} 的合格股票活跃"
    if key == "stability":
        volatility = _finite(item.get("volatility_20"))
        if volatility is None:
            return f"波动比 {beats} 的合格股票小"
        return f"近 20 个交易日平均每天波动约 {volatility * 100:.1f}%，比 {beats} 的合格股票平稳"
    return f"附加因子综合表现好于 {beats} 的合格股票"


def _headline(dimensions: list[dict[str, Any]]) -> str:
    # 贡献相同时按维度固定顺序（估值、走势、活跃度、稳定性）取前两项。
    strong = sorted(
        (
            (position, dim)
            for position, dim in enumerate(dimensions)
            if not dim["reverse"] and dim["score"] is not None and dim["score"] >= _HEADLINE_SCORE
        ),
        key=lambda item: (-(item[1]["contribution"] or 0.0), item[0]),
    )[:2]
    if not strong:
        return "各项比较均衡，综合得分靠前"
    return "，".join(_SHORT_PHRASES[dim["key"]].format(score=dim["score"]) for _, dim in strong)


def _risks(item: dict[str, Any], dimensions: list[dict[str, Any]], target_trade_date: str) -> list[dict[str, str]]:
    risks: list[dict[str, str]] = []
    momentum = _finite(item.get("momentum_20"))
    if momentum is not None and momentum >= _RALLY_RETURN:
        risks.append(_risk("warning", f"近 20 个交易日已上涨 {momentum * 100:.1f}%，短期涨幅较大，注意回调"))
    elif momentum is not None and momentum <= _SLUMP_RETURN:
        risks.append(_risk("warning", f"近 20 个交易日下跌 {abs(momentum) * 100:.1f}%，走势仍偏弱"))
    pct_chg = _finite(item.get("pct_chg"))
    limit = _price_limit_percent(str(item.get("symbol") or ""))
    if pct_chg is not None and pct_chg >= limit - 0.5:
        risks.append(_risk("warning", f"选出当天上涨 {pct_chg:.2f}%，接近涨停，下一交易日开盘可能买不进"))
    elif pct_chg is not None and pct_chg <= -(limit - 0.5):
        risks.append(_risk("warning", f"选出当天下跌 {abs(pct_chg):.2f}%，接近跌停"))
    volatility = _finite(item.get("volatility_20"))
    if volatility is not None and volatility >= _HIGH_VOLATILITY:
        risks.append(_risk("warning", f"近 20 个交易日平均每天波动约 {volatility * 100:.1f}%，波动偏大"))
    amount = _finite(item.get("avg_amount_20"))
    if amount is not None and amount < _THIN_AMOUNT_THOUSAND:
        risks.append(_risk("info", f"日均成交额约 {_money_from_thousand(amount)}，大额买卖可能明显影响价格"))
    pe_ttm = _finite(item.get("pe_ttm"))
    if pe_ttm is not None and pe_ttm >= _HIGH_PE:
        risks.append(_risk("info", f"市盈率 {pe_ttm:.0f} 倍，估值偏高"))
    listed_days = _days_between(item.get("list_date"), target_trade_date)
    if listed_days is not None and listed_days < 365:
        risks.append(_risk("info", "上市不满一年，可参考的历史较短"))
    for dim in dimensions:
        if not dim["reverse"] and dim["score"] is not None and dim["score"] < _WEAK_SCORE:
            risks.append(_risk("info", f"{dim['label']}偏弱：只好于 {dim['score']}% 的合格股票"))
    return risks


def _flag_industry_concentration(picks: list[dict[str, Any]], config: dict[str, Any]) -> None:
    limit = int((config.get("constraints") or {}).get("max_per_industry") or 0)
    if limit <= 0:
        return
    counts = Counter(pick["industry"] for pick in picks if pick["industry"])
    for pick in picks:
        count = counts.get(pick["industry"], 0)
        if pick["industry"] and count >= limit:
            pick["risks"].append(
                _risk("info", f"同行业（{pick['industry']}）已入选 {count} 只，达到行业上限，注意行业集中")
            )


def _risk(level: str, text: str) -> dict[str, str]:
    return {"level": level, "text": text}


def _price_limit_percent(symbol: str) -> float:
    code = symbol.split(".")[0]
    if symbol.upper().endswith(".BJ") or code.startswith(("4", "8", "92")):
        return 30.0
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    return 10.0


def _price_path(item: dict[str, Any]) -> list[dict[str, Any]]:
    """选出前 20 个交易日的复权走势，起点归一为 1；旧运行没有复权因子时不画。"""
    history = (item.get("input_evidence") or {}).get("history") or []
    points: list[tuple[str, float]] = []
    for bar in history:
        close, factor = _positive(bar.get("close")), _positive(bar.get("adj_factor"))
        if close is None or factor is None:
            return []
        points.append((str(bar.get("trade_date") or ""), close * factor))
    if len(points) < 2:
        return []
    base = points[0][1]
    return [{"trade_date": trade_date, "value": round(value / base, 6)} for trade_date, value in points]


# ---------------------------------------------------------------- 选出后表现


def since_selection(
    run: dict[str, Any], selected_symbols: Iterable[str], benchmark_symbols: Iterable[str],
) -> dict[str, Any] | None:
    """从选出后第一个交易日开盘到最新收盘的复权涨跌（未扣成本）；5 分钟缓存。"""
    run_id = str(run.get("run_id") or "")
    now = time.monotonic()
    with _cache_lock:
        cached = _SINCE_CACHE.get(run_id)
    if cached is not None and now - cached[0] < _SINCE_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        result = _compute_since_selection(
            str(run.get("target_trade_date") or ""),
            sorted(set(selected_symbols)),
            sorted(set(benchmark_symbols)),
        )
    except Exception as exc:
        logger.warning("自动选股选出后表现计算失败 run_id=%s error=%s", run_id, exc)
        result = None
    with _cache_lock:
        while len(_SINCE_CACHE) >= _SINCE_CACHE_LIMIT:
            _SINCE_CACHE.pop(next(iter(_SINCE_CACHE)))
        _SINCE_CACHE[run_id] = (now, result)
    return result


def _compute_since_selection(
    target_trade_date: str, selected: list[str], benchmark: list[str],
) -> dict[str, Any] | None:
    if not target_trade_date or not selected:
        return None
    dates = load_local_open_trade_dates(start_exclusive=target_trade_date, limit=max(EVALUATION_HORIZONS))
    waiting = {
        "status": "waiting",
        "entry_trade_date": dates[0] if dates else None,
        "as_of": None,
        "trading_days": 0,
        "basket_return": None,
        "benchmark_return": None,
        "excess_return": None,
        "covered_count": 0,
        "selected_count": len(selected),
        "per_symbol": {},
        "note": "下一个交易日开盘后开始观察",
    }
    if not dates:
        return waiting
    normalized_date = func.replace(DailyBar.trade_date, "-", "")
    bars: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    with SessionLocal() as db:
        for symbol, trade_date, open_price, close_price in db.execute(
            select(DailyBar.symbol, normalized_date, DailyBar.open, DailyBar.close).where(
                DailyBar.source == "tushare",
                DailyBar.symbol.in_(selected),
                DailyBar.trade_date.in_(_date_variants(dates)),
            )
        ).all():
            bars[(str(symbol), str(trade_date))] = (_positive(open_price), _positive(close_price))
        entry = dates[0]
        latest = None
        for trade_date in reversed(dates):
            covered = sum(1 for symbol in selected if (bars.get((symbol, trade_date)) or (None, None))[1])
            if covered / len(selected) >= 0.8:
                latest = trade_date
                break
        if latest is None:
            return waiting
        benchmark_set = set(benchmark)
        for symbol, trade_date, open_price, close_price in db.execute(
            select(DailyBar.symbol, normalized_date, DailyBar.open, DailyBar.close).where(
                DailyBar.source == "tushare",
                DailyBar.trade_date.in_(_date_variants([entry, latest])),
            )
        ).all():
            if str(symbol) in benchmark_set:
                bars[(str(symbol), str(trade_date))] = (_positive(open_price), _positive(close_price))
        factors = load_local_adj_factors(db, sorted(set(selected) | benchmark_set), [entry, latest])

    def adjusted_return(symbol: str) -> float | None:
        entry_open = (bars.get((symbol, entry)) or (None, None))[0]
        latest_close = (bars.get((symbol, latest)) or (None, None))[1]
        entry_factor, latest_factor = factors.get((symbol, entry)), factors.get((symbol, latest))
        if not (entry_open and latest_close and entry_factor and latest_factor):
            return None
        return latest_close * latest_factor / (entry_open * entry_factor) - 1.0

    per_symbol = {symbol: adjusted_return(symbol) for symbol in selected}
    basket_values = [value for value in per_symbol.values() if value is not None]
    benchmark_values = [value for value in (adjusted_return(symbol) for symbol in benchmark) if value is not None]
    basket = _mean(basket_values)
    benchmark_return = _mean(benchmark_values) if len(benchmark_values) >= max(1, len(benchmark) // 2) else None
    return {
        "status": "observing",
        "entry_trade_date": entry,
        "as_of": latest,
        "trading_days": dates.index(latest) + 1,
        "basket_return": basket,
        "benchmark_return": benchmark_return,
        "excess_return": basket - benchmark_return if basket is not None and benchmark_return is not None else None,
        "covered_count": len(basket_values),
        "selected_count": len(selected),
        "per_symbol": per_symbol,
        "note": "从选出后第一个交易日开盘算到最新收盘，已复权，未扣交易成本",
    }


def _attach_performance(
    picks: list[dict[str, Any]], since: dict[str, Any] | None, evaluations: list[dict[str, Any]],
) -> None:
    by_horizon: dict[int, dict[str, Any]] = {}
    for evaluation in evaluations:
        by_horizon[int(evaluation.get("horizon") or 0)] = {
            "status": evaluation.get("status"),
            "returns": {
                str(row.get("symbol")): row.get("net_return")
                for row in evaluation.get("per_symbol") or []
                if isinstance(row, dict)
            },
        }
    per_symbol = (since or {}).get("per_symbol") or {}
    for pick in picks:
        pick["performance"] = {
            "since_selection": per_symbol.get(pick["symbol"]),
            "horizons": [
                {
                    "horizon": horizon,
                    "status": by_horizon[horizon]["status"],
                    "status_label": EVALUATION_STATUS_LABELS.get(str(by_horizon[horizon]["status"]), "—"),
                    "net_return": by_horizon[horizon]["returns"].get(pick["symbol"])
                    if by_horizon[horizon]["status"] == "completed"
                    else None,
                }
                for horizon in EVALUATION_HORIZONS
                if horizon in by_horizon
            ],
        }


def _run_performance(run: dict[str, Any], since: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "since_selection": {key: value for key, value in since.items() if key != "per_symbol"} if since else None,
        "horizons": [
            {
                "horizon": int(evaluation.get("horizon") or 0),
                "status": evaluation.get("status"),
                "status_label": EVALUATION_STATUS_LABELS.get(str(evaluation.get("status")), "—"),
                "entry_trade_date": evaluation.get("entry_trade_date"),
                "exit_trade_date": evaluation.get("exit_trade_date"),
                "basket_return": evaluation.get("basket_return"),
                "benchmark_return": evaluation.get("benchmark_return"),
                "excess_return": evaluation.get("excess_return"),
                "max_drawdown": evaluation.get("max_drawdown"),
                "note": evaluation.get("error") if evaluation.get("status") != "completed" else None,
            }
            for evaluation in run.get("evaluations") or []
        ],
    }


# ---------------------------------------------------------------- 历史战绩


def _period_cell(evaluation: dict[str, Any] | None) -> dict[str, Any]:
    if not evaluation:
        return {"status": "missing", "status_label": "—"}
    excess = evaluation.get("excess_return")
    completed = evaluation.get("status") == "completed"
    return {
        "status": evaluation.get("status"),
        "status_label": EVALUATION_STATUS_LABELS.get(str(evaluation.get("status")), "—"),
        "exit_trade_date": evaluation.get("exit_trade_date"),
        "basket_return": evaluation.get("basket_return") if completed else None,
        "benchmark_return": evaluation.get("benchmark_return") if completed else None,
        "excess_return": excess if completed else None,
        "beat": (excess > 0) if completed and excess is not None else None,
    }


def _horizon_summary(horizon: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluations = [row["evaluations"].get(horizon) or {} for row in rows]
    completed = [
        item for item in evaluations
        if item.get("status") == "completed" and _finite(item.get("excess_return")) is not None
    ]
    excess = [float(item["excess_return"]) for item in completed]
    wins = sum(value > 0 for value in excess)
    return {
        "horizon": horizon,
        "completed": len(completed),
        "pending": sum(item.get("status") == "pending" for item in evaluations),
        "insufficient": sum(item.get("status") == "insufficient_data" for item in evaluations),
        "wins": wins,
        "win_rate": wins / len(completed) if completed else None,
        "avg_excess_return": _mean(excess),
        "avg_basket_return": _mean([_finite(item.get("basket_return")) for item in completed]),
        "avg_benchmark_return": _mean([_finite(item.get("benchmark_return")) for item in completed]),
        "best_excess_return": max(excess) if excess else None,
        "worst_excess_return": min(excess) if excess else None,
    }


def _track_notes(completed_five_day: int) -> list[str]:
    notes = [
        "每个交易日都会选一次，相邻几期的观察期互相重叠；平均值代表“典型一期”的表现，不能把各期收益连乘成累计收益。",
        "对比对象是同一天所有通过基础筛选的股票的等权平均，不是沪深 300 指数。",
        "收益已扣除佣金和滑点，但没有模拟涨停买不进、停牌卖不出等情况。",
    ]
    if completed_five_day < 20:
        notes.insert(0, f"目前只有 {completed_five_day} 期到期的 5 日结果，样本还少，不能说明长期效果。")
    return notes


def _track_brief(strategy_id: int) -> dict[str, Any] | None:
    rows = repository.track_record_rows(strategy_id, limit=20)
    summary = _horizon_summary(5, rows)
    if not summary["completed"]:
        return None
    return {
        "horizon": 5,
        "periods": summary["completed"],
        "wins": summary["wins"],
        "avg_excess_return": summary["avg_excess_return"],
    }


# ---------------------------------------------------------------- 运行状态、漏斗与方法说明


_EXCLUSION_TEXT = {
    "not_listed": "已退市或暂停上市",
    "st": "ST / *ST 风险警示股",
    "listing_too_short": "上市时间太短",
    "target_bar_missing": "当天没有行情（停牌等）",
    "daily_basic_missing": "缺少当日基础数据记录或有效市值",
    "valuation_invalid": "市盈率或市净率为空或非正，不满足估值筛选条件",
    "history_insufficient": "近 20 个交易日行情不全",
    "adj_factor_missing": "缺少复权数据",
    "liquidity_low": "成交太少",
    "required_factor_missing": "缺少附加因子数据",
    "not_in_current_catalog": "已不在本期股票名单",
}


def exclusion_text(code: str) -> str:
    return _EXCLUSION_TEXT.get(code, code)


def status_view(run: dict[str, Any] | None, *, last_completed_trade_date: str | None = None) -> dict[str, Any]:
    if run is None:
        return {
            "code": "empty",
            "title": "还没有选股结果",
            "message": f"{SCHEDULE_NOTE}管理员也可以点击“重新计算”立即生成。",
            "issues": [],
            "detail": None,
            "fallback_trade_date": None,
        }
    status = str(run.get("status") or "")
    trade_date = str(run.get("target_trade_date") or "")
    fallback = last_completed_trade_date if last_completed_trade_date and last_completed_trade_date != trade_date else None
    if status == "completed":
        return {
            "code": "ok",
            "title": f"{_date_label(trade_date)} 收盘后的选股结果",
            "message": "数据检查通过，结果已生成。",
            "issues": [],
            "detail": None,
            "fallback_trade_date": None,
        }
    if status == "running":
        return {
            "code": "running",
            "title": f"正在计算 {_date_label(trade_date)} 的结果",
            "message": "通常几分钟内完成，稍后刷新即可。",
            "issues": [],
            "detail": None,
            "fallback_trade_date": fallback,
        }
    if status == "blocked":
        gate = run.get("quality_gate") or {}
        return {
            "code": "blocked",
            "title": f"{_date_label(trade_date)} 的数据不完整，系统暂停出结果",
            "message": (
                "为了不用残缺数据误导判断，数据检查没通过时整批暂停，不会只拿部分股票硬算。"
                f"{SCHEDULE_NOTE}数据补齐后管理员也可以点击“重新计算”。"
            ),
            "issues": [_failure_text(failure) for failure in gate.get("failures") or []],
            "detail": None,
            "fallback_trade_date": fallback,
        }
    error = str(run.get("error") or "未知错误")
    if not trade_date:
        started = _date_label(str(run.get("started_at") or run.get("created_at") or "")[:10])
        title = f"最近一次选股没能运行（{started}）"
    else:
        title = f"{_date_label(trade_date)} 的计算出错了"
    if error.startswith(_CACHE_NOT_READY_PREFIX):
        return {
            "code": "failed",
            "title": title,
            "message": (
                "当时本地的行情数据还没准备好，系统没有选股（不会拿残缺数据硬算）。"
                "数据同步成功后会自动选股，进展见本页“数据同步”提示。"
            ),
            "issues": plain_data_issues(error[len(_CACHE_NOT_READY_PREFIX):]),
            "detail": error[:2000],
            "fallback_trade_date": fallback,
        }
    return {
        "code": "failed",
        "title": title,
        "message": "错误已记录，管理员可以在“高级”里查看详情后重新计算。",
        "issues": [error[:300]],
        "detail": error[:2000],
        "fallback_trade_date": fallback,
    }


_CACHE_NOT_READY_PREFIX = "自动选股本地缓存未就绪："
# 按顺序匹配，命中第一条即用；None 表示保留原文（数据源的报错本身就是可操作信息）。
_DATA_ISSUE_RULES: tuple[tuple[tuple[str, ...], str | None], ...] = (
    (("未配置 STOCK_TUSHARE_TOKEN",), "没有配置 TuShare 数据源 Token，无法拉取行情（请在“数据源设置”中配置）"),
    (("TuShare 返回错误", "TuShare 调用失败"), None),
    (("上市标的 0/",), "数据源还没有返回这一天的全市场行情，通常是当天数据尚未发布（收盘后约 15:00–17:00 陆续入库）"),
    (("全市场响应不完整", "行情响应格式无效", "含无效标的"), "数据源返回的全市场行情不完整"),
    (("交易日历", "历史交易日"), "交易日历没有同步到最新"),
    (("A 股目录", "股票目录", "stock_basic"), "股票名单没有同步完整"),
    (("复权因子", "adj_factor"), "复权数据不全"),
    (("daily_basic",), "市盈率、市值等每日指标不全"),
    (("日线",), "日线行情不全"),
    (("因子",), "策略所需的附加因子数据不全"),
)


def plain_data_issues(text: str, *, limit: int = 4) -> list[str]:
    """把数据入库/缓存检查的技术报错翻成去重后的白话原因。"""
    issues: list[str] = []
    for piece in (part.strip() for part in str(text or "").replace(";", "；").split("；")):
        if not piece:
            continue
        plain = piece
        for needles, replacement in _DATA_ISSUE_RULES:
            if any(needle in piece for needle in needles):
                plain = f"数据源报错：{piece}" if replacement is None else replacement
                break
        if plain not in issues:
            issues.append(plain)
    return issues[:limit]


def data_sync_view(now: datetime | None = None) -> dict[str, Any]:
    """最近一次“自动选股数据入库”任务的白话状态；选股结果停更时，原因几乎都在这里。"""
    from backend.app.scheduler.rhythm_scheduler import (
        AUTOMATIC_SELECTION_DATA_JOB_KEY,
        AUTOMATIC_SELECTION_DATA_RETRY_MINUTES,
        AUTOMATIC_SELECTION_DATA_RETRY_UNTIL,
        now_sh,
    )

    current = now or now_sh()
    with SessionLocal() as db:
        row = db.scalar(
            select(JobRun)
            .where(JobRun.job_key == AUTOMATIC_SELECTION_DATA_JOB_KEY)
            .order_by(JobRun.started_at.desc(), JobRun.id.desc())
            .limit(1)
        )
        run_time = db.scalar(
            select(ScheduledJob.run_time).where(ScheduledJob.job_key == AUTOMATIC_SELECTION_DATA_JOB_KEY)
        )
    schedule = f"每个交易日 {run_time or '收盘后'} 自动同步"
    if row is None:
        return {
            "status": "never",
            "ok": False,
            "title": "还没有同步过选股所需的行情数据",
            "at": None,
            "trigger_label": None,
            "target_trade_date": None,
            "issues": [],
            "detail": None,
            "next_step": f"{schedule}；管理员也可以点击“重新计算”立即同步并选股。",
        }
    try:
        output = json.loads(row.output_content or "{}")
    except (TypeError, ValueError):
        output = {}
    target = str((output or {}).get("target_trade_date") or "") or None
    trigger_label = "手动" if str(row.trigger_type or "").startswith("manual") else "定时"
    base = {
        "status": row.status,
        "ok": row.status == "success",
        # job_runs 存的是北京墙钟时间（now_sh），按接口约定显式带上 +08:00。
        "at": f"{row.started_at.isoformat(timespec='seconds')}+08:00" if row.started_at else None,
        "trigger_label": trigger_label,
        "target_trade_date": target,
        "issues": [],
        "detail": None,
        "next_step": None,
    }
    if row.status == "success":
        return {**base, "title": f"{_date_label(target)} 收盘数据已同步" if target else "行情数据已同步"}
    if row.status == "running":
        return {**base, "title": "正在同步行情数据", "next_step": "通常一两分钟完成，完成后会自动选股。"}
    same_day = row.started_at is not None and row.started_at.date() == current.date()
    if trigger_label == "定时" and same_day and current.strftime("%H:%M") < AUTOMATIC_SELECTION_DATA_RETRY_UNTIL:
        next_step = (
            f"系统会每 {AUTOMATIC_SELECTION_DATA_RETRY_MINUTES} 分钟自动重试，"
            f"直到 {AUTOMATIC_SELECTION_DATA_RETRY_UNTIL}；成功后自动选股。"
        )
    else:
        next_step = (
            f"{schedule}；管理员也可以点击“重新计算”立即重试"
            "（17:00 前会使用上一个交易日的收盘数据）。"
        )
    error = str(row.error or "")
    return {
        **base,
        "title": "最近一次数据同步失败",
        "issues": plain_data_issues(error) or ["数据同步没有成功"],
        "detail": error[:2000] or None,
        "next_step": next_step,
    }


def _safe_data_sync_view() -> dict[str, Any] | None:
    try:
        return data_sync_view()
    except Exception as exc:
        logger.warning("读取自动选股数据同步状态失败 error=%s", exc)
        return None


def _failure_text(failure: dict[str, Any]) -> str:
    code = str(failure.get("code") or "")
    actual, required = _finite(failure.get("actual")), _finite(failure.get("required"))

    def pct(value: float | None) -> str:
        return "—" if value is None else f"{value * 100:.0f}%"

    if code == "bar_coverage":
        return f"当天行情只拿到 {pct(actual)} 的股票（至少要 {pct(required)}）"
    if code == "basic_coverage":
        return f"当日基础数据只覆盖 {pct(actual)}（至少要 {pct(required)}）；按已入库且市值有效的记录统计"
    if code == "history_coverage":
        return f"过去 20 个交易日的行情或复权数据只覆盖 {pct(actual)}（至少要 {pct(required)}）"
    if code == "industry_coverage":
        return f"行业分类只覆盖 {pct(actual)}（至少要 {pct(required)}）"
    if code == "eligible_count":
        return (
            f"通过基础筛选的股票只有 {int(actual or 0)} 只（至少要 {int(required or 0)} 只），"
            "通常是当天数据还没同步完整"
        )
    if code.startswith("factor_coverage:"):
        return f"附加因子 {code.split(':', 2)[-1]} 只覆盖 {pct(actual)}（要求全部覆盖）"
    if code == "strategy_error":
        return str(failure.get("message") or "策略执行异常")
    return str(failure.get("message") or code)


def funnel_view(run: dict[str, Any], constraint_skipped: int) -> dict[str, Any]:
    config = _run_config(run)
    universe = int(run.get("universe_count") or 0)
    eligible = int(run.get("eligible_count") or 0)
    selected = int(run.get("selected_count") or 0)
    max_industry = int((config.get("constraints") or {}).get("max_per_industry") or 0)
    selection_note = f"按综合得分从高到低挑选前 {int(config.get('top_n') or 0)} 只"
    if max_industry > 0:
        selection_note += f"，同一行业最多 {max_industry} 只"
    if constraint_skipped:
        selection_note += f"；有 {constraint_skipped} 只得分够高但因行业或市值上限被跳过"
    exclusions = sorted(
        (
            {"code": code, "label": exclusion_text(code), "count": int(count)}
            for code, count in (run.get("exclusion_summary") or {}).items()
            if int(count or 0) > 0
        ),
        key=lambda item: (-item["count"], item["code"]),
    )
    return {
        "steps": [
            {"key": "universe", "label": "全市场股票", "count": universe, "note": "当天在册的沪深北 A 股"},
            {
                "key": "eligible",
                "label": "通过基础筛选",
                "count": eligible,
                "note": f"剔除 {max(universe - eligible, 0)} 只：ST、上市太短、成交太少、亏损或数据不全等",
            },
            {"key": "selected", "label": "最终入选", "count": selected, "note": selection_note},
        ],
        "exclusions": exclusions,
        "overlap_note": "一只股票可能同时命中多条剔除原因，所以各项相加会大于剔除总数。",
    }


def method_view(config: dict[str, Any]) -> dict[str, Any]:
    weights = _normalized_weights(config)
    constraints = config.get("constraints") or {}
    min_amount_wan = float(config.get("min_avg_amount") or 0.0) / 10  # 千元 → 万元
    max_industry = int(constraints.get("max_per_industry") or 0)
    max_bucket = int(constraints.get("max_per_market_cap_bucket") or 0)
    selection_rules = [f"按综合得分从高到低取前 {int(config.get('top_n') or 0)} 只"]
    if max_industry > 0:
        selection_rules.append(f"同一行业最多 {max_industry} 只，避免押注单一行业")
    if max_bucket > 0:
        selection_rules.append(f"小盘、中盘、大盘（按合格股票市值三等分）每档最多 {max_bucket} 只")
    selection_rules.append("入选股票等权观察，不做仓位建议")
    return {
        "filters": [
            "剔除 ST、*ST 等风险警示股",
            f"剔除上市不满 {int(config.get('min_listing_days') or 0)} 天的股票",
            f"剔除近 20 个交易日日均成交额低于 {min_amount_wan:,.0f} 万元的股票",
            "剔除亏损（市盈率为负）或净资产为负的股票",
            "剔除当天停牌、近 20 个交易日行情或复权数据不全的股票",
        ],
        "dimensions": [
            {
                "key": spec["key"],
                "label": spec["label"],
                "weight_percent": int(round(sum(abs(weight) for weight in spec["weights"].values()) * 100)),
                "description": spec["description"],
            }
            for spec in _dimension_specs(weights)
        ],
        "scoring": (
            "每个维度先在当天所有合格股票里比较（去掉极端值后标准化），再按权重加总成综合得分。"
            "页面上的维度分数表示这只股票超过了百分之多少的合格股票，100 分就是这一项最好。"
        ),
        "selection": selection_rules,
        "evaluation": [
            "当天收盘后选出，以下一个交易日的开盘价作为观察起点",
            "分别看 5、20、60 个交易日后的表现，扣除佣金和滑点",
            "对比对象是同一天所有合格股票的平均表现，跑赢它才说明打分有效",
        ],
        "schedule": SCHEDULE_NOTE,
        "disclaimer": DISCLAIMER,
        "limitations": RESEARCH_LIMITATIONS,
    }


def _summary(run: dict[str, Any], picks: list[dict[str, Any]], removed: list[dict[str, Any]]) -> dict[str, Any]:
    industries = Counter(pick["industry"] or "未分类" for pick in picks)
    averages: dict[str, tuple[str, float]] = {}
    for pick in picks:
        for dim in pick["dimensions"]:
            if dim["score"] is None or dim["reverse"]:
                continue
            label, total = averages.get(dim["key"], (dim["label"], 0.0))
            averages[dim["key"]] = (label, total + dim["score"])
    dimension_averages = [
        {"key": key, "label": label, "score": int(round(total / len(picks)))}
        for key, (label, total) in averages.items()
    ] if picks else []
    phrases = [
        _GROUP_PHRASES[item["key"]]
        for item in sorted(dimension_averages, key=lambda value: -value["score"])
        if item["score"] >= _HEADLINE_SCORE
    ][:2]
    headline = ""
    if picks:
        top = [f"{name}（{count} 只）" for name, count in industries.most_common(3) if count >= 2]
        headline = f"入选 {len(picks)} 只"
        headline += f"，行业以{'、'.join(top)}为主" if top else "，行业比较分散"
        if phrases:
            headline += f"；{'、'.join(phrases)}"
    return {
        "headline": headline,
        "selected_count": len(picks),
        "added": sum(pick["change_state"] == "added" for pick in picks),
        "retained": sum(pick["change_state"] == "retained" for pick in picks),
        "removed": len(removed),
        "industries": [{"industry": name, "count": count} for name, count in industries.most_common()],
        "dimension_averages": dimension_averages,
        "universe_count": run.get("universe_count"),
        "eligible_count": run.get("eligible_count"),
    }


# ---------------------------------------------------------------- 工具


def _empty_view(strategy: dict[str, Any] | None, index: list[dict[str, Any]], last_completed: str | None) -> dict[str, Any]:
    return {
        "strategy": _strategy_brief(strategy) if strategy else None,
        "available_dates": [],
        "trade_date": None,
        "is_latest": True,
        "last_completed_trade_date": last_completed,
        "run": None,
        "status": status_view(None),
        "summary": None,
        "picks": [],
        "removed": [],
        "funnel": None,
        "method": method_view(_run_config({"config": (strategy or {}).get("config")})),
        "performance": {"since_selection": None, "horizons": []},
        "track_brief": None,
        "data_sync": _safe_data_sync_view(),
        "disclaimer": DISCLAIMER,
        "limitations": RESEARCH_LIMITATIONS,
    }


def _strategy_brief(strategy: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": strategy.get("id"),
        "name": strategy.get("name"),
        "version": strategy.get("version"),
        "lifecycle_state": strategy.get("lifecycle_state"),
        "is_official": bool(strategy.get("is_official")),
    }


def _run_brief(run: dict[str, Any]) -> dict[str, Any]:
    return {
        key: run.get(key)
        for key in (
            "run_id",
            "status",
            "stage",
            "target_trade_date",
            "strategy_name",
            "strategy_version",
            "universe_count",
            "eligible_count",
            "selected_count",
            "selected_turnover",
            "llm_status",
            "llm_model",
            "llm_explanation",
            "finished_at",
            "started_at",
        )
    }


def _run_config(run: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_config(dict(run.get("config") or {}))
    except (TypeError, ValueError):
        return validate_config(dict(DEFAULT_STRATEGY_CONFIG))


def _date_label(trade_date: str) -> str:
    try:
        parsed = datetime.strptime(trade_date.replace("-", ""), "%Y%m%d")
    except ValueError:
        return trade_date or "—"
    return f"{parsed.month} 月 {parsed.day} 日"


def _days_between(start: Any, end: str) -> int | None:
    try:
        return (
            datetime.strptime(str(end).replace("-", ""), "%Y%m%d")
            - datetime.strptime(str(start or "").replace("-", ""), "%Y%m%d")
        ).days
    except ValueError:
        return None


def _date_variants(dates: Iterable[str]) -> list[str]:
    variants: set[str] = set()
    for value in dates:
        compact = str(value).replace("-", "")
        variants.add(compact)
        if len(compact) == 8:
            variants.add(f"{compact[:4]}-{compact[4:6]}-{compact[6:]}")
    return sorted(variants)


def _money_from_thousand(amount: float) -> str:
    if amount >= 100_000:
        return f"{amount / 100_000:.2f} 亿元"
    return f"{amount / 10:,.0f} 万元"


def _mean(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return sum(clean) / len(clean) if clean else None


def _scaled(value: Any, factor: float) -> float | None:
    number = _finite(value)
    return None if number is None else number * factor


def _round(value: Any, digits: int) -> float | None:
    number = _finite(value)
    return None if number is None else round(number, digits)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None
