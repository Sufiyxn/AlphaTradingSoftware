"""Product paper execution and account.

Paper mode simulates execution only - cash, fills, positions, and realized P&L.
It never simulates a market: no price, candle, or quote is produced here. Every
fill price is supplied by the caller from real market or signal data, and an
order without one is rejected rather than filled at an invented price.
"""
from .capabilities import PlatformCapabilities
from .models import AssetType, Order, OrderRequest, OrderStatus, OrderType
from .provider import MockTradingPlatformAdapter

NO_PRICE_REASON = (
    "paper execution does not supply market prices; "
    "submit a price obtained from a real market-data provider or TradingView signal"
)


class LocalPaperAdapter(MockTradingPlatformAdapter):
    """In-process paper account used by the product runtime."""

    identify_name = "local_paper"
    order_prefix = "PAPER"
    account_id = "paper-local"

    def __init__(self, *, cash: float = 100_000.0, **kwargs):
        kwargs.pop("capabilities", None)
        super().__init__(
            cash=cash,
            capabilities=PlatformCapabilities(
                asset_types=frozenset({AssetType.STOCK}),
                exchanges=frozenset({"US", "NYSE", "NASDAQ"}),
                order_types=frozenset(OrderType),
                short_selling=True, fractional_shares=True, extended_hours=True,
                order_replacement=True, order_cancellation=True,
                position_retrieval=True, account_retrieval=True, order_retrieval=True,
                market_data=False, paper_trading=True,
            ),
            **kwargs,
        )

    def get_quote(self, symbol: str):
        raise NotImplementedError(
            "paper execution does not provide market data; "
            "connect a market-data provider or TradingView signal source")

    def place_order(self, request: OrderRequest) -> Order:
        price = request.limit_price if request.limit_price is not None else request.stop_price
        if price is None:
            self._next_id += 1
            return Order(f"{self.order_prefix}-REJECTED-{self._next_id:06d}", request,
                         OrderStatus.REJECTED, 0, None, self.clock(),
                         (), rejection_reason=NO_PRICE_REASON)
        return super().place_order(request)
