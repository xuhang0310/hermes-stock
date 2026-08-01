"""Data models for the backtest engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class Position:
    """An open position."""

    symbol: str
    direction: int  # 1=long, -1=short
    entry_price: float
    entry_time: pd.Timestamp
    size: float
    leverage: float = 1.0
    entry_bar_idx: int = 0
    entry_commission: float = 0.0


@dataclass(frozen=True)
class TradeRecord:
    """A completed round-trip trade."""

    symbol: str
    direction: int
    entry_price: float
    exit_price: float
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    size: float
    leverage: float = 1.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = "signal"
    holding_bars: int = 0
    commission: float = 0.0
    entry_margin: float = 0.0
    exit_margin: float = 0.0


@dataclass(frozen=True)
class EquitySnapshot:
    """Daily equity snapshot."""

    timestamp: pd.Timestamp
    capital: float
    unrealized: float
    equity: float
    positions: int = 0
