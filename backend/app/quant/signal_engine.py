"""Signal engine abstract base class.

Signal engines generate position-weight signals from price data.
The output is a dict mapping symbol codes to pandas Series of signals,
where each value is in [-1, 1] representing the target position weight.

- 0 = flat (no position)
- 1 = fully long
- -1 = fully short (not allowed in A-shares)
- fractional values = partial position

This decouples strategy logic from execution, allowing users to define
arbitrary strategies without modifying the core backtest engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

import pandas as pd


class SignalEngine(ABC):
    """Abstract base for all signal engines.

    Subclasses implement ``generate()`` to produce position-weight signals
    from OHLCV data. The backtest engine then aligns, shifts (to avoid
    lookahead bias), and executes these signals bar-by-bar.

    Example::

        class MacdCrossSignalEngine(SignalEngine):
            def __init__(self, fast=12, slow=26, signal=9):
                self.fast = fast
                self.slow = slow
                self.signal = signal

            def generate(self, data_map):
                signals = {}
                for code, df in data_map.items():
                    macd = df['close'].ewm(span=self.fast).mean() - df['close'].ewm(span=self.slow).mean()
                    signal_line = macd.ewm(span=self.signal).mean()
                    hist = macd - signal_line
                    # 金叉=1，死叉=0
                    position = (hist > 0).astype(float)
                    signals[code] = position
                return signals
    """

    @abstractmethod
    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
        """Generate position-weight signals from OHLCV data.

        Args:
            data_map: Dict mapping symbol codes to OHLCV DataFrames.
                      Each DataFrame has columns: open, high, low, close, volume,
                      and optionally: pct_chg, pre_close, trade_date, etc.

        Returns:
            Dict mapping symbol codes to pandas Series of position weights.
            Each Series should have the same index as the input DataFrame.
            Values should be in [-1, 1], where:
              - 0 = flat (no position)
              - 1 = fully long (100% of capital)
              - -1 = fully short (not allowed in A-shares)
              - fractional = partial position

        Notes:
            - Signals are shifted by 1 bar by the execution engine (next-bar-open semantics).
            - This avoids lookahead bias: signal at bar T uses data up to bar T,
              but execution happens at bar T+1 open.
        """
        raise NotImplementedError

    def validate_output(self, signal_map: Dict[str, pd.Series]) -> Dict[str, pd.Series]:
        """Validate and clip signal output.

        Ensures all signals are in [-1, 1] and handles NaN values.

        Args:
            signal_map: Raw signal output from generate().

        Returns:
            Validated signal map with clipped values and filled NaN.
        """
        validated = {}
        for code, signal in signal_map.items():
            if not isinstance(signal, pd.Series):
                raise TypeError(f"{code} 的信号必须是 pd.Series，实际为 {type(signal)}")
            # Fill NaN with 0 (flat)
            signal = signal.fillna(0.0)
            # Clip to [-1, 1]
            signal = signal.clip(-1.0, 1.0)
            validated[code] = signal
        return validated


class ConstantSignalEngine(SignalEngine):
    """Always hold a constant position (useful for benchmarking).

    Example: ConstantSignalEngine(1.0) = buy-and-hold.
    """

    def __init__(self, position: float = 1.0):
        """Initialize with constant position weight.

        Args:
            position: Constant position weight in [-1, 1].
        """
        if not -1.0 <= position <= 1.0:
            raise ValueError(f"position must be in [-1, 1], got {position}")
        self.position = position

    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
        signals = {}
        for code, df in data_map.items():
            signals[code] = pd.Series(self.position, index=df.index)
        return signals
