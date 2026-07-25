from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.models import BacktestRun
from backend.app.market.stock_tools import (
    get_index_daily,
    get_tushare_daily,
    normalize_a_share_symbol,
)


MAX_PORTFOLIO_SYMBOLS = 20
MAX_BACKTEST_BARS = 1600


@dataclass(frozen=True)
class PortfolioBacktestConfig:
    symbols: tuple[str, ...]
    start_date: str
    end_date: str
    strategy: str = "risk_parity"
    benchmark: str = "equal_weight"
    rebalance_frequency: str = "monthly"
    lookback_days: int = 60
    covariance_shrinkage: float = 0.1
    initial_cash: float = 1_000_000.0
    commission_rate: float = 0.0003
    slippage_rate: float = 0.0005


def _compact_date(value: str | None, fallback: date) -> str:
    raw = str(value or fallback.strftime("%Y%m%d")).strip().replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"日期格式不正确：{value!r}，应为 YYYYMMDD 或 YYYY-MM-DD")
    return raw


def normalize_portfolio_config(payload: dict[str, Any]) -> PortfolioBacktestConfig:
    raw_symbols = payload.get("symbols")
    if isinstance(raw_symbols, str):
        raw_symbols = [item.strip() for item in raw_symbols.replace("，", ",").split(",")]
    if not isinstance(raw_symbols, (list, tuple)):
        raise ValueError("组合回测必须提供 symbols 数组")
    symbols: list[str] = []
    for raw in raw_symbols:
        symbol = normalize_a_share_symbol(str(raw or "").strip())
        if not symbol.endswith((".SH", ".SZ", ".BJ")):
            raise ValueError(f"无法识别 A 股代码：{raw}")
        if symbol not in symbols:
            symbols.append(symbol)
    if not 2 <= len(symbols) <= MAX_PORTFOLIO_SYMBOLS:
        raise ValueError(f"组合回测需要 2 至 {MAX_PORTFOLIO_SYMBOLS} 个不重复标的")

    today = date.today()
    start = _compact_date(payload.get("start_date"), today - timedelta(days=730))
    end = _compact_date(payload.get("end_date"), today)
    if start >= end:
        raise ValueError("开始日期必须早于结束日期")

    frequency = str(payload.get("rebalance_frequency") or "monthly").strip().lower()
    if frequency not in {"weekly", "monthly", "quarterly"}:
        raise ValueError("调仓频率只支持 weekly、monthly 或 quarterly")
    lookback = int(payload.get("lookback_days") or 60)
    if not 20 <= lookback <= 252:
        raise ValueError("风险估计窗口必须在 20 至 252 个交易日之间")
    shrinkage = float(payload.get("covariance_shrinkage") or 0.1)
    if not 0 <= shrinkage <= 1:
        raise ValueError("协方差收缩系数必须在 0 至 1 之间")
    initial_cash = float(payload.get("initial_cash") or 1_000_000)
    if not 10_000 <= initial_cash <= 1_000_000_000:
        raise ValueError("初始资金必须在 1 万至 10 亿元之间")

    return PortfolioBacktestConfig(
        symbols=tuple(symbols),
        start_date=start,
        end_date=end,
        rebalance_frequency=frequency,
        lookback_days=lookback,
        covariance_shrinkage=shrinkage,
        initial_cash=initial_cash,
        commission_rate=float(payload.get("commission_rate") or 0.0003),
        slippage_rate=float(payload.get("slippage_rate") or 0.0005),
    )


