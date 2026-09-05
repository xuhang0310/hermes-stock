from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime
from typing import Any, Iterable

import numpy as np
import pandas as pd

from backend.app.quant.factor_signal_engine import _zscore_cross_section

BUILTIN_STRATEGY_KEY = "forward-multifactor"
SCORING_IMPLEMENTATION_VERSION = "forward-selection-v2"
RESEARCH_LIMITATIONS = (
    "仅用于上线后的研究观察。未完整模拟复权、涨跌停、停牌成交失败、整手与真实容量，"
    "不构成投资建议或交易指令。"
)
DEFAULT_STRATEGY_CONFIG: dict[str, Any] = {
    "top_n": 30,
    "history_days": 20,
    "min_listing_days": 120,
    "min_avg_amount": 50_000.0,
    "winsor_lower": 0.01,
    "winsor_upper": 0.99,
    "feature_weights": {
        "value_pe": 0.20,
        "value_pb": 0.15,
        "momentum_20": 0.25,
        "liquidity_20": 0.15,
        "low_volatility_20": 0.25,
    },
    "tushare_factors": [],
    "constraints": {
        "max_per_industry": 4,
        "max_per_market_cap_bucket": 15,
    },
    "quality": {
        "min_bar_coverage": 0.90,
        "min_basic_coverage": 0.85,
        "min_history_coverage": 0.85,
        "min_industry_coverage": 0.80,
        "min_factor_coverage": 1.0,
        "min_eligible_count": 1000,
        "min_evaluation_coverage": 0.80,
    },
    "costs": {"commission_rate": 0.0003, "slippage_rate": 0.0005},
    "limitations": RESEARCH_LIMITATIONS,
}

