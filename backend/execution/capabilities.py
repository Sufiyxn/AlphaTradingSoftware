from dataclasses import dataclass, field

from .models import AssetType, OrderType


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    asset_types: frozenset[AssetType] = field(default_factory=lambda: frozenset({AssetType.STOCK}))
    exchanges: frozenset[str] = field(default_factory=lambda: frozenset({"US"}))
    order_types: frozenset[OrderType] = field(default_factory=lambda: frozenset({OrderType.MARKET, OrderType.LIMIT}))
    short_selling: bool = False
    fractional_shares: bool = False
    extended_hours: bool = False
    order_cancellation: bool = True
    order_replacement: bool = False
    position_retrieval: bool = True
    account_retrieval: bool = True
    order_retrieval: bool = True
    market_data: bool = False
    paper_trading: bool = True

    @property
    def market_orders(self) -> bool:
        return OrderType.MARKET in self.order_types

    @property
    def limit_orders(self) -> bool:
        return OrderType.LIMIT in self.order_types

    @property
    def stop_orders(self) -> bool:
        return OrderType.STOP in self.order_types

    @property
    def stop_limit_orders(self) -> bool:
        return OrderType.STOP_LIMIT in self.order_types
