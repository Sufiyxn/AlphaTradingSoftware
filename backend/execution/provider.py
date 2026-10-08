from datetime import datetime, timezone
from typing import Sequence

from backend.market_data.models import Quote
from backend.portfolio.models import Position

from .capabilities import PlatformCapabilities
from .identity import ProviderInstanceMixin
from .interface import ExecutionProvider, TradingPlatformAdapter
from .models import AccountSnapshot, AssetType, Balance, Fill, Order, OrderRequest, OrderStatus, OrderType


class MockTradingPlatformAdapter(ProviderInstanceMixin):
    """Deterministic test fixture with no network, credentials, or venue access.

    Automated tests only. The product wires :class:`LocalPaperAdapter` instead;
    nothing under ``ApplicationRuntime`` ever constructs this class.

    Order ids are ``MOCK-<instance>-<sequence>``: the instance id names this
    adapter instance and the sequence numbers its orders, so two adapters -
    or two runs of one adapter - never hand out the same identity.
    """

    identify_name = "mock"
    order_prefix = "MOCK"
    account_id = "mock-account"

    def __init__(self, *, cash: float = 100_000.0, partial_fill_quantity: int | None = None,
                 fail_orders: bool = False, clock=lambda: datetime(2000, 1, 1, tzinfo=timezone.utc),
                 capabilities: PlatformCapabilities | None = None, positions: Sequence[Position] = ()):
        self.cash = cash
        self.partial_fill_quantity = partial_fill_quantity
        self.fail_orders = fail_orders
        self.clock = clock
        self._capabilities = capabilities or PlatformCapabilities(
            asset_types=frozenset({AssetType.STOCK}), exchanges=frozenset({"US", "NYSE", "NASDAQ"}),
            order_types=frozenset(OrderType), short_selling=True, fractional_shares=True,
            extended_hours=True, order_replacement=True, market_data=True, paper_trading=True)
        self._orders: dict[str, Order] = {}
        self._positions = {position.symbol: position for position in positions}
        self._init_provider_identity()
        self._connected = True

    def identify(self) -> str:
        return self.identify_name

    def capabilities(self) -> PlatformCapabilities:
        return self._capabilities

    def is_authenticated(self) -> bool:
        return False

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(self.cash, self.cash, "USD", self.account_id,
                               (Balance("USD", self.cash, self.cash, self.cash),))

    def place_order(self, request: OrderRequest) -> Order:
        order_id = self._mint_order_id()
        filled = 0 if self.fail_orders else min(request.quantity, self.partial_fill_quantity or request.quantity)
        status = OrderStatus.REJECTED if self.fail_orders else (
            OrderStatus.FILLED if filled == request.quantity else OrderStatus.PARTIALLY_FILLED
        )
        timestamp = self.clock()
        fill_price = request.limit_price if request.limit_price is not None else request.stop_price
        if fill_price is None and filled:
            fill_price = self.get_quote(request.symbol).price
        fills = (Fill(filled, fill_price, timestamp),) if filled and fill_price is not None else ()
        order = Order(order_id, request, status, filled, fill_price if filled else None, timestamp, fills)
        self._orders[order_id] = order
        return order

    def cancel_order(self, order_id: str) -> Order:
        order = self._orders[order_id]
        cancelled = Order(order.order_id, order.request, OrderStatus.CANCELLED,
                          order.filled_quantity, order.average_fill_price, order.created_at,
                          order.fills, order.rejection_reason)
        self._orders[order_id] = cancelled
        return cancelled

    def replace_order(self, order_id: str, request: OrderRequest) -> Order:
        existing = self._orders[order_id]
        replacement = Order(existing.order_id, request, OrderStatus.PENDING, 0, None, existing.created_at)
        self._orders[order_id] = replacement
        if self.fail_orders:
            replacement = Order(existing.order_id, request, OrderStatus.REJECTED, 0, None,
                               existing.created_at, rejection_reason="mock configured to reject orders")
        else:
            qty = min(request.quantity, self.partial_fill_quantity or request.quantity)
            price = request.limit_price if request.limit_price is not None else request.stop_price
            if price is None and qty:
                price = self.get_quote(request.symbol).price
            status = OrderStatus.FILLED if qty == request.quantity else OrderStatus.PARTIALLY_FILLED
            fills = (Fill(qty, price, existing.created_at),) if qty and price is not None else ()
            replacement = Order(existing.order_id, request, status, qty, price, existing.created_at, fills)
        self._orders[order_id] = replacement
        return replacement

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol.strip().upper(), self.clock(), 100.0, 0.0)

    def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def get_orders(self) -> Sequence[Order]:
        return tuple(self._orders.values())

    def get_positions(self) -> Sequence[Position]:
        return tuple(self._positions.values())

    def health_check(self) -> dict:
        """Local connectivity evidence only - this adapter owns no network."""
        if not self._connected:
            return {"success": False, "status": "DISCONNECTED",
                    "detail": "the local account adapter is disconnected"}
        return {"success": True, "status": "PASSED",
                "detail": "local in-process account; no network request was made"}

    def reconcile_orders(self, since: datetime | None = None) -> Sequence[Order]:
        """Reconciliation hook: the orders this adapter knows about since a cut-off."""
        orders = self.get_orders()
        if since is None:
            return orders
        return tuple(order for order in orders if order.created_at >= since)


class MockExecutionProvider(MockTradingPlatformAdapter):
    """Legacy name retained for existing paper-trading callers."""