_ST_NAME_RE = re.compile(r"^(?:S\*ST|\*ST|SST|ST)", re.IGNORECASE)
_BASE_FEATURES = {
    "value_pe",
    "value_pb",
    "momentum_20",
    "liquidity_20",
    "low_volatility_20",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def data_fingerprint(*parts: Any) -> str:
    return hashlib.sha256(canonical_json(parts).encode("utf-8")).hexdigest()


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("策略配置必须是对象")
    unknown_keys = sorted(set(config) - set(DEFAULT_STRATEGY_CONFIG))
    if unknown_keys:
        raise ValueError(f"存在未知策略配置：{', '.join(unknown_keys)}")

    normalized = copy.deepcopy(DEFAULT_STRATEGY_CONFIG)
    normalized.update(
        {
            key: copy.deepcopy(value)
            for key, value in config.items()
            if key not in {"constraints", "quality", "costs", "feature_weights", "tushare_factors"}
        }
    )

    top_n = _integer(normalized.get("top_n"))
    if top_n is None or not 1 <= top_n <= 200:
        raise ValueError("Top-N 必须介于 1 和 200")
    normalized["top_n"] = top_n

    history_days = _integer(normalized.get("history_days"))
    if history_days != 20:
        raise ValueError("首期策略历史窗口固定为 20 个交易日")
    normalized["history_days"] = history_days

    min_listing_days = _integer(normalized.get("min_listing_days"))
    if min_listing_days is None or not 0 <= min_listing_days <= 36_500:
        raise ValueError("最短上市天数必须介于 0 和 36500")
    normalized["min_listing_days"] = min_listing_days

    min_avg_amount = _finite(normalized.get("min_avg_amount"))
    if min_avg_amount is None or min_avg_amount < 0:
        raise ValueError("最低平均成交额必须是非负有限数")
    normalized["min_avg_amount"] = min_avg_amount

    weights = config.get("feature_weights", normalized["feature_weights"])
    if not isinstance(weights, dict) or not weights:
        raise ValueError("策略至少需要一个特征权重")
    unknown_weights = sorted(set(str(key) for key in weights) - _BASE_FEATURES)
    if unknown_weights:
        raise ValueError(f"存在未知或重复来源的特征权重：{', '.join(unknown_weights)}")
    normalized_weights: dict[str, float] = {}
    for key, value in weights.items():
        weight = _finite(value)
        if weight is None:
            raise ValueError(f"特征权重必须是有限数：{key}")
        normalized_weights[str(key)] = weight
    if not any(abs(value) > 1e-12 for value in normalized_weights.values()):
        raise ValueError("特征权重必须包含至少一个非零有限数")
    normalized["feature_weights"] = normalized_weights

    factor_specs = config.get("tushare_factors", normalized["tushare_factors"])
    if not isinstance(factor_specs, list) or len(factor_specs) > 10:
        raise ValueError("TuShare 因子配置必须是列表且最多 10 个")
    factor_ids: set[str] = set()
    normalized_factors: list[dict[str, Any]] = []
    for spec in factor_specs:
        if not isinstance(spec, dict) or set(spec) != {"id", "weight"}:
            raise ValueError("TuShare 因子配置只能包含 id 和 weight")
        factor_id = str(spec.get("id") or "").strip()
        if not factor_id.startswith("tushare:") or len(factor_id) <= len("tushare:"):
            raise ValueError(f"TuShare 因子标识不正确：{factor_id or '空'}")
        if factor_id in factor_ids:
            raise ValueError(f"TuShare 因子重复：{factor_id}")
        factor_ids.add(factor_id)
        weight = _finite(spec.get("weight"))
        if weight is None or abs(weight) <= 1e-12:
            raise ValueError(f"TuShare 因子权重必须为非零有限数：{factor_id}")
        normalized_factors.append({"id": factor_id, "weight": weight})
    normalized["tushare_factors"] = normalized_factors

    lower = _finite(normalized.get("winsor_lower"))
    upper = _finite(normalized.get("winsor_upper"))
    if lower is None or upper is None or not 0 <= lower < upper <= 1:
        raise ValueError("去极值分位必须满足 0 ≤ lower < upper ≤ 1")
    normalized["winsor_lower"] = lower
    normalized["winsor_upper"] = upper

    constraints = _merged_section(config, "constraints")
    for key in ("max_per_industry", "max_per_market_cap_bucket"):
        value = _integer(constraints.get(key))
        if value is None or not 0 <= value <= 200:
            raise ValueError(f"约束 {key} 必须介于 0 和 200")
        constraints[key] = value
    normalized["constraints"] = constraints

    quality = _merged_section(config, "quality")
    for key in (
        "min_bar_coverage",
        "min_basic_coverage",
        "min_history_coverage",
        "min_industry_coverage",
        "min_factor_coverage",
        "min_evaluation_coverage",
    ):
        value = _finite(quality.get(key))
        if value is None or not 0 <= value <= 1:
            raise ValueError(f"质量门禁 {key} 必须介于 0 和 1")
        quality[key] = value
    minimum = _integer(quality.get("min_eligible_count"))
    if minimum is None or minimum < 1:
        raise ValueError("质量门禁 min_eligible_count 必须至少为 1")
    quality["min_eligible_count"] = minimum
    if normalized_factors and quality["min_factor_coverage"] != 1.0:
        raise ValueError("配置必需因子时 min_factor_coverage 必须为 1")
    if constraints["max_per_industry"] > 0 and quality["min_industry_coverage"] <= 0:
        raise ValueError("启用行业约束时 min_industry_coverage 必须大于 0")
    normalized["quality"] = quality

    costs = _merged_section(config, "costs")
    for key in ("commission_rate", "slippage_rate"):
        value = _finite(costs.get(key))
        if value is None or not 0 <= value <= 0.1:
            raise ValueError(f"成本参数 {key} 必须介于 0 和 0.1")
        costs[key] = value
    normalized["costs"] = costs
    normalized["limitations"] = RESEARCH_LIMITATIONS
    return normalized


def required_tushare_factors(config: dict[str, Any]) -> list[str]:
    return [str(item["id"]) for item in config.get("tushare_factors") or []]


def build_candidates(
    rows: Iterable[dict[str, Any]],
    config: dict[str, Any],
    *,
    target_trade_date: str,
) -> list[dict[str, Any]]:
    config = validate_config(config)
    target = datetime.strptime(target_trade_date, "%Y%m%d").date()
    history_days = int(config["history_days"])
    required_history_bars = history_days + 1
    min_listing_days = int(config.get("min_listing_days") or 0)
    min_avg_amount = float(config.get("min_avg_amount") or 0.0)
    factor_ids = required_tushare_factors(config)
    candidates: list[dict[str, Any]] = []

    for source in sorted(rows, key=lambda item: str(item.get("symbol") or "")):
        row = dict(source)
        symbol = str(row.get("symbol") or "").strip()
        exclusions: list[str] = []
        list_status = str(row.get("list_status") or "").upper()
        name = str(row.get("name") or symbol)
        if list_status != "L":
            exclusions.append("not_listed")
        if _ST_NAME_RE.search(name.strip().upper()):
            exclusions.append("st")

        list_date = str(row.get("list_date") or "")
        try:
            age_days = (target - datetime.strptime(list_date, "%Y%m%d").date()).days
        except ValueError:
            age_days = -1
        if age_days < min_listing_days:
            exclusions.append("listing_too_short")

        close_price = _finite(row.get("close_price"))
        pct_chg = _finite(row.get("pct_chg"))
        if close_price is None or close_price <= 0:
            exclusions.append("target_bar_missing")

        pe_ttm = _finite(row.get("pe_ttm"))
        pb = _finite(row.get("pb"))
        total_mv = _finite(row.get("total_mv"))
        circ_mv = _finite(row.get("circ_mv"))
        if pe_ttm is None or pb is None or total_mv is None:
            exclusions.append("daily_basic_missing")
        elif pe_ttm <= 0 or pb <= 0:
            exclusions.append("valuation_invalid")

        expected_dates = [str(value).replace("-", "") for value in row.get("expected_history_dates") or []]
        calendar_valid = (
            len(expected_dates) == required_history_bars
            and expected_dates == sorted(set(expected_dates))
            and expected_dates[-1] == target_trade_date
        )
        history = sorted(
            [
                {
                    "trade_date": str(item.get("trade_date") or "").replace("-", ""),
                    "close": _finite(item.get("close")),
                    "amount": _finite(item.get("amount")),
                }
                for item in row.get("history") or []
                if str(item.get("trade_date") or "").replace("-", "") <= target_trade_date
            ],
            key=lambda item: item["trade_date"],
        )[-required_history_bars:]
        closes = [item["close"] for item in history]
        liquidity_amounts = [item["amount"] for item in history[-history_days:]]
        history_complete = (
            calendar_valid
            and [item["trade_date"] for item in history] == expected_dates
            and all(value is not None and value > 0 for value in closes)
            and len(liquidity_amounts) == history_days
            and all(value is not None and value >= 0 for value in liquidity_amounts)
        )
        if not history_complete:
            exclusions.append("history_insufficient")
            avg_amount = momentum = volatility = None
        else:
            close_array = np.asarray(closes, dtype=float)
            valid_amounts = [
                value
                for value in liquidity_amounts
                if value is not None and value >= 0
            ]
            avg_amount = (
                float(np.mean(valid_amounts))
                if len(valid_amounts) == history_days
                else None
            )
            momentum = float(close_array[-1] / close_array[0] - 1.0)
            returns = np.diff(close_array) / close_array[:-1]
            volatility = (
                float(np.std(returns, ddof=1))
                if len(returns) > 1
                else 0.0
            )
            if avg_amount is None:
                exclusions.append("history_insufficient")
            elif avg_amount <= 0 or avg_amount < min_avg_amount:
                exclusions.append("liquidity_low")

        supplied_factors = row.get("factor_values") or {}
        factor_values: dict[str, float] = {}
        for factor_id in factor_ids:
            value = _finite(supplied_factors.get(factor_id))
            if value is None:
                exclusions.append("required_factor_missing")
            else:
                factor_values[factor_id] = value

        feature_values: dict[str, float] = {}
        if pe_ttm is not None and pe_ttm > 0:
            feature_values["value_pe"] = -math.log(pe_ttm)
        if pb is not None and pb > 0:
            feature_values["value_pb"] = -math.log(pb)
        if momentum is not None:
            feature_values["momentum_20"] = momentum
        if avg_amount is not None and avg_amount > 0:
            feature_values["liquidity_20"] = math.log(avg_amount)
        if volatility is not None:
            feature_values["low_volatility_20"] = -volatility
        feature_values.update(factor_values)

        candidate = {
            "symbol": symbol,
            "name": name,
            "market": row.get("market"),
            "list_status": list_status or None,
            "list_date": list_date or None,
            "industry": _text_or_none(row.get("industry")),
            "close_price": close_price,
            "pct_chg": pct_chg,
            "pe_ttm": pe_ttm,
            "pb": pb,
            "total_mv": total_mv,
            "circ_mv": circ_mv,
            "avg_amount_20": avg_amount,
            "momentum_20": momentum,
            "volatility_20": volatility,
            "factor_values": feature_values,
            "input_evidence": {"expected_history_dates": expected_dates, "history": history},
            "history_complete": history_complete,
            "z_scores": {},
            "eligible": not exclusions,
            "exclusion_codes": list(dict.fromkeys(exclusions)),
            "market_cap_bucket": None,
            "composite_score": None,
            "rank": None,
            "percentile": None,
            "selected": False,
            "research_weight": None,
            "change_state": "unchanged",
            "constraint_codes": [],
        }
        candidates.append(candidate)
    return candidates


def quality_gate(candidates: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    listed = [item for item in candidates if item.get("list_status") == "L"]
    denominator = len(listed)
    eligible_count = sum(bool(item.get("eligible")) for item in candidates)
    factors = required_tushare_factors(config)

    def ratio(count: int) -> float:
        return count / denominator if denominator else 0.0

    metrics: dict[str, float | int] = {
        "catalog_count": denominator,
        "bar_count": sum(item.get("close_price") is not None for item in listed),
        "basic_count": sum(
            item.get("pe_ttm") is not None and item.get("pb") is not None and item.get("total_mv") is not None
            for item in listed
        ),
        "history_count": sum(bool(item.get("history_complete")) for item in listed),
        "industry_count": sum(bool(item.get("industry")) for item in listed),
        "eligible_count": eligible_count,
    }
    metrics["bar_coverage"] = ratio(int(metrics["bar_count"]))
    metrics["basic_coverage"] = ratio(int(metrics["basic_count"]))
    metrics["history_coverage"] = ratio(int(metrics["history_count"]))
    metrics["industry_coverage"] = ratio(int(metrics["industry_count"]))
    factor_coverage: dict[str, float] = {}
    for factor_id in factors:
        count = sum(factor_id in (item.get("factor_values") or {}) for item in listed)
        factor_coverage[factor_id] = ratio(count)
    metrics["factor_coverage"] = factor_coverage

    thresholds = config.get("quality") or {}
    constraints = config.get("constraints") or {}
    failures: list[dict[str, Any]] = []
    _require_coverage(failures, "bar_coverage", metrics["bar_coverage"], thresholds.get("min_bar_coverage", 0.0))
    _require_coverage(failures, "basic_coverage", metrics["basic_coverage"], thresholds.get("min_basic_coverage", 0.0))
    _require_coverage(failures, "history_coverage", metrics["history_coverage"], thresholds.get("min_history_coverage", DEFAULT_STRATEGY_CONFIG["quality"]["min_history_coverage"]))
    if int(constraints.get("max_per_industry") or 0) > 0:
        _require_coverage(
            failures,
            "industry_coverage",
            metrics["industry_coverage"],
            thresholds.get("min_industry_coverage", 0.0),
        )
    for factor_id, coverage in factor_coverage.items():
        _require_coverage(
            failures,
            f"factor_coverage:{factor_id}",
            coverage,
            thresholds.get("min_factor_coverage", 0.0),
        )
    minimum = int(thresholds.get("min_eligible_count") or 0)
    if eligible_count < minimum:
        failures.append({"code": "eligible_count", "actual": eligible_count, "required": minimum})
    return {"passed": not failures, "metrics": metrics, "thresholds": thresholds, "failures": failures}


def score_candidates(candidates: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    eligible = [item for item in candidates if item.get("eligible")]
    if not eligible:
        return candidates
    weights = _normalized_weights(config)
    symbols = [item["symbol"] for item in eligible]
    raw = pd.DataFrame(
        {
            symbol: {key: (item.get("factor_values") or {}).get(key) for key in weights}
            for symbol, item in zip(symbols, eligible)
        }
    ).reindex(index=list(weights))
    if raw.isna().any().any():
        raise ValueError("合格池仍存在必需特征缺失，拒绝静默跳过")
    lower = float(config.get("winsor_lower", 0.01))
    upper = float(config.get("winsor_upper", 0.99))
    clipped = raw.copy()
    for feature in clipped.index:
        values = clipped.loc[feature]
        clipped.loc[feature] = values.clip(values.quantile(lower), values.quantile(upper))
    z_frame = _zscore_cross_section(clipped).fillna(0.0)
    score_by_symbol = {
        symbol: float(sum(float(z_frame.at[feature, symbol]) * weight for feature, weight in weights.items()))
        for symbol in symbols
    }
    by_symbol = {item["symbol"]: item for item in eligible}
    ranked_symbols = sorted(symbols, key=lambda symbol: (-score_by_symbol[symbol], symbol))
    total = len(ranked_symbols)
    _assign_market_cap_buckets(eligible)
    for rank, symbol in enumerate(ranked_symbols, start=1):
        item = by_symbol[symbol]
        item["z_scores"] = {feature: float(z_frame.at[feature, symbol]) for feature in weights}
        item["composite_score"] = score_by_symbol[symbol]
        item["rank"] = rank
        item["percentile"] = 1.0 if total == 1 else 1.0 - (rank - 1) / (total - 1)
    _select_top_n(eligible, config)
    return candidates


def apply_membership_changes(
    candidates: list[dict[str, Any]], previous_selected: Iterable[str],
) -> dict[str, int]:
    previous = {str(symbol) for symbol in previous_selected}
    current = {str(item["symbol"]) for item in candidates if item.get("selected")}
    by_symbol = {str(item["symbol"]): item for item in candidates}
    for symbol in current:
        by_symbol[symbol]["change_state"] = "retained" if symbol in previous else "added"
    for symbol in previous - current:
        if symbol in by_symbol:
            by_symbol[symbol]["change_state"] = "removed"
    return {
        "added": len(current - previous),
        "retained": len(current & previous),
        "removed": len(previous - current),
    }


def exclusion_summary(candidates: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in candidates:
        counts.update(str(code) for code in item.get("exclusion_codes") or [])
    return dict(sorted(counts.items()))


def fingerprint_payload(
    candidates: Iterable[dict[str, Any]],
    config: dict[str, Any],
    target_trade_date: str,
    *,
    previous_selection: dict[str, Any] | None = None,
) -> str:
    rows = []
    for item in sorted(candidates, key=lambda value: str(value.get("symbol") or "")):
        rows.append(
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "market": item.get("market"),
                "list_status": item.get("list_status"),
                "list_date": item.get("list_date"),
                "industry": item.get("industry"),
                "close_price": item.get("close_price"),
                "pct_chg": item.get("pct_chg"),
                "pe_ttm": item.get("pe_ttm"),
                "pb": item.get("pb"),
                "total_mv": item.get("total_mv"),
                "circ_mv": item.get("circ_mv"),
                "avg_amount_20": item.get("avg_amount_20"),
                "momentum_20": item.get("momentum_20"),
                "volatility_20": item.get("volatility_20"),
                "factor_values": item.get("factor_values"),
                "input_evidence": item.get("input_evidence"),
                "exclusion_codes": item.get("exclusion_codes"),
            }
        )
    return data_fingerprint(
        SCORING_IMPLEMENTATION_VERSION,
        target_trade_date,
        config,
        previous_selection or {},
        rows,
    )


def _select_top_n(eligible: list[dict[str, Any]], config: dict[str, Any]) -> None:
    constraints = config.get("constraints") or {}
    max_industry = int(constraints.get("max_per_industry") or 0)
    max_bucket = int(constraints.get("max_per_market_cap_bucket") or 0)
    top_n = int(config["top_n"])
    industry_counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    selected: list[dict[str, Any]] = []
    for item in sorted(eligible, key=lambda value: (int(value["rank"]), str(value["symbol"]))):
        industry = _text_or_none(item.get("industry"))
        bucket = _text_or_none(item.get("market_cap_bucket"))
        constraint_codes: list[str] = []
        if max_industry > 0 and not industry:
            constraint_codes.append("industry_missing")
        elif max_industry > 0 and industry_counts[industry] >= max_industry:
            constraint_codes.append("industry_limit")
        if max_bucket > 0 and bucket and bucket_counts[bucket] >= max_bucket:
            constraint_codes.append("market_cap_bucket_limit")
        item["constraint_codes"] = constraint_codes
        if constraint_codes:
            continue
        item["selected"] = True
        selected.append(item)
        if industry:
            industry_counts[industry] += 1
        if bucket:
            bucket_counts[bucket] += 1
        if len(selected) >= top_n:
            break
    weight = 1.0 / len(selected) if selected else None
    for item in selected:
        item["research_weight"] = weight


def _assign_market_cap_buckets(eligible: list[dict[str, Any]]) -> None:
    values = [float(item["total_mv"]) for item in eligible if item.get("total_mv") is not None]
    if not values:
        return
    low, high = np.quantile(np.asarray(values, dtype=float), [1 / 3, 2 / 3])
    for item in eligible:
        value = _finite(item.get("total_mv"))
        if value is None:
            continue
        item["market_cap_bucket"] = "small" if value <= low else "large" if value >= high else "mid"


def _normalized_weights(config: dict[str, Any]) -> dict[str, float]:
    configured = {str(key): float(value) for key, value in (config.get("feature_weights") or {}).items()}
    for spec in config.get("tushare_factors") or []:
        configured[str(spec["id"])] = float(spec["weight"])
    nonzero = {key: value for key, value in configured.items() if abs(value) > 1e-12}
    denominator = sum(abs(value) for value in nonzero.values())
    if denominator <= 1e-12:
        raise ValueError("策略权重绝对值之和不能为零")
    return {key: value / denominator for key, value in nonzero.items()}


def _require_coverage(
    failures: list[dict[str, Any]], code: str, actual: Any, required: Any,
) -> None:
    actual_value = float(actual or 0.0)
    required_value = float(required or 0.0)
    if actual_value + 1e-12 < required_value:
        failures.append({"code": code, "actual": actual_value, "required": required_value})


def _merged_section(config: dict[str, Any], key: str) -> dict[str, Any]:
    supplied = config.get(key)
    if supplied is not None and not isinstance(supplied, dict):
        raise ValueError(f"策略配置 {key} 必须是对象")
    defaults = copy.deepcopy(DEFAULT_STRATEGY_CONFIG[key])
    values = supplied or {}
    unknown = sorted(set(values) - set(defaults))
    if unknown:
        raise ValueError(f"策略配置 {key} 存在未知字段：{', '.join(unknown)}")
    defaults.update(values)
    return defaults


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    number = _finite(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
