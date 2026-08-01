"""Quantitative research services for Hermes Stock."""

from backend.app.quant.backtest_models import EquitySnapshot, Position, TradeRecord
from backend.app.quant.signal_engine import ConstantSignalEngine, SignalEngine
from backend.app.quant.engines.china_a import ChinaAEngine
from backend.app.quant.backtest_metrics import calc_metrics

__all__ = [
    "SignalEngine",
    "ConstantSignalEngine",
    "ChinaAEngine",
    "Position",
    "TradeRecord",
    "EquitySnapshot",
    "calc_metrics",
]

