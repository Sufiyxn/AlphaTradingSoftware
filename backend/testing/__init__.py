"""Deterministic fixtures for automated tests only.

Nothing in this package is imported by :class:`backend.core.runtime` or by any
other module in the production service graph. The fixtures exist so tests can
exercise pipelines without network access and without the product ever
generating a synthetic price, candle, quote, account, or connection state.
"""
from backend.execution import MockExecutionProvider, MockTradingPlatformAdapter
from backend.scanner import TemporaryTestAnalyzer

from .market_data import (
    FixtureHistoryProvider,
    MockMarketDataProvider,
    fixture_history,
)

__all__ = [
    "FixtureHistoryProvider",
    "MockExecutionProvider",
    "MockMarketDataProvider",
    "MockTradingPlatformAdapter",
    "TemporaryTestAnalyzer",
    "fixture_history",
]
