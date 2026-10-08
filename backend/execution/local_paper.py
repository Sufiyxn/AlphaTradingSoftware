"""Product paper execution and account.

Paper mode simulates execution only - cash, fills, positions, and realized P&L.
It never simulates a market: no price, candle, or quote is produced here. Every
fill price is supplied by the caller from real market or signal data, and an
order without one is rejected rather than filled at an invented price.
"""
from dataclasses import replace

from .capabilities import PlatformCapabilities
from .models import AssetType, Order, OrderRequest, OrderStatus, OrderType
from .provider import MockTradingPlatformAdapter

NO_PRICE_REASON = (
    "paper execution does not supply market prices; "
    "submit a price obtained from a real market-data provider or TradingView signal"
)


def paper_fill_id(provider_order_id: str, index: int) -> str:
    """The paper venue's fill number: ``<provider_order_id>-F<index>``.

    The paper venue identifies its executions the way a real venue does -
    derived from the order it filled and the position of the fill within
    that order - so a paper fill has a provider number to adopt, restart
    with, and compare, instead of leaving identity to a fallback.
    """
    return f"{provider_order_id}-F{index}"


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
            return Order(self._mint_rejected_id(), request,
                         OrderStatus.REJECTED, 0, None, self.clock(),
                         (), rejection_reason=NO_PRICE_REASON)
        order = self._identify_fills(super().place_order(request))
        # Store the stamped order, so what the venue remembers - and what
        # reconciliation reads back - is exactly what it returned.
        self._orders[order.order_id] = order
        return order

    def _identify_fills(self, order: Order) -> Order:
        """Stamp each execution with the paper venue's own fill number."""
        if not order.fills:
            return order
        fills = tuple(
            replace(fill,
                    provider_fill_id=(fill.provider_fill_id
                                      or paper_fill_id(order.order_id, index)))
            for index, fill in enumerate(order.fills, start=1))
        return replace(order, fills=fills)

    def _mint_rejected_id(self) -> str:
        """A rejected order still gets its own provider identity."""
        return (f"{self.order_prefix}-REJECTED-{self.provider_instance_id}"
                f"-{self._next_sequence():06d}")
