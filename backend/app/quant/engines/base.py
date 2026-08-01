"""Base backtest engine with shared bar-by-bar execution loop.

All market engines inherit from BaseEngine and override market-rule methods.
The shared run_backtest() handles: data loading → signal generation →
pre-compute target weights → bar-by-bar execution with market rule enforcement
→ metrics → artifacts.
"""

from __future__ import annotations

import json
import logging
import math
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backend.app.quant.backtest_metrics import (
    by_exit_reason_stats,
    by_symbol_stats,
    calc_metrics,
    calc_trade_turnover_series,
)
from backend.app.quant.backtest_models import EquitySnapshot, Position, TradeRecord
from backend.app.quant.signal_engine import SignalEngine

logger = logging.getLogger(__name__)


def _json_safe_scalar_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Scalar metrics for stdout JSON; non-finite floats become null."""
    return {
        k: (None if isinstance(v, float) and not math.isfinite(v) else v)
        for k, v in metrics.items()
        if not isinstance(v, dict)
    }


class BaseEngine(ABC):
    """Abstract base for all market engines.

    Subclasses override market-rule methods:
      - can_execute: whether a trade is allowed by market rules
      - round_size: lot-size rounding
      - calc_commission: fee structure
      - apply_slippage: slippage model
    """

    def __init__(self, config: dict):
        self.config = config
        self.initial_capital: float = config.get("initial_cash", 1_000_000)
        self.default_leverage: float = config.get("leverage", 1.0)
        self.capital: float = self.initial_capital
        self.positions: Dict[str, Position] = {}
        self.trades: List[TradeRecord] = []
        self.equity_snapshots: List[EquitySnapshot] = []
        self._bar_idx: int = 0

    # ── Market rule interface (subclass must implement) ──

    @abstractmethod
    def can_execute(self, symbol: str, direction: int, bar: pd.Series) -> bool:
        """Whether market rules allow this trade.

        Args:
            symbol: Instrument identifier.
            direction: 1 (long), -1 (short), 0 (close).
            bar: Current bar data (OHLCV + extras).

        Returns:
            True if allowed.
        """

    @abstractmethod
    def round_size(self, raw_size: float, price: float) -> float:
        """Round position size per market lot rules."""

    @abstractmethod
    def calc_commission(self, size: float, price: float, direction: int, is_open: bool) -> float:
        """Calculate commission for a trade."""

    @abstractmethod
    def apply_slippage(self, price: float, direction: int) -> float:
        """Apply slippage to execution price."""

    # ── PnL calculation ──

    def _calc_pnl(
        self, symbol: str, direction: int, size: float,
        entry_price: float, exit_price: float,
    ) -> float:
        """Realised PnL for a closed position."""
        return direction * size * (exit_price - entry_price)

    # ── Main entry ──

    def run_backtest(
        self,
        config: Dict[str, Any],
        data_map: Dict[str, pd.DataFrame],
        signal_engine: SignalEngine,
    ) -> Dict[str, Any]:
        """Full backtest pipeline.

        Args:
            config: Backtest configuration dict.
            data_map: Pre-loaded OHLCV data.
            signal_engine: SignalEngine instance.

        Returns:
            Metrics dictionary.
        """
        codes = list(data_map.keys())
        if not codes:
            raise ValueError("No data provided")

        # 1. Generate signals
        signal_map = signal_engine.generate(data_map)
        signal_map = signal_engine.validate_output(signal_map)

        if not isinstance(signal_map, dict):
            raise TypeError(f"SignalEngine.generate() must return dict, got {type(signal_map)}")

        valid_codes = sorted(c for c in signal_map if c in data_map)
        if not valid_codes:
            raise ValueError("No valid signals generated")

        # 2. Align signals (shift by 1 to avoid lookahead)
        dates, close_df, target_pos, ret_df = self._align_signals(
            data_map, signal_map, valid_codes
        )

        # 3. Bar-by-bar execution
        self._execute_bars(dates, data_map, close_df, target_pos, valid_codes)

        # 4. Build output series
        equity_series = pd.Series(
            [s.equity for s in self.equity_snapshots],
            index=[s.timestamp for s in self.equity_snapshots],
        )
        bench_ret = ret_df.mean(axis=1) if ret_df.shape[1] > 0 else pd.Series(0.0, index=dates)

        # 5. Metrics
        realized_turnover = calc_trade_turnover_series(self.trades, equity_series)
        m = calc_metrics(
            equity_series,
            self.trades,
            self.initial_capital,
            bars_per_year=252,
            bench_ret=bench_ret,
            target_pos=target_pos,
            turnover_series=realized_turnover,
        )
        m["by_symbol"] = by_symbol_stats(self.trades)
        m["by_exit_reason"] = by_exit_reason_stats(self.trades)

        return m

    # ── Signal alignment ──

    def _align_signals(
        self,
        data_map: Dict[str, pd.DataFrame],
        signal_map: Dict[str, pd.Series],
        codes: List[str],
    ) -> tuple:
        """Build aligned date index, close matrix, target-position matrix, return matrix.

        Signal is shifted by 1 bar (next-bar-open semantics) then normalised so
        ``sum(abs(weights)) <= 1.0``.
        """
        # Build unified sorted date index
        all_idx_arrays = [data_map[c].index.values for c in codes]
        merged = np.unique(np.concatenate(all_idx_arrays))
        dates = pd.DatetimeIndex(merged)

        n_dates = len(dates)
        n_codes = len(codes)

        # Build close matrix
        dates_i8 = dates.values.view("i8")
        close_arr = np.full((n_dates, n_codes), np.nan)
        for j, c in enumerate(codes):
            series = data_map[c]["close"]
            row_idx = np.searchsorted(dates_i8, series.index.values.view("i8"))
            close_arr[row_idx, j] = series.values

        # Forward-fill with limit
        _tmp = pd.DataFrame(close_arr)
        close_arr = _tmp.ffill(limit=5).values

        # Build position matrix with shift(1)
        pos_arr = np.full((n_dates, n_codes), np.nan)
        for j, c in enumerate(codes):
            sig_vals = signal_map[c].reindex(data_map[c].index).values.astype(np.float64, copy=True)
            np.nan_to_num(sig_vals, copy=False, nan=0.0)
            np.clip(sig_vals, -1.0, 1.0, out=sig_vals)
            # shift(1): prepend 0, drop last
            shifted_vals = np.empty_like(sig_vals)
            shifted_vals[0] = 0.0
            shifted_vals[1:] = sig_vals[:-1]
            row_idx = np.searchsorted(dates_i8, data_map[c].index.values.view("i8"))
            pos_arr[row_idx, j] = shifted_vals

        # Forward-fill positions
        _tmp = pd.DataFrame(pos_arr)
        pos_arr = np.nan_to_num(_tmp.ffill(limit=5).values, nan=0.0)

        close = pd.DataFrame(close_arr, index=dates, columns=codes)
        pos = pd.DataFrame(pos_arr, index=dates, columns=codes)
        ret = close.pct_change().fillna(0.0)

        # Normalize weights so sum(abs(weights)) <= 1
        scale = pos.abs().sum(axis=1).clip(lower=1.0)
        pos = pos.div(scale, axis=0)

        return dates, close, pos, ret

    # ── Execution loop ──

    def _execute_bars(
        self,
        dates: pd.DatetimeIndex,
        data_map: Dict[str, pd.DataFrame],
        close_df: pd.DataFrame,
        target_pos: pd.DataFrame,
        codes: List[str],
    ) -> None:
        """Bar-by-bar execution with market rule enforcement."""
        _target_arr = target_pos[codes].values
        _close_arr = close_df[codes].values
        _code_to_col = {c: j for j, c in enumerate(codes)}

        for i, ts in enumerate(dates):
            self._bar_idx = i

            # a. Value the book
            equity = self._calc_equity(close_df, ts, i, _close_arr, _code_to_col)
            target_weights: Dict[str, Optional[float]] = {}
            for c in codes:
                try:
                    val = _target_arr[i, _code_to_col[c]]
                    target_weights[c] = float(val) if not np.isnan(val) else 0.0
                except Exception as exc:
                    target_weights[c] = None
                    logger.warning("Target weight failed for %s at %s: %s", c, ts, exc)

            # b. Close positions that need to exit
            for c in codes:
                target_w = target_weights[c]
                current_pos = self.positions.get(c)
                if target_w is None or current_pos is None:
                    continue
                target_dir = 1 if target_w > 1e-9 else 0
                if target_dir == 0 or target_dir != current_pos.direction:
                    try:
                        self._rebalance(c, 0.0, data_map.get(c), ts, equity)
                    except Exception as exc:
                        logger.warning("Rebalance close failed for %s at %s: %s", c, ts, exc)

            # c. Open new positions
            for c in sorted(codes):
                target_w = target_weights[c]
                if target_w is None:
                    continue
                target_dir = 1 if target_w > 1e-9 else 0
                current_pos = self.positions.get(c)
                if current_pos is not None:
                    continue  # already handled above
                if target_dir != 0:
                    try:
                        self._rebalance(c, target_w, data_map.get(c), ts, equity)
                    except Exception as exc:
                        logger.warning("Rebalance open failed for %s at %s: %s", c, ts, exc)

            # d. Record equity snapshot
            snap_equity = self._calc_equity(close_df, ts, i, _close_arr, _code_to_col)
            self.equity_snapshots.append(EquitySnapshot(
                timestamp=ts,
                capital=self.capital,
                unrealized=snap_equity - self.capital,
                equity=snap_equity,
                positions=len(self.positions),
            ))

        # e. Force close all remaining positions
        if len(dates) > 0:
            last_ts = dates[-1]
            _last_row = len(dates) - 1
            for c in list(self.positions.keys()):
                pos = self.positions[c]
                mark_price = self._safe_price(
                    close_df, ts, c, pos.entry_price,
                    _arr=_close_arr, _row=_last_row, _col=_code_to_col.get(c),
                )
                exit_price = self.apply_slippage(mark_price, -pos.direction)
                self._close_position(c, exit_price, last_ts, "end_of_backtest")

            if self.equity_snapshots:
                self.equity_snapshots[-1] = EquitySnapshot(
                    timestamp=last_ts,
                    capital=self.capital,
                    unrealized=0.0,
                    equity=self.capital,
                    positions=0,
                )

    def _calc_equity(
        self, close_df: pd.DataFrame, ts: pd.Timestamp, row_idx: int,
        _close_arr: np.ndarray, _code_to_col: Dict[str, int],
    ) -> float:
        """Total equity = free cash + sum(unrealised PnL) per position."""
        if not self.positions:
            return self.capital

        equity = self.capital
        for sym, pos in self.positions.items():
            cp = self._safe_price(
                close_df, ts, sym, pos.entry_price,
                _arr=_close_arr, _row=row_idx, _col=_code_to_col.get(sym),
            )
            unrealized = self._calc_pnl(sym, pos.direction, pos.size, pos.entry_price, cp)
            equity += unrealized
        return equity

    def _rebalance(
        self,
        symbol: str,
        target_weight: float,
        df: Optional[pd.DataFrame],
        ts: pd.Timestamp,
        equity: float,
    ) -> None:
        """Adjust position for *symbol* toward *target_weight*."""
        target_dir = 1 if target_weight > 1e-9 else 0
        current_pos = self.positions.get(symbol)

        if current_pos is None and target_dir == 0:
            return
        if df is None or ts not in df.index:
            return

        bar = df.loc[ts]

        # Close if target is flat or direction changed
        if current_pos is not None:
            need_close = target_dir == 0 or target_dir != current_pos.direction
            if need_close:
                if self.can_execute(symbol, 0, bar):
                    open_price = float(bar.get("open", bar.get("close", 0)))
                    price = self.apply_slippage(open_price, -current_pos.direction)
                    self._close_position(symbol, price, ts, "signal")
                else:
                    return  # blocked

        if target_dir != 0 and symbol not in self.positions:
            # Open new position
            if not self.can_execute(symbol, 1, bar):
                return
            open_price = float(bar.get("open", bar.get("close", 0)))
            if open_price <= 0:
                return
            price = self.apply_slippage(open_price, 1)
            target_notional = target_weight * equity
            size = self.round_size(target_notional / price, price)
            if size <= 0:
                return
            commission = self.calc_commission(size, price, 1, is_open=True)
            cost = size * price + commission
            if cost > self.capital:
                return  # insufficient funds

            self.capital -= cost
            self.positions[symbol] = Position(
                symbol=symbol,
                direction=1,
                entry_price=price,
                entry_time=ts,
                size=size,
                leverage=1.0,
                entry_bar_idx=self._bar_idx,
                entry_commission=commission,
            )

    def _close_position(
        self,
        symbol: str,
        exit_price: float,
        exit_time: pd.Timestamp,
        reason: str,
    ) -> None:
        """Close position, record trade, return capital."""
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return

        pnl = self._calc_pnl(symbol, pos.direction, pos.size, pos.entry_price, exit_price)
        exit_comm = self.calc_commission(pos.size, exit_price, pos.direction, is_open=False)
        pnl_pct = pnl / (pos.size * pos.entry_price) * 100 if pos.size * pos.entry_price > 1e-9 else 0.0

        self.capital += pos.size * exit_price - exit_comm

        holding_bars = max(self._bar_idx - pos.entry_bar_idx, 0)

        self.trades.append(TradeRecord(
            symbol=symbol,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            size=pos.size,
            leverage=pos.leverage,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            holding_bars=holding_bars,
            commission=pos.entry_commission + exit_comm,
            entry_margin=pos.size * pos.entry_price,
            exit_margin=pos.size * exit_price,
        ))

    @staticmethod
    def _safe_price(
        close_df: pd.DataFrame,
        ts: pd.Timestamp,
        symbol: str,
        fallback: float,
        *,
        _arr: Optional[np.ndarray] = None,
        _row: Optional[int] = None,
        _col: Optional[int] = None,
    ) -> float:
        """Get close price with fallback."""
        if _arr is not None and _row is not None and _col is not None:
            val = _arr[_row, _col]
            return float(val) if not np.isnan(val) else fallback
        if ts in close_df.index and symbol in close_df.columns:
            val = close_df.at[ts, symbol]
            if pd.notna(val):
                return float(val)
        return fallback
