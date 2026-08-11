"""Performance metrics for backtest evaluation.

Comprehensive metrics covering:
- Returns: total, annual, monthly
- Risk: max drawdown, volatility, downside risk
- Risk-adjusted: Sharpe, Sortino, Calmar
- Trade quality: win rate, profit factor, avg holding
- Relative: alpha, beta, information ratio
- Robustness: bootstrapped confidence intervals
"""

from __future__ import annotations

import math
import warnings
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backend.app.quant.backtest_models import TradeRecord


def calc_metrics(
    equity_curve: pd.Series,
    trades: List[TradeRecord],
    initial_cash: float,
    bars_per_year: int,
    bench_ret: Optional[pd.Series] = None,
    target_pos: Optional[pd.DataFrame] = None,
    turnover_series: Optional[pd.Series] = None,
    risk_free_rate: float = 0.03,
) -> Dict[str, Any]:
    """Calculate comprehensive performance metrics.

    Args:
        equity_curve: Daily equity values.
        trades: List of completed trades.
        initial_cash: Starting capital.
        bars_per_year: Annualisation factor (252 for daily).
        bench_ret: Benchmark daily returns (optional).
        target_pos: Target position weights (optional, for concentration).
        turnover_series: Daily turnover (optional).
        risk_free_rate: Annual risk-free rate.

    Returns:
        Dict of performance metrics.
    """
    if len(equity_curve) < 2:
        return {"error": "数据不足，无法计算指标"}

    # Basic returns
    total_return = equity_curve.iloc[-1] / initial_cash - 1
    n_days = (equity_curve.index[-1] - equity_curve.index[0]).days
    n_years = n_days / 365.25 if n_days > 0 else 1.0
    # Handle total_return < -1 (lost more than 100%) to avoid complex numbers
    if total_return <= -1.0:
        annual_return = -1.0  # Total loss
    elif n_years > 0:
        annual_return = (1 + total_return) ** (1 / n_years) - 1
    else:
        annual_return = 0.0

    # Daily returns
    port_ret = equity_curve.pct_change().fillna(0.0)

    # Risk metrics
    volatility = port_ret.std() * math.sqrt(bars_per_year) if len(port_ret) > 1 else 0.0
    downside_ret = port_ret[port_ret < 0]
    downside_risk = downside_ret.std() * math.sqrt(bars_per_year) if len(downside_ret) > 1 else 0.0

    # Drawdown
    peak = equity_curve.cummax()
    drawdown = (equity_curve - peak) / peak.replace(0, 1)
    max_drawdown = drawdown.min()

    # Risk-adjusted metrics
    daily_rf = (1 + risk_free_rate) ** (1 / bars_per_year) - 1
    excess_ret = port_ret - daily_rf
    sharpe = (excess_ret.mean() / excess_ret.std() * math.sqrt(bars_per_year)
              if excess_ret.std() > 1e-10 else 0.0)
    # Sortino: use excess returns for downside risk calculation (consistent with Sharpe)
    downside_excess = excess_ret[excess_ret < 0]
    downside_std = downside_excess.std() if len(downside_excess) > 0 else 0.0
    sortino = (excess_ret.mean() / downside_std * math.sqrt(bars_per_year)
               if downside_std > 1e-10 else 0.0)
    calmar = annual_return / abs(max_drawdown) if abs(max_drawdown) > 1e-10 else 0.0

    # Trade statistics
    n_trades = len(trades)
    if n_trades > 0:
        winners = [t for t in trades if t.pnl > 0]
        losers = [t for t in trades if t.pnl <= 0]
        win_rate = len(winners) / n_trades
        gross_profit = sum(t.pnl for t in winners)
        gross_loss = abs(sum(t.pnl for t in losers))
        profit_factor = gross_profit / gross_loss if gross_loss > 1e-10 else float("inf")
        avg_pnl = sum(t.pnl for t in trades) / n_trades
        avg_pnl_pct = sum(t.pnl_pct for t in trades) / n_trades
        avg_holding_bars = sum(t.holding_bars for t in trades) / n_trades
        max_consecutive_wins = _max_consecutive(trades, win=True)
        max_consecutive_losses = _max_consecutive(trades, win=False)
    else:
        win_rate = 0.0
        profit_factor = 0.0
        avg_pnl = 0.0
        avg_pnl_pct = 0.0
        avg_holding_bars = 0.0
        max_consecutive_wins = 0
        max_consecutive_losses = 0

    # Relative performance (vs benchmark)
    alpha = 0.0
    beta = 0.0
    information_ratio = 0.0
    benchmark_return = None
    excess_return = None
    if bench_ret is not None and len(bench_ret) > 1:
        bench_ret = bench_ret.reindex(port_ret.index).fillna(0.0)
        benchmark_return = float((1 + bench_ret).prod() - 1)
        excess_return = float(total_return - benchmark_return)
        active_ret = port_ret - bench_ret
        cov = np.cov(port_ret, bench_ret)
        if cov.shape == (2, 2):
            var_bench = cov[1, 1]
            if var_bench > 1e-10:
                beta = cov[0, 1] / var_bench
                # Jensen's alpha: α = Rf·(1-β) + (Rp - β·Rb)
                # Annualized version
                port_ret_annual = port_ret.mean() * bars_per_year
                bench_ret_annual = bench_ret.mean() * bars_per_year
                alpha = risk_free_rate * (1 - beta) + (port_ret_annual - beta * bench_ret_annual)
        tracking_error = active_ret.std() * math.sqrt(bars_per_year)
        if tracking_error > 1e-10:
            information_ratio = active_ret.mean() * bars_per_year / tracking_error

    # Position concentration
    concentration = 0.0
    if target_pos is not None and not target_pos.empty:
        concentration = target_pos.abs().mean().mean()

    # Turnover
    avg_turnover = turnover_series.mean() if turnover_series is not None else 0.0

    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "volatility": volatility,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "downside_risk": downside_risk,
        "n_trades": n_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_pnl": avg_pnl,
        "avg_pnl_pct": avg_pnl_pct,
        "avg_holding_bars": avg_holding_bars,
        "max_consecutive_wins": max_consecutive_wins,
        "max_consecutive_losses": max_consecutive_losses,
        "alpha": alpha,
        "benchmark_return": benchmark_return,
        "excess_return": excess_return,
        "beta": beta,
        "information_ratio": information_ratio,
        "concentration": concentration,
        "avg_turnover": avg_turnover,
        "n_days": n_days,
        "initial_cash": initial_cash,
        "final_equity": equity_curve.iloc[-1],
    }