def load_portfolio_market_data(
    config: PortfolioBacktestConfig,
) -> tuple[pd.DataFrame, pd.Series | None, list[str]]:
    start = datetime.strptime(config.start_date, "%Y%m%d").date()
    history_start = start - timedelta(days=max(120, config.lookback_days * 2))
    closes: list[pd.Series] = []
    warnings: list[str] = []
    for symbol in config.symbols:
        response = get_tushare_daily(
            symbol,
            start_date=history_start.strftime("%Y%m%d"),
            end_date=config.end_date,
            limit=MAX_BACKTEST_BARS,
        )
        if response.get("error"):
            raise ValueError(f"获取 {symbol} 日线失败：{response['error']}")
        frame = pd.DataFrame(response.get("bars") or [])
        if frame.empty or not {"trade_date", "close"}.issubset(frame.columns):
            raise ValueError(f"{symbol} 缺少可用于组合回测的收盘价")
        frame["trade_date"] = pd.to_datetime(frame["trade_date"].astype(str))
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        series = (
            frame.dropna(subset=["trade_date", "close"])
            .sort_values("trade_date")
            .drop_duplicates("trade_date", keep="last")
            .set_index("trade_date")["close"]
            .rename(symbol)
        )
        closes.append(series)

    prices = pd.concat(closes, axis=1, join="inner").dropna()
    if prices.empty:
        raise ValueError("多个标的之间没有可对齐的共同交易日")
    requested = prices.loc[
        (prices.index >= pd.Timestamp(config.start_date))
        & (prices.index <= pd.Timestamp(config.end_date))
    ]
    if len(requested) < 20:
        raise ValueError(f"回测区间内只有 {len(requested)} 个共同交易日，至少需要 20 个")
    available_history = int((prices.index < requested.index[0]).sum())
    if available_history < config.lookback_days:
        warnings.append(
            f"首期调仓前只有 {available_history} 个共同历史交易日，"
            f"少于设定的 {config.lookback_days} 日窗口，已使用全部可用历史。"
        )
    market_returns: pd.Series | None = None
    benchmark = get_index_daily(
        "000300.SH",
        start_date=config.start_date,
        end_date=config.end_date,
    )
    if benchmark.get("error"):
        warnings.append("沪深300行情获取失败，Beta/Alpha 市场回归不可用。")
    else:
        benchmark_frame = pd.DataFrame(benchmark.get("rows") or [])
        if {"trade_date", "close"}.issubset(benchmark_frame.columns):
            benchmark_frame["trade_date"] = pd.to_datetime(
                benchmark_frame["trade_date"].astype(str)
            )
            benchmark_frame["close"] = pd.to_numeric(
                benchmark_frame["close"], errors="coerce"
            )
            market_returns = (
                benchmark_frame.dropna(subset=["trade_date", "close"])
                .sort_values("trade_date")
                .drop_duplicates("trade_date", keep="last")
                .set_index("trade_date")["close"]
                .pct_change()
            )
        else:
            warnings.append("沪深300行情字段不完整，Beta/Alpha 市场回归不可用。")
    return prices, market_returns, warnings


def equal_risk_contribution_weights(covariance: np.ndarray) -> np.ndarray:
    covariance = np.asarray(covariance, dtype=float)
    count = covariance.shape[0]
    if covariance.shape != (count, count) or count < 2:
        raise ValueError("协方差矩阵维度不正确")
    diagonal = np.clip(np.diag(covariance), 1e-12, None)
    covariance = covariance.copy()
    covariance[np.diag_indices(count)] = diagonal
    budgets = np.full(count, 1.0 / count)
    weights = 1.0 / np.sqrt(diagonal)
    weights /= weights.sum()

    for _ in range(1000):
        previous = weights.copy()
        for index in range(count):
            variance = max(float(covariance[index, index]), 1e-12)
            cross = float(covariance[index] @ weights - variance * weights[index])
            discriminant = max(cross * cross + 4 * variance * budgets[index], 0.0)
            weights[index] = max((-cross + math.sqrt(discriminant)) / (2 * variance), 1e-12)
        if np.max(np.abs(weights - previous)) < 1e-10:
            break
    weights = np.clip(weights, 0.0, None)
    return weights / weights.sum()


