"""Universal broker-neutral execution abstractions and local mock adapter."""

from .capabilities import PlatformCapabilities
from .interface import ExecutionProvider, TradingPlatformAdapter
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
    "ExecutionResult", "ExecutionRouter", "Fill", "Instrument", "MarketSession", "MockExecutionProvider",
    "MockTradingPlatformAdapter", "Order", "OrderRequest", "OrderStatus", "OrderType",
    "PlatformCapabilities", "TradingPlatformAdapter", "UniversalPosition", "UniversalQuote",
]
