"""Deterministic, pluggable strategy evaluation."""

from .baseline import BaselineTrendMomentumStrategy
from .indicators import atr, ema, rsi
from .models import Direction, StrategyConfig, StrategySignal, TradeSetup
from .service import StrategyEngine

__all__ = [
    "BaselineTrendMomentumStrategy",
    "Direction",
    "StrategyConfig",
    "StrategyEngine",
    "StrategySignal",
    "TradeSetup",
    "atr",
    "ema",
    "rsi",
]
