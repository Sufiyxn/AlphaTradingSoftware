"""Deterministic historical trade simulation; no live execution."""

from .config import BacktestConfig
from .engine import BacktestEngine
from .metrics import calculate_metrics
from .models import BacktestResult, PerformanceMetrics, SimulatedTrade

__all__ = ["BacktestConfig", "BacktestEngine", "BacktestResult", "PerformanceMetrics", "SimulatedTrade", "calculate_metrics"]
