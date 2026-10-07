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
    live_trading: bool = False

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

    def names(self) -> tuple[str, ...]:
        """Normalized capability names this adapter actually supports.

        Only supported capabilities appear - an unsupported capability is
        simply absent, never emulated or claimed.
        """
        names: list[str] = []
        assets = {AssetType.STOCK, AssetType.ETF}
        if assets & set(self.asset_types):
            names.append("equities")
        if self.fractional_shares:
            names.append("fractional_shares")
        for supported, name in (
            (self.market_orders, "market_orders"),
            (self.limit_orders, "limit_orders"),
            (self.stop_orders, "stop_orders"),
            (self.stop_limit_orders, "stop_limit_orders"),
            (self.short_selling, "short_selling"),
            (self.extended_hours, "extended_hours"),
            (self.order_cancellation, "order_cancellation"),
            (self.order_replacement, "order_modification"),
            (self.account_retrieval, "real_time_account_info"),
            (self.position_retrieval, "positions"),
            (self.order_retrieval, "order_status"),
            (self.paper_trading, "paper_trading"),
            (self.live_trading, "live_trading"),
        ):
            if supported:
                names.append(name)
        if self.market_data:
            names.append("market_data")
        return tuple(names)
