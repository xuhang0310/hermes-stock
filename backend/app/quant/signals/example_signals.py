"""Example signal engines for common strategies.

These demonstrate how to implement SignalEngine for various trading strategies:
- MACD: Moving Average Convergence Divergence crossover
- MA Cross: Moving average crossover (golden cross / death cross)
- Bollinger Bands: Mean reversion around Bollinger Bands
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.app.quant.signal_engine import SignalEngine


class MacdSignalEngine(SignalEngine):
    """MACD crossover strategy.

    Buy when MACD line crosses above signal line (golden cross).
    Sell when MACD line crosses below signal line (death cross).
    """

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        """Initialize MACD parameters.

        Args:
            fast: Fast EMA period (default 12).
            slow: Slow EMA period (default 26).
            signal: Signal line EMA period (default 9).
        """
        self.fast = fast
        self.slow = slow
        self.signal = signal

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        signals = {}
        for code, df in data_map.items():
            if "close" not in df.columns or len(df) < self.slow + self.signal:
                signals[code] = pd.Series(0.0, index=df.index)
                continue

            close = df["close"]
            # Calculate MACD
            ema_fast = close.ewm(span=self.fast, adjust=False).mean()
            ema_slow = close.ewm(span=self.slow, adjust=False).mean()
            macd_line = ema_fast - ema_slow
            signal_line = macd_line.ewm(span=self.signal, adjust=False).mean()

            # Position: 1 when MACD > signal (bullish), 0 otherwise
            position = (macd_line > signal_line).astype(float)
            signals[code] = position

        return signals


class MaCrossSignalEngine(SignalEngine):
    """Moving average crossover strategy.

    Buy when short MA crosses above long MA (golden cross).
    Sell when short MA crosses below long MA (death cross).
    """

    def __init__(self, short_period: int = 5, long_period: int = 20):
        """Initialize MA periods.

        Args:
            short_period: Short moving average period (default 5).
            long_period: Long moving average period (default 20).
        """
        self.short_period = short_period
        self.long_period = long_period

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        signals = {}
        for code, df in data_map.items():
            if "close" not in df.columns or len(df) < self.long_period:
                signals[code] = pd.Series(0.0, index=df.index)
                continue

            close = df["close"]
            ma_short = close.rolling(window=self.short_period).mean()
            ma_long = close.rolling(window=self.long_period).mean()

            # Position: 1 when short MA > long MA, 0 otherwise
            position = (ma_short > ma_long).astype(float)
            signals[code] = position

        return signals


class BollingerBandSignalEngine(SignalEngine):
    """Bollinger Bands mean reversion strategy.

    Buy when price touches lower band (oversold).
    Sell when price touches upper band (overbought).
    """

    def __init__(self, period: int = 20, num_std: float = 2.0):
        """Initialize Bollinger Band parameters.

        Args:
            period: Moving average period (default 20).
            num_std: Number of standard deviations for bands (default 2.0).
        """
        self.period = period
        self.num_std = num_std

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        signals = {}
        for code, df in data_map.items():
            if "close" not in df.columns or len(df) < self.period:
                signals[code] = pd.Series(0.0, index=df.index)
                continue

            close = df["close"]
            ma = close.rolling(window=self.period).mean()
            std = close.rolling(window=self.period).std()

            upper_band = ma + self.num_std * std
            lower_band = ma - self.num_std * std

            # Simple strategy: buy when below lower band, sell when above upper band
            position = pd.Series(0.0, index=df.index)
            position[close < lower_band] = 1.0  # oversold: buy
            position[close > upper_band] = 0.0  # overbought: sell

            # Hold position between bands
            position = position.ffill().fillna(0.0)
            signals[code] = position

        return signals


class RsiSignalEngine(SignalEngine):
    """RSI (Relative Strength Index) overbought/oversold strategy.

    Buy when RSI < oversold_threshold (default 30).
    Sell when RSI > overbought_threshold (default 70).
    """

    def __init__(self, period: int = 14, oversold: float = 30.0, overbought: float = 70.0):
        """Initialize RSI parameters.

        Args:
            period: RSI calculation period (default 14).
            oversold: Oversold threshold (default 30).
            overbought: Overbought threshold (default 70).
        """
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        signals = {}
        for code, df in data_map.items():
            if "close" not in df.columns or len(df) < self.period + 1:
                signals[code] = pd.Series(0.0, index=df.index)
                continue

            close = df["close"]
            delta = close.diff()

            gain = delta.where(delta > 0, 0.0)
            loss = -delta.where(delta < 0, 0.0)

            avg_gain = gain.rolling(window=self.period).mean()
            avg_loss = loss.rolling(window=self.period).mean()

            rs = avg_gain / avg_loss.replace(0, np.nan)
            rsi = 100 - (100 / (1 + rs))

            # Position: 1 when RSI < oversold, 0 when RSI > overbought
            position = pd.Series(0.5, index=df.index)  # hold middle
            position[rsi < self.oversold] = 1.0
            position[rsi > self.overbought] = 0.0

            position = position.ffill().fillna(0.0)
            signals[code] = position

        return signals