def _rebalance_key(timestamp: pd.Timestamp, frequency: str) -> tuple[int, int]:
    if frequency == "weekly":
        iso = timestamp.isocalendar()
        return int(iso.year), int(iso.week)
    if frequency == "quarterly":
        return timestamp.year, (timestamp.month - 1) // 3 + 1
    return timestamp.year, timestamp.month


def _risk_weights(history: pd.DataFrame, config: PortfolioBacktestConfig) -> tuple[np.ndarray, np.ndarray]:
    sample = history.tail(config.lookback_days)
    if len(sample) < 10:
        return np.full(len(config.symbols), 1.0 / len(config.symbols)), np.full(
            len(config.symbols), 1.0 / len(config.symbols)
        )
    covariance = sample.cov().to_numpy(dtype=float) * 252
    diagonal = np.diag(np.diag(covariance))
    covariance = (
        (1 - config.covariance_shrinkage) * covariance
        + config.covariance_shrinkage * diagonal
    )
    weights = equal_risk_contribution_weights(covariance)
    portfolio_variance = max(float(weights @ covariance @ weights), 1e-12)
    contributions = weights * (covariance @ weights) / portfolio_variance
    return weights, contributions


def _regression_metrics(strategy: pd.Series, benchmark: pd.Series) -> dict[str, float | None]:
    aligned = pd.concat(
        [strategy.rename("strategy"), benchmark.rename("benchmark")],
        axis=1,
        join="inner",
    ).dropna()
    if len(aligned) < 3:
        return {
            "information_ratio": None,
            "alpha_daily": None,
            "alpha_annual": None,
            "alpha_t_stat": None,
            "beta": None,
            "r_squared": None,
        }
    x = aligned["benchmark"].to_numpy(dtype=float)
    y = aligned["strategy"].to_numpy(dtype=float)
    design = np.column_stack([np.ones(len(x)), x])
    coefficients, _, _, _ = np.linalg.lstsq(design, y, rcond=None)
    alpha_daily, beta = float(coefficients[0]), float(coefficients[1])
    fitted = design @ coefficients
    residual = y - fitted
    ss_total = float(np.square(y - y.mean()).sum())
    r_squared = 1 - float(np.square(residual).sum()) / ss_total if ss_total > 0 else 0.0
    alpha_t_stat = 0.0
    if len(x) > 2:
        sigma2 = float(np.square(residual).sum()) / (len(x) - 2)
        covariance = sigma2 * np.linalg.pinv(design.T @ design)
        alpha_se = math.sqrt(max(float(covariance[0, 0]), 0.0))
        alpha_t_stat = alpha_daily / alpha_se if alpha_se > 0 else 0.0
    active = aligned["strategy"] - aligned["benchmark"]
    active_std = float(active.std(ddof=1))
    information_ratio = (
        float(active.mean()) / active_std * math.sqrt(252) if active_std > 0 else 0.0
    )
    return {
        "information_ratio": round(information_ratio, 8),
        "alpha_daily": round(alpha_daily, 8),
        "alpha_annual": round((1 + alpha_daily) ** 252 - 1, 8),
        "alpha_t_stat": round(alpha_t_stat, 8),
        "beta": round(beta, 8),
        "r_squared": round(r_squared, 8),
    }