def by_symbol_stats(trades: List[TradeRecord]) -> Dict[str, Dict[str, Any]]:
    """Calculate per-symbol trade statistics."""
    if not trades:
        return {}

    by_sym: Dict[str, List[TradeRecord]] = {}
    for t in trades:
        by_sym.setdefault(t.symbol, []).append(t)

    stats = {}
    for sym, sym_trades in by_sym.items():
        n = len(sym_trades)
        winners = sum(1 for t in sym_trades if t.pnl > 0)
        total_pnl = sum(t.pnl for t in sym_trades)
        avg_pnl_pct = sum(t.pnl_pct for t in sym_trades) / n if n > 0 else 0.0
        stats[sym] = {
            "n_trades": n,
            "win_rate": winners / n if n > 0 else 0.0,
            "total_pnl": total_pnl,
            "avg_pnl_pct": avg_pnl_pct,
        }
    return stats


def by_exit_reason_stats(trades: List[TradeRecord]) -> Dict[str, Dict[str, Any]]:
    """Calculate statistics grouped by exit reason."""
    if not trades:
        return {}

    by_reason: Dict[str, List[TradeRecord]] = {}
    for t in trades:
        by_reason.setdefault(t.exit_reason, []).append(t)

    stats = {}
    for reason, reason_trades in by_reason.items():
        n = len(reason_trades)
        total_pnl = sum(t.pnl for t in reason_trades)
        stats[reason] = {
            "count": n,
            "total_pnl": total_pnl,
        }
    return stats


def calc_trade_turnover_series(
    trades: List[TradeRecord],
    equity_curve: pd.Series,
) -> pd.Series:
    """Calculate daily turnover series from trades."""
    if not trades:
        return pd.Series(0.0, index=equity_curve.index)

    turnover = pd.Series(0.0, index=equity_curve.index)
    for t in trades:
        entry_date = t.entry_time.normalize()
        exit_date = t.exit_time.normalize()
        trade_value = t.size * t.entry_price
        if entry_date in turnover.index:
            turnover.loc[entry_date] += trade_value
        if exit_date in turnover.index:
            turnover.loc[exit_date] += t.size * t.exit_price

    # Normalize by equity
    turnover = turnover / equity_curve.replace(0, 1)
    return turnover


def _max_consecutive(trades: List[TradeRecord], win: bool = True) -> int:
    """Find maximum consecutive wins or losses."""
    max_count = 0
    current = 0
    for t in trades:
        is_win = t.pnl > 0
        if is_win == win:
            current += 1
            max_count = max(max_count, current)
        else:
            current = 0
    return max_count
