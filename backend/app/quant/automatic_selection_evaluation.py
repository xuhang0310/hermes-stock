from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

import numpy as np

from backend.app.quant.automatic_selection_scoring import RESEARCH_LIMITATIONS

EVALUATION_HORIZONS = (5, 20, 60)


def evaluate_forward_window(
    *,
    target_trade_date: str,
    horizon: int,
    selected_symbols: Iterable[str],
    benchmark_symbols: Iterable[str],
    bars: Iterable[dict[str, Any]],
    trading_dates: Iterable[str],
    min_coverage: float,
    commission_rate: float,
    slippage_rate: float,
    turnover: float | None,
) -> dict[str, Any]:
    if horizon not in EVALUATION_HORIZONS:
        raise ValueError("评估周期只支持 5、20、60 个交易日")
    selected = sorted(set(str(symbol) for symbol in selected_symbols if symbol))
    benchmark = sorted(set(str(symbol) for symbol in benchmark_symbols if symbol))
    dates = sorted(set(str(value) for value in trading_dates if str(value) > target_trade_date))
    if len(dates) < horizon:
        return {
            "status": "pending",
            "entry_trade_date": dates[0] if dates else None,
            "exit_trade_date": None,
            "selected_count": len(selected),
            "covered_count": 0,
            "coverage_ratio": 0.0,
            "benchmark_count": len(benchmark),
            "benchmark_covered_count": 0,
            "benchmark_coverage_ratio": 0.0,
            "turnover": turnover,
            "commission_rate": commission_rate,
            "slippage_rate": slippage_rate,
            "daily_series": [],
            "per_symbol": [],
            "limitations": RESEARCH_LIMITATIONS,
            "error": f"未来交易日仅有 {len(dates)} 个，尚未达到 {horizon} 日评估周期",
        }

    evaluation_dates = dates[:horizon]
    entry_date = evaluation_dates[0]
    exit_date = evaluation_dates[-1]
    by_symbol: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    relevant = set(selected) | set(benchmark)
    for source in bars:
        symbol = str(source.get("symbol") or "")
        trade_date = str(source.get("trade_date") or "")
        if symbol not in relevant or trade_date not in evaluation_dates:
            continue
        open_price = _positive(source.get("open"))
        close_price = _positive(source.get("close"))
        if close_price is None:
            continue
        by_symbol[symbol][trade_date] = {
            "open": open_price,
            "close": close_price,
        }

    cost = _rate(commission_rate, "佣金率") + _rate(slippage_rate, "滑点率")
    selected_paths, selected_details = _build_paths(selected, by_symbol, evaluation_dates, cost)
    benchmark_paths, benchmark_details = _build_paths(benchmark, by_symbol, evaluation_dates, cost)
    selected_coverage = len(selected_paths) / len(selected) if selected else 0.0
    benchmark_coverage = len(benchmark_paths) / len(benchmark) if benchmark else 0.0
    base = {
        "entry_trade_date": entry_date,
        "exit_trade_date": exit_date,
        "selected_count": len(selected),
        "covered_count": len(selected_paths),
        "coverage_ratio": selected_coverage,
        "benchmark_count": len(benchmark),
        "benchmark_covered_count": len(benchmark_paths),
        "benchmark_coverage_ratio": benchmark_coverage,
        "turnover": turnover,
        "commission_rate": commission_rate,
        "slippage_rate": slippage_rate,
        "per_symbol": selected_details,
        "limitations": RESEARCH_LIMITATIONS,
    }
    coverage_failures = []
    if not selected:
        coverage_failures.append("本轮没有入选股票")
    elif selected_coverage + 1e-12 < min_coverage:
        coverage_failures.append(
            f"入选组合覆盖率 {selected_coverage:.2%} 低于门槛 {min_coverage:.2%}"
        )
    if not benchmark:
        coverage_failures.append("本轮没有合格池基准")
    elif benchmark_coverage + 1e-12 < min_coverage:
        coverage_failures.append(
            f"合格池基准覆盖率 {benchmark_coverage:.2%} 低于门槛 {min_coverage:.2%}"
        )
    if coverage_failures:
        return {
            **base,
            "status": "insufficient_data",
            "basket_return": None,
            "benchmark_return": None,
            "excess_return": None,
            "max_drawdown": None,
            "sharpe": None,
            "information_ratio": None,
            "daily_series": [],
            "error": "；".join(coverage_failures),
        }

    basket_values = np.mean(np.asarray(selected_paths, dtype=float), axis=0)
    benchmark_values = np.mean(np.asarray(benchmark_paths, dtype=float), axis=0)
    basket_returns = _returns_from_values(basket_values)
    benchmark_returns = _returns_from_values(benchmark_values)
    active_returns = basket_returns - benchmark_returns
    daily_series = [
        {
            "trade_date": trade_date,
            "basket_value": float(basket_values[index]),
            "benchmark_value": float(benchmark_values[index]),
            "basket_return": float(basket_returns[index]),
            "benchmark_return": float(benchmark_returns[index]),
            "active_return": float(active_returns[index]),
        }
        for index, trade_date in enumerate(evaluation_dates)
    ]
    return {
        **base,
        "status": "completed",
        "basket_return": float(basket_values[-1] - 1.0),
        "benchmark_return": float(benchmark_values[-1] - 1.0),
        "excess_return": float(basket_values[-1] - benchmark_values[-1]),
        "max_drawdown": _max_drawdown(basket_values),
        "sharpe": _annualized_ratio(basket_returns),
        "information_ratio": _annualized_ratio(active_returns),
        "daily_series": daily_series,
        "error": None,
    }


