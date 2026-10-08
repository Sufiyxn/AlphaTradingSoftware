"""Pure functions that move an order through its lifecycle.

Every helper here is a transformation on an immutable :class:`Order` - no
store, no journal, no provider, no clock. The caller owns time and persistence
so that the same transition logic can be exercised without a running system.

Two rules hold everywhere:

* a transition the state machine does not allow raises
  :class:`InvalidOrderTransition` instead of quietly jumping status;
* quantity is always recomputed from the fills actually carried by the order,
  so an order can never report more executed quantity than the executions
  behind it. If a provider reports a filled quantity larger than the sum of
  the fills it gave us, that gap is a discrepancy to journal - never a fill to
  invent.
"""
from __future__ import annotations

from datetime import datetime

from .models import (
    Fill,
    InvalidOrderTransition,
    Order,
    OrderStatus,
    can_transition,
    is_terminal,
)

__all__ = [
    "InvalidOrderTransition",
    "add_fill",
    "fill_totals",
    "status_for_fill",
    "with_fills",
    "with_status",
]


def fill_totals(fills: tuple[Fill, ...]) -> tuple[float, float | None]:
    """``(executed quantity, volume-weighted average price)`` for these fills."""
    quantity = 0.0
    notional = 0.0
    for fill in fills:
        quantity += float(fill.quantity)
        notional += float(fill.quantity) * float(fill.price)
    if quantity <= 0:
        return 0.0, None
    return quantity, notional / quantity


def with_status(order: Order, status: OrderStatus, now: datetime, *,
                provider_order_id: str | None = None,
                provider_instance_id: str | None = None,
                rejection_reason: str | None = None) -> Order:
    """Move ``order`` to ``status``, or raise if the move is not permitted.

    Re-applying the status the order already holds is a no-op apart from the
    timestamp, which keeps provider polling idempotent. ``provider_order_id``
    is only ever set from a value the provider actually returned; passing
    ``None`` never clears an identity the provider already gave us. The
    instance that minted that identity travels with it the same way.
    """
    if not isinstance(status, OrderStatus):
        raise ValueError("an order status must be an OrderStatus")
    if not can_transition(order.status, status):
        raise InvalidOrderTransition(order.status, status)
    identity = provider_order_id if provider_order_id is not None else order.provider_order_id
    instance = (provider_instance_id if provider_instance_id is not None
                else order.provider_instance_id)
    reason = rejection_reason if rejection_reason is not None else order.rejection_reason
    submitted_at = order.submitted_at
    if status is not OrderStatus.NEW and submitted_at is None:
        submitted_at = now
    return Order(
        intent=order.intent,
        status=status,
        created_at=order.created_at,
        provider_order_id=identity,
        fills=order.fills,
        filled_quantity=order.filled_quantity,
        average_fill_price=order.average_fill_price,
        updated_at=now,
        submitted_at=submitted_at,
        rejection_reason=reason,
        connection_id=order.connection_id,
        provider_id=order.provider_id,
        provider_instance_id=instance,
    )


def with_fills(order: Order, fills: tuple[Fill, ...], now: datetime, *,
               provider_order_id: str | None = None) -> Order:
    """Replace the recorded fills and recompute executed quantity/average.

    Fills are de-duplicated by the provider's own fill identifier when it has
    one and by the platform's identifier otherwise, so replaying the same
    execution twice cannot double-count a position.
    """
    identity = provider_order_id if provider_order_id is not None else order.provider_order_id
    merged: list[Fill] = []
    seen: set[str] = set()
    for fill in fills:
        key = fill.provider_fill_id or fill.fill_id
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        merged.append(fill)
    quantity, average = fill_totals(tuple(merged))
    return Order(
        intent=order.intent,
        status=order.status,
        created_at=order.created_at,
        provider_order_id=identity,
        fills=tuple(merged),
        filled_quantity=quantity,
        average_fill_price=average,
        updated_at=now,
        submitted_at=order.submitted_at,
        rejection_reason=order.rejection_reason,
        connection_id=order.connection_id,
        provider_id=order.provider_id,
        provider_instance_id=order.provider_instance_id,
    )


def add_fill(order: Order, fill: Fill, now: datetime, *,
             provider_order_id: str | None = None) -> Order:
    """Append one provider-reported execution to the order."""
    return with_fills(order, tuple(order.fills) + (fill,), now,
                      provider_order_id=provider_order_id)


def status_for_fill(order: Order, filled_quantity: float) -> OrderStatus | None:
    """The lifecycle status implied by an executed quantity, or ``None``.

    Returns ``None`` when the quantity says nothing new - an already terminal
    order is never rewritten by a late fill report.
    """
    if is_terminal(order.status) and order.status is not OrderStatus.FILLED:
        return None
    if filled_quantity <= 0:
        return None
    if filled_quantity + 1e-9 >= float(order.intent.quantity):
        return OrderStatus.FILLED
    return OrderStatus.PARTIALLY_FILLED
