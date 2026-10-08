"""Provider-agnostic view of what an execution provider currently believes.

Reconciliation can only be as honest as the snapshot it is given, so the
snapshot carries its own trustworthiness:

``complete``
    ``True`` only when the provider answered the whole request. A partial
    page, a timeout, or a filtered subset is ``False`` - and an incomplete
    snapshot is reported as unresolved, never as "everything matches".
``error``
    The provider failed outright. No local state may be derived from it.

Nothing here reaches the network. A connection adapts itself to
:class:`ProviderStateClient` and produces a :class:`ProviderStateSnapshot`;
the reconciler decides what the two sides mean together.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from backend.execution.models import OrderStatus
from backend.strategies.models import Direction

__all__ = [
    "ProviderStateClient",
    "ProviderStateSnapshot",
    "RemoteFill",
    "RemoteOrder",
    "RemotePosition",
    "snapshot_from_adapter",
]


@dataclass(frozen=True, slots=True)
class RemoteOrder:
    """One order as the provider reports it."""

    provider_order_id: str
    symbol: str
    quantity: float
    filled_quantity: float
    status: OrderStatus
    client_order_id: str | None = None
    side: Direction | None = None
    limit_price: float | None = None
    updated_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        }


@dataclass(frozen=True, slots=True)
class RemoteFill:
    """One execution as the provider reports it."""

    quantity: float
    price: float
    timestamp: datetime
    provider_fill_id: str | None = None
    provider_order_id: str | None = None
    fee: float = 0.0
    side: Direction | None = None


@dataclass(frozen=True, slots=True)
class RemotePosition:
    """One provider-side position. ``quantity`` is signed, not absolute."""

    symbol: str
    quantity: float
    direction: Direction | None = None
    average_entry_price: float | None = None

    @property
    def is_flat(self) -> bool:
        return abs(self.quantity) < 1e-9


@dataclass(frozen=True, slots=True)
class ProviderStateSnapshot:
    """Everything the reconciler gets from one provider read.

    ``orders``/``fills``/``positions`` are what the provider says; the flags
    say whether that answer can be trusted at all.
    """

    orders: tuple[RemoteOrder, ...] = ()
    fills: tuple[RemoteFill, ...] = ()
    positions: tuple[RemotePosition, ...] = ()
    complete: bool = False
    error: str | None = None
    retrieved_at: datetime | None = None
    connection_id: str | None = None
    provider_id: str | None = None
    #: The provider instance that produced this view. Together with the
    #: connection it scopes every order identity in it: a number that looks
    #: like one we hold may have been minted by a different instance, and the
    #: reconciler must never assume otherwise.
    provider_instance_id: str | None = None

    @property
    def is_usable(self) -> bool:
        """True when the answer may be compared against local state."""
        return self.complete and self.error is None

    @property
    def order_count(self) -> int:
        return len(self.orders)


@runtime_checkable
class ProviderStateClient(Protocol):
    """The surface reconciliation and cancellation need from a connection."""

    def fetch_provider_state(self) -> ProviderStateSnapshot:
        """Return the provider's current orders/fills/positions view."""
        ...


def snapshot_from_adapter(adapter, *, now: datetime,
                          connection_id: str | None = None,
                          provider_id: str | None = None) -> ProviderStateSnapshot:
    """Read an execution adapter into a trust-carrying snapshot.

    A failure to read produces a snapshot that says so (``error`` plus
    ``complete=False``) instead of an empty one, because "the provider gave us
    nothing" and "the provider has nothing" must never look the same.
    """
    from backend.execution.models import normalize_order_status

    instance_id = getattr(adapter, "provider_instance_id", None)
    instance_id = str(instance_id) if instance_id else None

    def _fail(message: str) -> ProviderStateSnapshot:
        return ProviderStateSnapshot(error=message, complete=False,
                                     retrieved_at=now, connection_id=connection_id,
                                     provider_id=provider_id,
                                     provider_instance_id=instance_id)

    reader = getattr(adapter, "get_orders", None)
    if not callable(reader):
        return _fail("the execution connection cannot list its orders")
    try:
        remote_orders = []
        remote_fills = []
        for order in reader():
            request = getattr(order, "request", None)
            status = normalize_order_status(getattr(order, "status", None))
            remote_orders.append(RemoteOrder(
                provider_order_id=str(getattr(order, "order_id", "") or ""),
                symbol=str(getattr(request, "symbol", "") or "").strip().upper(),
                quantity=float(getattr(request, "quantity", 0) or 0),
                filled_quantity=float(getattr(order, "filled_quantity", 0) or 0),
                status=status,
                client_order_id=_echoed_client_order_id(request),
                side=getattr(request, "direction", None),
                limit_price=getattr(request, "limit_price", None),
                updated_at=getattr(order, "updated_at", None)
                           or getattr(order, "created_at", None),
            ))
            for fill in (getattr(order, "fills", ()) or ()):
                remote_fills.append(RemoteFill(
                    quantity=float(fill.quantity),
                    price=float(fill.price),
                    timestamp=fill.timestamp,
                    # The provider's own number is what reconciliation
                    # matches on; the platform's identity is only the
                    # fallback for a fill the provider never identified.
                    provider_fill_id=(getattr(fill, "provider_fill_id", None)
                                      or getattr(fill, "fill_id", None) or None),
                    provider_order_id=str(getattr(order, "order_id", "") or ""),
                    fee=float(getattr(fill, "fee", 0) or 0),
                    side=getattr(fill, "side", None),
                ))
        position_reader = getattr(adapter, "get_positions", None)
        remote_positions = []
        if callable(position_reader):
            for position in position_reader():
                remote_positions.append(RemotePosition(
                    symbol=str(getattr(position, "symbol", "") or "").strip().upper(),
                    quantity=float(getattr(position, "quantity", 0) or 0),
                    direction=getattr(position, "direction", None),
                    average_entry_price=getattr(position, "average_entry_price", None),
                ))
    except NotImplementedError as error:
        return _fail(str(error))
    except Exception as error:  # noqa: BLE001 - an unreadable provider is a finding
        return _fail(f"the provider state could not be read: {error}")
    return ProviderStateSnapshot(
        orders=tuple(remote_orders),
        fills=tuple(remote_fills),
        positions=tuple(remote_positions),
        complete=True,
        error=None,
        retrieved_at=now,
        connection_id=connection_id,
        provider_id=provider_id,
        provider_instance_id=instance_id,
    )


def _echoed_client_order_id(request) -> str | None:
    """The platform id the provider echoed back, when it echoes one at all.

    A provider that does not echo it is not a provider that failed: the id
    stays ``None`` and reconciliation falls back to the provider's own
    identifier instead of guessing.
    """
    value = getattr(request, "client_order_id", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
