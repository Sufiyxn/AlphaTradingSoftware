"""Broker-neutral execution abstractions, the product paper adapter, and test fixtures."""

from .capabilities import PlatformCapabilities
from .gate import LIVE_BLOCKERS, ExecutionSafetyGate
from .interface import ExecutionProvider, TradingPlatformAdapter
from .local_paper import LocalPaperAdapter, NO_PRICE_REASON
from .models import (
    AccountSnapshot, AssetType, Balance, ExecutionMode, ExecutionResult, Fill, Instrument,
    MarketSession, Order, OrderRequest, OrderStatus, OrderType, UniversalPosition,
    UniversalQuote, normalize_order_status,
)
from .provider import MockExecutionProvider, MockTradingPlatformAdapter
from .registry import AdapterRegistry
from .router import ExecutionRouter

__all__ = [
    "AccountSnapshot", "AdapterRegistry", "AssetType", "Balance", "ExecutionMode",
    "ExecutionProvider", "ExecutionResult", "ExecutionRouter", "ExecutionSafetyGate",
    "Fill", "Instrument", "LIVE_BLOCKERS", "LocalPaperAdapter", "MarketSession",
    "MockExecutionProvider", "MockTradingPlatformAdapter", "NO_PRICE_REASON", "Order",
    "OrderRequest", "OrderStatus", "OrderType", "PlatformCapabilities",
    "TradingPlatformAdapter", "UniversalPosition", "UniversalQuote",
    "normalize_order_status",
]