def _standalone_metrics(equity: pd.Series, initial_cash: float) -> dict[str, Any]:
    returns = equity.pct_change().fillna(0.0)
    total_return = float(equity.iloc[-1] / initial_cash - 1)
    periods = max(len(equity) - 1, 1)
    annual_return = float((max(equity.iloc[-1], 0.01) / initial_cash) ** (252 / periods) - 1)
    volatility = float(returns.std(ddof=1))
    sharpe = float(returns.mean()) / volatility * math.sqrt(252) if volatility > 0 else 0.0
    downside = returns[returns < 0]
    downside_std = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    sortino = float(returns.mean()) / downside_std * math.sqrt(252) if downside_std > 0 else 0.0
    drawdown = equity / equity.cummax() - 1
    max_drawdown = float(drawdown.min())
    calmar = annual_return / abs(max_drawdown) if max_drawdown < 0 else 0.0
    positive = returns[returns > 0]
    negative = returns[returns < 0]
    profit_loss_ratio = (
        float(positive.mean()) / abs(float(negative.mean()))
        if len(positive) and len(negative)
        else 0.0
    )
    return {
        "initial_cash": round(initial_cash, 8),
        "final_equity": round(float(equity.iloc[-1]), 8),
        "total_return": round(total_return, 8),
        "annual_return": round(annual_return, 8),
        "annual_volatility": round(volatility * math.sqrt(252), 8),
        "max_drawdown": round(max_drawdown, 8),
        "sharpe": round(sharpe, 8),
        "sortino": round(sortino, 8),
        "calmar": round(calmar, 8),
        "win_rate": round(len(positive) / max(len(returns) - 1, 1), 8),
        "profit_loss_ratio": round(profit_loss_ratio, 8),
        "winning_trades": len(positive),
        "losing_trades": len(negative),
    }


