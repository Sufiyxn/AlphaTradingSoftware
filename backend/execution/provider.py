from datetime import datetime, timezone
from typing import Protocol, Sequence

from backend.portfolio.models import Position

from .models import AccountSnapshot, Order, OrderRequest, OrderStatus


class ExecutionProvider(Protocol):
    def place_order(self, request: OrderRequest) -> Order: ...
    def cancel_order(self, order_id: str) -> Order: ...
    def get_order(self, order_id: str) -> Order | None: ...
    def get_orders(self) -> Sequence[Order]: ...
    def get_positions(self) -> Sequence[Position]: ...
    def get_account(self) -> AccountSnapshot: ...


class MockExecutionProvider:
    """Deterministic in-memory execution adapter; never contacts an external venue."""

    def __init__(self, *, cash: float = 100_000.0, partial_fill_quantity: int | None = None,
                 fail_orders: bool = False, clock=lambda: datetime(2000, 1, 1, tzinfo=timezone.utc)):
        self.cash = cash
        self.partial_fill_quantity = partial_fill_quantity
        self.fail_orders = fail_orders
        self.clock = clock
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._next_id = 1

    def place_order(self, request: OrderRequest) -> Order:
        order_id = f"MOCK-{self._next_id:06d}"
        self._next_id += 1
        filled = 0 if self.fail_orders else min(request.quantity, self.partial_fill_quantity or request.quantity)
        status = OrderStatus.REJECTED if self.fail_orders else (
            OrderStatus.FILLED if filled == request.quantity else OrderStatus.PARTIALLY_FILLED
        )
        order = Order(order_id, request, status, filled, request.limit_price if filled else None, self.clock())
        self._orders[order_id] = order
        return order

    def cancel_order(self, order_id: str) -> Order:
        order = self._orders[order_id]
        cancelled = Order(order.order_id, order.request, OrderStatus.CANCELLED, order.filled_quantity, order.average_fill_price, order.created_at)
        self._orders[order_id] = cancelled
        return cancelled

    def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def get_orders(self) -> Sequence[Order]:
        return tuple(self._orders.values())

    def get_positions(self) -> Sequence[Position]:
        return tuple(self._positions.values())

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(self.cash, self.cash, "USD")
