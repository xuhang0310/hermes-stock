"""Signal-based backtest runner.

This module provides the main entry point for running custom signal engine
backtests. It loads data, instantiates the engine, runs the backtest, and
returns formatted results.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import pandas as pd

from backend.app.core.settings import settings
from backend.app.data.repository import StockRepository
from backend.app.quant.engines.china_a import ChinaAEngine
from backend.app.quant.signal_engine import SignalEngine
from backend.app.quant.signals.example_signals import (
    BollingerBandSignalEngine,
    MacdSignalEngine,
    MaCrossSignalEngine,
    RsiSignalEngine,
)

logger = logging.getLogger(__name__)


# Registry of available signal engines
SIGNAL_ENGINE_REGISTRY: Dict[str, type] = {
    "macd": MacdSignalEngine,
    "ma_cross": MaCrossSignalEngine,
    "bollinger": BollingerBandSignalEngine,
    "rsi": RsiSignalEngine,
}


def list_signal_engines() -> Dict[str, str]:
    """List available signal engines with descriptions."""
    return {
        "macd": "MACD 金叉死叉策略（快线 12，慢线 26，信号线 9）",
        "ma_cross": "均线交叉策略（短期 5 日，长期 20 日）",
        "bollinger": "布林带均值回归策略（周期 20，标准差倍数 2.0）",
        "rsi": "RSI 超买超卖策略（周期 14，超卖 30，超买 70）",
    }


def run_signal_backtest(
    strategy: str,
    symbols: list[str],
    start_date: str,
    end_date: str,
    strategy_params: Optional[Dict[str, Any]] = None,
    initial_cash: float = 1_000_000,
    commission_rate: float = 0.00025,
    slippage: float = 0.001,
) -> Dict[str, Any]:
    """Run a signal-based backtest.

    Args:
        strategy: Strategy name (one of: macd, ma_cross, bollinger, rsi).
        symbols: List of stock codes (e.g., ["000001.SZ", "600519.SH"]).
        start_date: Start date in YYYYMMDD format.
        end_date: End date in YYYYMMDD format.
        strategy_params: Optional dict of strategy-specific parameters.
        initial_cash: Initial capital (default 1,000,000).
        commission_rate: Commission rate (default 0.00025).
        slippage: Slippage rate (default 0.001).

    Returns:
        Dict with backtest results including metrics, trades, and equity curve.
    """
    if strategy not in SIGNAL_ENGINE_REGISTRY:
        raise ValueError(f"Unknown strategy: {strategy}. Available: {list(SIGNAL_ENGINE_REGISTRY.keys())}")

    # Instantiate signal engine
    engine_class = SIGNAL_ENGINE_REGISTRY[strategy]
    params = strategy_params or {}
    signal_engine = engine_class(**params)

    # Load data
    data_map = _load_stock_data(symbols, start_date, end_date)
    if not data_map:
        raise ValueError("No data loaded. Check symbols and date range.")

    # Configure market engine
    config = {
        "initial_cash": initial_cash,
        "commission_rate": commission_rate,
        "slippage": slippage,
    }
    market_engine = ChinaAEngine(config)

    # Run backtest
    metrics = market_engine.run_backtest(config, data_map, signal_engine)

    # Format results
    result = {
        "strategy": strategy,
        "symbols": symbols,
        "period": {"start": start_date, "end": end_date},
        "metrics": metrics,
        "trades": [
            {
                "symbol": t.symbol,
                "entry_date": str(t.entry_time.date()) if hasattr(t.entry_time, "date") else str(t.entry_time),
                "exit_date": str(t.exit_time.date()) if hasattr(t.exit_time, "date") else str(t.exit_time),
                "entry_price": round(t.entry_price, 2),
                "exit_price": round(t.exit_price, 2),
                "size": int(t.size),
                "pnl": round(t.pnl, 2),
                "pnl_pct": round(t.pnl_pct, 2),
                "holding_bars": t.holding_bars,
            }
            for t in market_engine.trades
        ],
        "equity_curve": [
            {
                "date": str(s.timestamp.date()) if hasattr(s.timestamp, "date") else str(s.timestamp),
                "equity": round(s.equity, 2),
                "positions": s.positions,
            }
            for s in market_engine.equity_snapshots
        ],
    }

    return result


def _load_stock_data(
    symbols: list[str],
    start_date: str,
    end_date: str,
) -> Dict[str, pd.DataFrame]:
    """Load stock data from TuShare.

    Args:
        symbols: List of stock codes.
        start_date: Start date in YYYYMMDD format.
        end_date: End date in YYYYMMDD format.

    Returns:
        Dict mapping symbol codes to OHLCV DataFrames.
    """
    try:
        pro = _get_tushare_api()
        data_map = {}

        for symbol in symbols:
            # Convert symbol format: 000001.SZ -> 000001.SZ (tushare format)
            ts_code = symbol

            # Fetch daily data
            df = pro.daily(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )

            if df is None or df.empty:
                logger.warning("No data for %s", symbol)
                continue

            # Rename columns to standard OHLCV
            df = df.rename(columns={
                "trade_date": "date",
                "vol": "volume",
            })

            # Convert date to datetime and set as index
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()

            # Keep only needed columns
            df = df[["open", "high", "low", "close", "volume", "pct_chg", "pre_close"]]

            data_map[symbol] = df

        return data_map

    except Exception as e:
        logger.error("Failed to load stock data: %s", e)
        raise


def _get_tushare_api():
    """Get TuShare API instance."""
    try:
        import tushare as ts
        token = getattr(settings, "tushare_token", None)
        if not token:
            raise ValueError("TuShare token not configured")
        ts.set_token(token)
        return ts.pro_api()
    except ImportError:
        raise ImportError("tushare not installed. Install with: pip install tushare")


def run_custom_signal_backtest(
    signal_engine: SignalEngine,
    symbols: list[str],
    start_date: str,
    end_date: str,
    initial_cash: float = 1_000_000,
    commission_rate: float = 0.00025,
    slippage: float = 0.001,
) -> Dict[str, Any]:
    """Run a backtest with a custom signal engine.

    This is the low-level API for running backtests with user-defined strategies.

    Args:
        signal_engine: Custom SignalEngine instance.
        symbols: List of stock codes.
        start_date: Start date in YYYYMMDD format.
        end_date: End date in YYYYMMDD format.
        initial_cash: Initial capital.
        commission_rate: Commission rate.
        slippage: Slippage rate.

    Returns:
        Dict with backtest results.
    """
    # Load data
    data_map = _load_stock_data(symbols, start_date, end_date)
    if not data_map:
        raise ValueError("No data loaded")

    # Configure market engine
    config = {
        "initial_cash": initial_cash,
        "commission_rate": commission_rate,
        "slippage": slippage,
    }
    market_engine = ChinaAEngine(config)

    # Run backtest
    metrics = market_engine.run_backtest(config, data_map, signal_engine)

    return {
        "metrics": metrics,
        "trades": len(market_engine.trades),
        "final_equity": market_engine.equity_snapshots[-1].equity if market_engine.equity_snapshots else initial_cash,
    }
