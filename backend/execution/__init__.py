"""Broker-neutral execution abstractions, the product paper adapter, and test fixtures."""

from .capabilities import PlatformCapabilities
from .interface import ExecutionProvider, TradingPlatformAdapter
from .local_paper import LocalPaperAdapter, NO_PRICE_REASON
from .models import (
    AccountSnapshot, AssetType, Balance, ExecutionResult, Fill, Instrument,
    MarketSession, Order, OrderRequest, OrderStatus, OrderType, UniversalPosition,
    UniversalQuote,
)
from .provider import MockExecutionProvider, MockTradingPlatformAdapter
from .registry import AdapterRegistry
from .router import ExecutionRouter

__all__ = [
    "AccountSnapshot", "AdapterRegistry", "AssetType", "Balance", "ExecutionProvider",
    "ExecutionResult", "ExecutionRouter", "Fill", "Instrument", "LocalPaperAdapter",
    "MarketSession", "MockExecutionProvider", "MockTradingPlatformAdapter",
    "NO_PRICE_REASON", "Order", "OrderRequest", "OrderStatus", "OrderType",
    "PlatformCapabilities", "TradingPlatformAdapter", "UniversalPosition", "UniversalQuote",
]
