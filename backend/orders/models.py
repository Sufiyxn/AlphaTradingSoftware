"""Canonical order-lifecycle models: intent, order, status, fill.

Three concepts stay separate on purpose and must never be collapsed:

``OrderIntent``
    What the platform asks a provider to do - symbol, side, quantity, order
    type, prices, mode, connection. It exists before anything is submitted.
``Order``
    The execution request *and* its lifecycle: an identity the platform owns
    (``client_order_id``), an identity the provider owns
    (``provider_order_id``, absent until the provider answers), a status, and
    the fills reported against it. An order is not a position.
``Fill``
    One executed quantity at one price, reported by the provider. Fills are
    the only thing that ever moves a position.

A ``Position`` (``backend.portfolio``) is what results. Submitting an order
never creates a position; only fills do.

Nothing in this module talks to a provider, a network, or a clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.execution.models import (
    ExecutionMode,
    Fill,
    OrderRequest,
    OrderStatus,
    OrderType,
)
from backend.strategies.models import Direction

__all__ = [
    "ExecutionMode",
    "Fill",
    "InvalidOrderTransition",
    "Order",
    "OrderIntent",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
    "TERMINAL_STATUSES",
    "VALID_TRANSITIONS",
    "can_transition",
    "is_terminal",
    "next_states",
    "order_from_dict",
]


class InvalidOrderTransition(Exception):
    """Raised when a lifecycle transition is not permitted for the status."""

    def __init__(self, source: OrderStatus, target: OrderStatus) -> None:
        self.source = source
        self.target = target
        super().__init__(
            f"an order cannot move from {source.value} to {target.value}")


#: Statuses an order never leaves. A filled order cannot be cancelled and a
#: cancelled order cannot become filled - the platform refuses both.
TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
    OrderStatus.FAILED,
})

VALID_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    # Created locally; the provider has never seen it.
    OrderStatus.NEW: frozenset({
        OrderStatus.SUBMITTED, OrderStatus.REJECTED,
        OrderStatus.CANCELLED, OrderStatus.FAILED}),
    # Handed to the provider; the answer has not been interpreted yet.
    OrderStatus.SUBMITTED: frozenset({
        OrderStatus.PENDING, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED,
        OrderStatus.EXPIRED, OrderStatus.FAILED, OrderStatus.UNKNOWN}),
    OrderStatus.PENDING: frozenset({
        OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED,
        OrderStatus.EXPIRED, OrderStatus.FAILED, OrderStatus.UNKNOWN}),
    OrderStatus.ACCEPTED: frozenset({
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED,
        OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.FAILED,
        OrderStatus.UNKNOWN}),
    OrderStatus.PARTIALLY_FILLED: frozenset({
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED,
        OrderStatus.EXPIRED, OrderStatus.FAILED, OrderStatus.UNKNOWN}),
    # Terminal states never move again...
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.FAILED: frozenset(),
    # ...but an unrecognised state may always be replaced by a known one.
    OrderStatus.UNKNOWN: frozenset({
        OrderStatus.SUBMITTED, OrderStatus.PENDING, OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED,
        OrderStatus.REJECTED, OrderStatus.EXPIRED, OrderStatus.FAILED,
        OrderStatus.UNKNOWN}),
}

# An unknown state can only be reached from a state we do not fully understand.
for _status in (OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.PENDING,
                OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED):
    VALID_TRANSITIONS[_status] = VALID_TRANSITIONS[_status] | {OrderStatus.UNKNOWN}
VALID_TRANSITIONS = {
    status: frozenset(targets) for status, targets in VALID_TRANSITIONS.items()}
del _status


def is_terminal(status: OrderStatus) -> bool:
    """True for statuses an order never leaves (filled, cancelled, ...)."""
    return status in TERMINAL_STATUSES


def next_states(status: OrderStatus) -> frozenset[OrderStatus]:
    """The statuses ``status`` may legally move to (including itself)."""
    return VALID_TRANSITIONS.get(status, frozenset()) | {status}


def can_transition(source: OrderStatus, target: OrderStatus) -> bool:
    """Whether ``source -> target`` is a legal lifecycle move.

    Every state may always move to itself, so re-applying the status the
    provider already reported is a no-op rather than an error.
    """
    if not isinstance(source, OrderStatus) or not isinstance(target, OrderStatus):
        return False
    if source is target:
        return True
    return target in VALID_TRANSITIONS.get(source, frozenset())


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """The platform's request to execute one order, before any provider sees it.

    ``client_order_id`` is minted by the platform and is never a symbol and
    never a timestamp: it is the stable internal identity that survives
    restarts, reconciliation, and any provider re-identification.
    """

    client_order_id: str
    symbol: str
    direction: Direction
    quantity: float
    order_type: OrderType = OrderType.LIMIT
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str = "DAY"
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    connection_id: str | None = None
    provider_id: str | None = None
    source: str = "internal"
    signal_id: str | None = None
    risk_code: str | None = None

    @property
    def reference_price(self) -> float | None:
        """The price carried by the order itself; ``None`` when it has none."""
        return self.limit_price if self.limit_price is not None else self.stop_price

    @property
    def is_buy(self) -> bool:
        return self.direction is Direction.LONG

    def to_order_request(self) -> OrderRequest:
        """The provider-facing request shape built from this intent."""
        return OrderRequest(
            symbol=self.symbol,
            direction=self.direction,
            quantity=self.quantity,
            limit_price=self.limit_price,
            order_type=self.order_type,
            stop_price=self.stop_price,
            time_in_force=self.time_in_force,
            execution_mode=self.execution_mode,
        )


@dataclass(frozen=True, slots=True)
class Order:
    """One order's lifecycle: identity, status, and the fills behind it.

    ``client_order_id`` is internal and permanent. ``provider_order_id`` is
    whatever the provider answered with and stays ``None`` until submission
    succeeds - it is never invented, never reused as the internal identity,
    and never derived from the symbol or a timestamp.

    ``filled_quantity``/``average_fill_price``/``remaining_quantity`` are all
    derived from the recorded fills, so an order can never claim to be filled
    more than the executions it actually carries.
    """

    intent: OrderIntent
    status: OrderStatus
    created_at: datetime
    provider_order_id: str | None = None
    fills: tuple[Fill, ...] = ()
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    updated_at: datetime | None = None
    submitted_at: datetime | None = None
    rejection_reason: str | None = None
    connection_id: str | None = None
    provider_id: str | None = None

    # -- identity ---------------------------------------------------------
    @property
    def client_order_id(self) -> str:
        return self.intent.client_order_id

    @property
    def order_id(self) -> str:
        """Alias used by the journal and by existing callers."""
        return self.intent.client_order_id

    @property
    def request(self) -> OrderRequest:
        """The provider-facing request; kept for journal compatibility."""
        return self.intent.to_order_request()

    @property
    def symbol(self) -> str:
        return self.intent.symbol

    @property
    def direction(self) -> Direction:
        return self.intent.direction

    @property
    def quantity(self) -> float:
        return self.intent.quantity

    # -- quantity ---------------------------------------------------------
    @property
    def remaining_quantity(self) -> float:
        return max(0.0, float(self.intent.quantity) - float(self.filled_quantity))

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)

    @property
    def is_open(self) -> bool:
        """True while the order still has capacity left to reserve or fill."""
        return not self.is_terminal and self.remaining_quantity > 0

    @property
    def has_fills(self) -> bool:
        return bool(self.fills)

    def describe(self) -> dict:
        """Secret-free, JSON-ready view used by the API and the audit trail."""
        return {
            "order_id": self.client_order_id,
            "client_order_id": self.client_order_id,
            "provider_order_id": self.provider_order_id,
            "symbol": self.intent.symbol,
            "direction": self.intent.direction.value,
            "quantity": self.intent.quantity,
            "order_type": self.intent.order_type.value,
            "limit_price": self.intent.limit_price,
            "time_in_force": self.intent.time_in_force,
            "execution_mode": self.intent.execution_mode.value,
            "connection_id": self.connection_id,
            "provider_id": self.provider_id,
            "source": self.intent.source,
            "signal_id": self.intent.signal_id,
            "status": self.status.value,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "average_fill_price": self.average_fill_price,
            "rejection_reason": self.rejection_reason,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "fills": [
                {
                    "fill_id": fill.fill_id,
                    "order_id": fill.order_id,
                    "provider_fill_id": fill.provider_fill_id,
                    "provider_order_id": fill.provider_order_id,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "fee": fill.fee,
                    "side": fill.side.value if fill.side else None,
                    "timestamp": fill.timestamp.isoformat(),
                }
                for fill in self.fills
            ],
        }


def order_from_dict(payload: dict) -> Order:
    """Rebuild a canonical order from the journal's serialized form.

    Anything unparseable raises, because silently dropping an outstanding
    order would hide it from reconciliation after a restart.
    """
    if not isinstance(payload, dict):
        raise ValueError("an order record must be a mapping")
    intent = payload.get("intent") or {}
    if not isinstance(intent, dict):
        raise ValueError("an order record must carry an intent")
    status = _status(payload.get("status"))
    created_at = _datetime(payload.get("created_at"))
    if created_at is None:
        raise ValueError("an order record must carry created_at")
    quantity = _number(intent.get("quantity")) or 0.0
    if quantity <= 0:
        raise ValueError("an order record must carry a positive quantity")
    direction = _direction(intent.get("direction")) or Direction.LONG
    rebuilt_intent = OrderIntent(
        client_order_id=str(intent.get("client_order_id")
                            or payload.get("order_id") or ""),
        symbol=str(intent.get("symbol") or "").strip().upper(),
        direction=direction,
        quantity=quantity,
        order_type=_order_type(intent.get("order_type")),
        limit_price=_number(intent.get("limit_price")),
        stop_price=_number(intent.get("stop_price")),
        time_in_force=str(intent.get("time_in_force") or "DAY"),
        execution_mode=_execution_mode(intent.get("execution_mode")),
        connection_id=intent.get("connection_id") or payload.get("connection_id"),
        provider_id=intent.get("provider_id") or payload.get("provider_id"),
        source=str(intent.get("source") or "internal"),
        signal_id=intent.get("signal_id"),
        risk_code=intent.get("risk_code"),
    )
    if not rebuilt_intent.client_order_id or not rebuilt_intent.symbol:
        raise ValueError("an order record must carry an order id and a symbol")
    fills = tuple(_fill_from_dict(item) for item in (payload.get("fills") or ()))
    filled = payload.get("filled_quantity")
    filled_quantity = float(filled) if isinstance(filled, (int, float)) else sum(
        fill.quantity for fill in fills)
    average = _number(payload.get("average_fill_price"))
    return Order(
        intent=rebuilt_intent,
        status=status,
        created_at=created_at,
        provider_order_id=payload.get("provider_order_id"),
        fills=fills,
        filled_quantity=filled_quantity,
        average_fill_price=average,
        updated_at=_datetime(payload.get("updated_at")),
        submitted_at=_datetime(payload.get("submitted_at")),
        rejection_reason=payload.get("rejection_reason"),
        connection_id=payload.get("connection_id"),
        provider_id=payload.get("provider_id"),
    )


def _fill_from_dict(payload: dict) -> Fill:
    if not isinstance(payload, dict):
        raise ValueError("a fill must be a mapping")
    timestamp = _datetime(payload.get("timestamp"))
    if timestamp is None:
        raise ValueError("a fill must carry a timestamp")
    quantity = _number(payload.get("quantity")) or 0.0
    price = _number(payload.get("price")) or 0.0
    if quantity <= 0 or price <= 0:
        raise ValueError("a fill must carry a positive quantity and price")
    return Fill(
        quantity=quantity,
        price=price,
        timestamp=timestamp,
        fee=_number(payload.get("fee")) or 0.0,
        order_id=payload.get("order_id"),
        fill_id=payload.get("fill_id"),
        provider_fill_id=payload.get("provider_fill_id"),
        provider_order_id=payload.get("provider_order_id"),
        side=_direction(payload.get("side")),
    )


def _status(value) -> OrderStatus:
    if isinstance(value, OrderStatus):
        return value
    if isinstance(value, str):
        for item in OrderStatus:
            if item.value == value:
                return item
    return OrderStatus.UNKNOWN


def _direction(value) -> Direction | None:
    if isinstance(value, Direction):
        return value
    if isinstance(value, str):
        for item in Direction:
            if item.value == value:
                return item
    return None


def _order_type(value) -> OrderType:
    if isinstance(value, OrderType):
        return value
    if isinstance(value, str):
        for item in OrderType:
            if item.value == value:
                return item
    return OrderType.LIMIT


def _execution_mode(value) -> ExecutionMode:
    if isinstance(value, ExecutionMode):
        return value
    if isinstance(value, str):
        for item in ExecutionMode:
            if item.value == value:
                return item
    return ExecutionMode.PAPER


def _datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