def calculate_turnover(current_symbols: Iterable[str], previous_symbols: Iterable[str]) -> float:
    current = set(str(symbol) for symbol in current_symbols if symbol)
    previous = set(str(symbol) for symbol in previous_symbols if symbol)
    if not current and not previous:
        return 0.0
    if not previous:
        return 1.0 if current else 0.0
    return 1.0 - len(current & previous) / max(len(current), len(previous))


def _build_paths(
    symbols: list[str],
    by_symbol: dict[str, dict[str, dict[str, float]]],
    dates: list[str],
    one_way_cost: float,
) -> tuple[list[list[float]], list[dict[str, Any]]]:
    paths: list[list[float]] = []
    details: list[dict[str, Any]] = []
    for symbol in symbols:
        symbol_bars = by_symbol.get(symbol) or {}
        missing_dates = [date for date in dates if date not in symbol_bars]
        entry_open = (symbol_bars.get(dates[0]) or {}).get("open")
        if entry_open is None:
            missing_dates = list(dict.fromkeys([dates[0], *missing_dates]))
        if missing_dates:
            details.append(
                {
                    "symbol": symbol,
                    "covered": False,
                    "missing_trade_dates": missing_dates,
                    "entry_price": entry_open,
                    "exit_price": None,
                    "net_return": None,
                }
            )
            continue
        closes = [float(symbol_bars[date]["close"]) for date in dates]
        values: list[float] = []
        wealth = 1.0 - one_way_cost
        previous_price = float(entry_open)
        for close_price in closes:
            wealth *= close_price / previous_price
            values.append(wealth)
            previous_price = close_price
        values[-1] *= 1.0 - one_way_cost
        paths.append(values)
        details.append(
            {
                "symbol": symbol,
                "covered": True,
                "missing_trade_dates": [],
                "entry_price": float(entry_open),
                "exit_price": closes[-1],
                "gross_return": float(closes[-1] / float(entry_open) - 1.0),
                "net_return": float(values[-1] - 1.0),
            }
        )
    return paths, details


def _returns_from_values(values: np.ndarray) -> np.ndarray:
    previous = np.concatenate(([1.0], values[:-1]))
    return values / previous - 1.0


def _max_drawdown(values: np.ndarray) -> float:
    curve = np.concatenate(([1.0], values))
    peaks = np.maximum.accumulate(curve)
    return float(np.min(curve / peaks - 1.0))


def _annualized_ratio(returns: np.ndarray) -> float:
    if len(returns) < 2:
        return 0.0
    std = float(np.std(returns, ddof=1))
    if std <= 1e-12:
        return 0.0
    value = float(np.mean(returns) / std * math.sqrt(252.0))
    return value if math.isfinite(value) else 0.0


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _rate(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是有限非负数") from exc
    if not math.isfinite(number) or not 0 <= number < 1:
        raise ValueError(f"{label}必须是有限非负数且小于 1")
    return number
