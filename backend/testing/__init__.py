"""Deterministic fixtures for automated tests only.

Nothing in this package is imported by :class:`backend.core.runtime` or by any
other module in the production service graph. The fixtures exist so tests can
exercise pipelines without network access and without the product ever
generating a synthetic price, candle, quote, account, or connection state.
"""
from backend.execution import MockExecutionProvider, MockTradingPlatformAdapter
from backend.scanner import TemporaryTestAnalyzer

from .execution import (
    ScriptedAdapter,
    account_failure,
    auth_failure,
    connection_failure,
    full_paper_capabilities,
    rate_limit_failure,
    timeout_failure,
)
from .http import FakeResponse, FakeTransport, connection_error, timeout_error
from .market_data import (
    FixtureHistoryProvider,
    MockMarketDataProvider,
    fixture_history,
)

__all__ = [
    "FakeResponse",
    "FakeTransport",
    "FixtureHistoryProvider",
    "MockExecutionProvider",
    "MockMarketDataProvider",
    "MockTradingPlatformAdapter",
    "ScriptedAdapter",
    "TemporaryTestAnalyzer",
    "account_failure",
    "auth_failure",
    "connection_error",
    "connection_failure",
    "fixture_history",
    "full_paper_capabilities",
    "rate_limit_failure",
    "timeout_error",
    "timeout_failure",
]
