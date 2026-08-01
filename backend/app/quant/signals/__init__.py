"""Example signal engines for common strategies."""

from backend.app.quant.signals.example_signals import (
    BollingerBandSignalEngine,
    MacdSignalEngine,
    MaCrossSignalEngine,
    RsiSignalEngine,
)
from backend.app.quant.signals.momentum_signal import MomentumSignalEngine
from backend.app.quant.signals.turtle_signal import TurtleSignalEngine

__all__ = [
    "MacdSignalEngine",
    "MaCrossSignalEngine",
    "BollingerBandSignalEngine",
    "RsiSignalEngine",
    "MomentumSignalEngine",
    "TurtleSignalEngine",
]