def _portfolio_metrics(
    equity: pd.Series,
    benchmark_equity: pd.Series,
    initial_cash: float,
    rebalance_count: int,
    total_turnover: float,
    total_cost: float,
    market_returns: pd.Series | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    metrics = _standalone_metrics(equity, initial_cash)
    benchmark_metrics = _standalone_metrics(benchmark_equity, initial_cash)
    strategy_returns = equity.pct_change().fillna(0.0)
    equal_weight_returns = benchmark_equity.pct_change().fillna(0.0)
    equal_weight_regression = _regression_metrics(strategy_returns, equal_weight_returns)
    market_regression = (
        _regression_metrics(strategy_returns, market_returns)
        if market_returns is not None
        else {
            "information_ratio": None,
            "alpha_daily": None,
            "alpha_annual": None,
            "alpha_t_stat": None,
            "beta": None,
            "r_squared": None,
        }
    )
    metrics.update(
        {
            "trade_count": rebalance_count,
            "rebalance_count": rebalance_count,
            "avg_holding_days": round(
                max(len(equity) - 1, 1) / max(rebalance_count, 1), 8
            ),
            "turnover": round(total_turnover, 8),
            "total_cost": round(total_cost, 2),
            "benchmark_return": benchmark_metrics["total_return"],
            "excess_return": round(
                metrics["total_return"] - benchmark_metrics["total_return"], 8
            ),
            "information_ratio": equal_weight_regression["information_ratio"],
            "alpha_daily": market_regression["alpha_daily"],
            "alpha_annual": market_regression["alpha_annual"],
            "alpha_t_stat": market_regression["alpha_t_stat"],
            "beta": market_regression["beta"],
            "r_squared": market_regression["r_squared"],
        }
    )
    benchmark_metrics.update(
        {
            "trade_count": rebalance_count,
            "rebalance_count": rebalance_count,
            "avg_holding_days": round(
                max(len(benchmark_equity) - 1, 1) / max(rebalance_count, 1), 8
            ),
        }
    )
    return metrics, benchmark_metrics


def _regime_analysis(
    strategy_returns: pd.Series,
    market_returns: pd.Series | None,
) -> dict[str, Any]:
    if market_returns is None:
        return {"status": "skipped", "reason": "沪深300行情不可用，无法划分市场状态。"}
    aligned = pd.concat(
        [strategy_returns.rename("strategy"), market_returns.rename("market")],
        axis=1,
        join="inner",
    ).dropna()
    if len(aligned) < 504:
        return {
            "status": "skipped",
            "reason": f"只有 {len(aligned)} 个共同交易日，至少需要 504 个交易日才能进行可靠的市场状态分解。",
        }
    market_regime = (1 + aligned["market"]).rolling(60).apply(np.prod, raw=True) - 1
    rows: list[dict[str, Any]] = []
    for code, label, mask in (
        ("risk_on", "风险偏好", market_regime >= 0),
        ("risk_off", "风险规避", market_regime < 0),
    ):
        sample = aligned.loc[mask.fillna(False), "strategy"]
        volatility = float(sample.std(ddof=1))
        rows.append(
            {
                "code": code,
                "label": label,
                "trading_days": len(sample),
                "annual_return": round(float(sample.mean()) * 252, 8) if len(sample) else None,
                "sharpe": (
                    round(float(sample.mean()) / volatility * math.sqrt(252), 8)
                    if len(sample) > 1 and volatility > 0
                    else None
                ),
            }
        )
    return {"status": "completed", "window_days": 60, "regimes": rows}


def _bootstrap_significance(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    iterations: int = 500,
    block_size: int = 5,
) -> dict[str, Any]:
    aligned = pd.concat(
        [strategy_returns.rename("strategy"), benchmark_returns.rename("benchmark")],
        axis=1,
        join="inner",
    ).dropna()
    active = aligned["strategy"] - aligned["benchmark"]
    if len(active) < 60:
        return {
            "status": "skipped",
            "reason": f"只有 {len(active)} 个有效日收益，至少需要 60 个才能进行区块自助法检验。",
        }
    observed = float(active.mean()) * 252
    centered = active.to_numpy(dtype=float) - float(active.mean())
    rng = np.random.default_rng(42)
    simulated: list[float] = []
    block_count = math.ceil(len(centered) / block_size)
    for _ in range(iterations):
        starts = rng.integers(0, len(centered), size=block_count)
        sample = np.concatenate(
            [
                np.take(centered, np.arange(start, start + block_size), mode="wrap")
                for start in starts
            ]
        )[: len(centered)]
        simulated.append(float(sample.mean()) * 252)
    p_value = (sum(abs(value) >= abs(observed) for value in simulated) + 1) / (
        iterations + 1
    )

    strategy = aligned["strategy"].to_numpy(dtype=float)
    actual_curve = np.cumprod(1 + strategy)
    actual_drawdown = float(np.min(actual_curve / np.maximum.accumulate(actual_curve) - 1))
    random_drawdowns: list[float] = []
    for _ in range(iterations):
        shuffled = rng.permutation(strategy)
        curve = np.cumprod(1 + shuffled)
        random_drawdowns.append(
            float(np.min(curve / np.maximum.accumulate(curve) - 1))
        )
    drawdown_percentile = (
        sum(value <= actual_drawdown for value in random_drawdowns) + 1
    ) / (iterations + 1)
    return {
        "status": "completed",
        "method": "5日区块自助法检验主动收益；随机排列检验回撤路径",
        "iterations": iterations,
        "annual_excess_return": round(observed, 8),
        "excess_return_p_value": round(p_value, 8),
        "significant": p_value < 0.05,
        "actual_max_drawdown": round(actual_drawdown, 8),
        "drawdown_worse_percentile": round(drawdown_percentile, 8),
    }


def run_portfolio_backtest(
    config: PortfolioBacktestConfig,
    prices: pd.DataFrame,
    market_returns: pd.Series | None = None,
    initial_warnings: list[str] | None = None,
) -> dict[str, Any]:
    prices = prices.loc[:, list(config.symbols)].sort_index().dropna()
    returns = prices.pct_change()
    test_prices = prices.loc[
        (prices.index >= pd.Timestamp(config.start_date))
        & (prices.index <= pd.Timestamp(config.end_date))
    ]
    if len(test_prices) < 20:
        raise ValueError("组合回测至少需要 20 个共同交易日")

    count = len(config.symbols)
    strategy_weights = np.zeros(count)
    benchmark_weights = np.zeros(count)
    strategy_equity = config.initial_cash
    benchmark_equity = config.initial_cash
    cost_rate = config.commission_rate + config.slippage_rate
    previous_key: tuple[int, int] | None = None
    equity_rows: list[dict[str, Any]] = []
    benchmark_rows: list[dict[str, Any]] = []
    weight_series: list[dict[str, Any]] = []
    rebalance_history: list[dict[str, Any]] = []
    contribution_pnl = np.zeros(count)
    total_turnover = 0.0
    total_cost = 0.0

    for timestamp in test_prices.index:
        key = _rebalance_key(timestamp, config.rebalance_frequency)
        if key != previous_key:
            historical = returns.loc[returns.index < timestamp].dropna()
            target, risk_contributions = _risk_weights(historical, config)
            equal_target = np.full(count, 1.0 / count)
            turnover = float(np.abs(target - strategy_weights).sum())
            benchmark_turnover = float(np.abs(equal_target - benchmark_weights).sum())
            cost = strategy_equity * turnover * cost_rate
            benchmark_cost = benchmark_equity * benchmark_turnover * cost_rate
            strategy_equity -= cost
            benchmark_equity -= benchmark_cost
            strategy_weights = target
            benchmark_weights = equal_target
            weights_dict = {
                symbol: round(float(weight), 8)
                for symbol, weight in zip(config.symbols, strategy_weights)
            }
            rebalance_history.append(
                {
                    "time": timestamp.strftime("%Y-%m-%d"),
                    "turnover": round(turnover, 8),
                    "cost": round(cost, 2),
                    "weights": weights_dict,
                    "risk_contributions": {
                        symbol: round(float(value), 8)
                        for symbol, value in zip(config.symbols, risk_contributions)
                    },
                }
            )
            weight_series.append({"time": timestamp.strftime("%Y-%m-%d"), "weights": weights_dict})
            previous_key = key
            total_turnover += turnover
            total_cost += cost

        day_returns = returns.loc[timestamp].fillna(0.0).to_numpy(dtype=float)
        contribution_pnl += strategy_equity * strategy_weights * day_returns
        strategy_day_return = float(strategy_weights @ day_returns)
        benchmark_day_return = float(benchmark_weights @ day_returns)
        strategy_equity *= 1 + strategy_day_return
        benchmark_equity *= 1 + benchmark_day_return
        strategy_denominator = max(1 + strategy_day_return, 1e-12)
        benchmark_denominator = max(1 + benchmark_day_return, 1e-12)
        strategy_weights = strategy_weights * (1 + day_returns) / strategy_denominator
        benchmark_weights = benchmark_weights * (1 + day_returns) / benchmark_denominator
        equity_rows.append({"time": timestamp.strftime("%Y-%m-%d"), "equity": round(strategy_equity, 2)})
        benchmark_rows.append({"time": timestamp.strftime("%Y-%m-%d"), "equity": round(benchmark_equity, 2)})

    equity = pd.Series(
        [row["equity"] for row in equity_rows],
        index=pd.DatetimeIndex([row["time"] for row in equity_rows]),
        dtype=float,
    )
    benchmark_equity_series = pd.Series(
        [row["equity"] for row in benchmark_rows],
        index=pd.DatetimeIndex([row["time"] for row in benchmark_rows]),
        dtype=float,
    )
    drawdown = equity / equity.cummax() - 1
    for index, value in enumerate(drawdown):
        equity_rows[index]["drawdown"] = round(float(value), 8)

    metrics, benchmark_metrics = _portfolio_metrics(
        equity,
        benchmark_equity_series,
        config.initial_cash,
        len(rebalance_history),
        total_turnover,
        total_cost,
        market_returns,
    )
    trading_days = len(test_prices)
    confidence = "medium" if trading_days >= 252 else "low" if trading_days >= 120 else "insufficient"
    rating = {
        "medium": "中等置信度",
        "low": "低置信度",
        "insufficient": "样本不足",
    }[confidence]
    warnings = list(initial_warnings or [])
    warnings.extend(
        [
            "风险平价采用仅做多的等风险贡献（ERC）权重，并对样本协方差进行收缩。",
            "每个调仓日只使用此前可见的收益率估计风险，避免未来函数。",
            "组合按权重级别模拟并计入佣金与滑点，未模拟 A 股整手约束、涨跌停和停牌成交失败。",
            "等权基准使用相同标的、相同调仓频率和相同成本口径。",
            "统计显著性使用主动收益的 5 日区块自助法；不会使用对 Sharpe 数学上无效的简单收益顺序置换。",
        ]
    )
    if confidence != "medium":
        warnings.append("回测区间不足一个完整的 252 交易日，年化指标仅供描述。")

    normalized = test_prices / test_prices.iloc[0] * 100
    asset_curves = [
        {
            "symbol": symbol,
            "points": [
                {"time": timestamp.strftime("%Y-%m-%d"), "value": round(float(value), 6)}
                for timestamp, value in normalized[symbol].items()
            ],
        }
        for symbol in config.symbols
    ]
    average_weights = {
        symbol: float(
            np.mean([row["weights"][symbol] for row in rebalance_history])
        )
        for symbol in config.symbols
    }
    average_risk = {
        symbol: float(
            np.mean([row["risk_contributions"][symbol] for row in rebalance_history])
        )
        for symbol in config.symbols
    }
    asset_stats = [
        {
            "symbol": symbol,
            "asset_return": round(float(test_prices[symbol].iloc[-1] / test_prices[symbol].iloc[0] - 1), 8),
            "average_weight": round(average_weights[symbol], 8),
            "average_risk_contribution": round(average_risk[symbol], 8),
            "pnl_contribution": round(float(contribution_pnl[index]), 2),
            "return_contribution": round(
                float(contribution_pnl[index]) / config.initial_cash, 8
            ),
        }
        for index, symbol in enumerate(config.symbols)
    ]
    ranked_contributions = sorted(
        asset_stats,
        key=lambda item: item["pnl_contribution"],
        reverse=True,
    )
    net_profit = metrics["final_equity"] - config.initial_cash
    positive_contribution = sum(
        max(float(item["pnl_contribution"]), 0.0) for item in asset_stats
    )
    top_contributor = ranked_contributions[0] if ranked_contributions else None
    concentration = {
        "net_profit": round(net_profit, 2),
        "top_contributor": top_contributor,
        "top_positive_share": (
            round(float(top_contributor["pnl_contribution"]) / positive_contribution, 8)
            if top_contributor and positive_contribution > 0
            else None
        ),
        "remove_top_contributor_pnl": (
            round(net_profit - float(top_contributor["pnl_contribution"]), 2)
            if top_contributor
            else None
        ),
        "total_cost": round(total_cost, 2),
    }
    strategy_returns = equity.pct_change().fillna(0.0)
    equal_weight_returns = benchmark_equity_series.pct_change().fillna(0.0)
    regime_analysis = _regime_analysis(strategy_returns, market_returns)
    statistical_tests = _bootstrap_significance(
        strategy_returns,
        equal_weight_returns,
    )
    fingerprint_payload = json.dumps(
        {
            "prices": {
                symbol: [round(float(value), 6) for value in test_prices[symbol]]
                for symbol in config.symbols
            },
            "dates": [timestamp.strftime("%Y-%m-%d") for timestamp in test_prices.index],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "status": "completed",
        "report_type": "portfolio",
        "strategy": {
            "code": "risk_parity",
            "name": "风险平价组合",
            "timeframe": "1D",
            "source": "Hermes deterministic portfolio backtest engine",
        },
        "symbol": f"组合({count})",
        "symbols": list(config.symbols),
        "benchmark_label": "等权组合",
        "period": {
            "start_date": test_prices.index[0].strftime("%Y-%m-%d"),
            "end_date": test_prices.index[-1].strftime("%Y-%m-%d"),
            "trading_days": trading_days,
        },
        "config": asdict(config),
        "metrics": metrics,
        "benchmark_metrics": benchmark_metrics,
        "confidence": confidence,
        "rating": rating,
        "warnings": warnings,
        "price_series": [],
        "trade_markers": [],
        "equity_curve": equity_rows,
        "benchmark_curve": benchmark_rows,
        "asset_curves": asset_curves,
        "weight_series": weight_series,
        "rebalance_history": rebalance_history,
        "trades": [],
        "attribution": {
            "top_winner": None,
            "top_loser": None,
            "remove_top_winner_pnl": None,
            "exit_reasons": [],
            "asset_stats": asset_stats,
            "concentration": concentration,
            "market_regression": {
                "benchmark": "000300.SH",
                "beta": metrics.get("beta"),
                "alpha_annual": metrics.get("alpha_annual"),
                "alpha_t_stat": metrics.get("alpha_t_stat"),
                "r_squared": metrics.get("r_squared"),
            },
            "regime_analysis": regime_analysis,
            "statistical_tests": statistical_tests,
        },
        "data_fingerprint": hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest(),
    }


def portfolio_source_code(config: PortfolioBacktestConfig) -> str:
    return (
        "# Long-only equal-risk-contribution portfolio\n"
        f"symbols = {list(config.symbols)!r}\n"
        f"lookback_days = {config.lookback_days}\n"
        f"rebalance_frequency = {config.rebalance_frequency!r}\n"
        f"covariance_shrinkage = {config.covariance_shrinkage}\n"
        "cov = (1 - shrinkage) * sample_cov + shrinkage * diag(sample_cov)\n"
        "weights = equal_risk_contribution(cov)\n"
        "# Covariance uses only returns strictly before each rebalance date.\n"
    )


def portfolio_summary_markdown(run_id: str, result: dict[str, Any]) -> str:
    metrics = result["metrics"]
    period = result["period"]
    symbols = "、".join(result["symbols"])
    return (
        f"### 风险平价组合回测完成 · {symbols}\n\n"
        f"- 区间：{period['start_date']} 至 {period['end_date']}（{period['trading_days']} 个交易日）\n"
        f"- 总收益：{metrics['total_return'] * 100:+.2f}% · 等权基准：{metrics['benchmark_return'] * 100:+.2f}%\n"
        f"- 超额收益：{metrics['excess_return'] * 100:+.2f}% · Sharpe：{metrics['sharpe']:.2f}\n"
        f"- 最大回撤：{metrics['max_drawdown'] * 100:.2f}% · 调仓：{metrics['rebalance_count']} 次\n"
        f"- 置信度：**{result['rating']}**\n\n"
        f"[查看完整回测报告](/backtests/{run_id})"
    )


def create_portfolio_backtest(
    payload: dict[str, Any],
    *,
    owner_user_id: int | None = None,
    agent_run_id: str | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    config = normalize_portfolio_config(payload)
    prices, market_returns, warnings = load_portfolio_market_data(config)
    result = run_portfolio_backtest(config, prices, market_returns, warnings)
    run_id = uuid.uuid4().hex
    result["run_id"] = run_id
    result["report_url"] = f"/backtests/{run_id}"
    summary = portfolio_summary_markdown(run_id, result)
    source_code = portfolio_source_code(config)

    owns_session = db is None
    session = db or SessionLocal()
    try:
        session.add(
            BacktestRun(
                run_id=run_id,
                owner_user_id=owner_user_id,
                agent_run_id=agent_run_id,
                status="completed",
                symbol="PORTFOLIO",
                strategy_code="risk_parity",
                timeframe="1D",
                start_date=result["period"]["start_date"],
                end_date=result["period"]["end_date"],
                confidence=result["confidence"],
                config_json=json.dumps(result["config"], ensure_ascii=False),
                metrics_json=json.dumps(result["metrics"], ensure_ascii=False),
                result_json=json.dumps(result, ensure_ascii=False),
                summary_markdown=summary,
                source_code=source_code,
                data_fingerprint=result["data_fingerprint"],
                finished_at=datetime.now(),
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        if owns_session:
            session.close()
    return {**result, "summary_markdown": summary, "source_code": source_code}
