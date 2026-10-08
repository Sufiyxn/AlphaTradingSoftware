"""Broker-neutral execution abstractions, the product paper adapter, and test fixtures."""

from .capabilities import PlatformCapabilities
from .fill_identity import deterministic_fill_key, fill_identity, normalize_fill
from .gate import LIVE_BLOCKERS, ExecutionSafetyGate
from .identity import (
    ConnectionInstanceRegistry,
    ProviderInstanceIdentity,
    ProviderInstanceMixin,
    mint_instance_id,
)
from .interface import ExecutionProvider, TradingPlatformAdapter
from .local_paper import LocalPaperAdapter, NO_PRICE_REASON, paper_fill_id
from .models import (
    AccountSnapshot, AssetType, Balance, ExecutionMode, ExecutionResult, Fill, Instrument,
    MarketSession, Order, OrderRequest, OrderStatus, OrderType, UniversalPosition,
    UniversalQuote, normalize_order_status,
)
from .provider import MockExecutionProvider, MockTradingPlatformAdapter
from .registry import AdapterRegistry
from .router import ExecutionRouter

__all__ = [
    "AccountSnapshot", "AdapterRegistry", "AssetType", "Balance",
    "ConnectionInstanceRegistry", "ExecutionMode", "ExecutionResult", "ExecutionProvider",
    "ExecutionRouter", "ExecutionSafetyGate",
    "Fill", "Instrument", "LIVE_BLOCKERS", "LocalPaperAdapter", "MarketSession",
    "MockExecutionProvider", "MockTradingPlatformAdapter", "NO_PRICE_REASON", "Order",
    "OrderRequest", "OrderStatus", "OrderType", "PlatformCapabilities",
    "ProviderInstanceIdentity", "ProviderInstanceMixin",
    "TradingPlatformAdapter", "UniversalPosition", "UniversalQuote",
    "deterministic_fill_key", "fill_identity", "mint_instance_id",
    "normalize_fill", "normalize_order_status", "paper_fill_id",
]
