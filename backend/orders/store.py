"""Thread-safe in-memory home for the platform's canonical orders.

The store is the single source of truth for "what orders does this process
believe exist and in what state". It is deliberately a plain, provider-free
object: nothing here calls a broker, reads a price, or decides anything about
risk.

Order identity never changes. ``order_id`` is the platform's
``client_order_id``; the provider's identifier is recorded alongside it and
never replaces it, so a provider that re-identifies, re-uses, or drops ids
cannot hide an order from us.
"""
from __future__ import annotations

import threading

from backend.portfolio.models import PendingOrderReservation

from .lifecycle import with_status
from .models import (
    InvalidOrderTransition,
    Order,
    OrderIntent,
    OrderStatus,
    order_from_dict,
)

__all__ = ["InvalidOrderTransition", "OrderStore"]


class OrderStore:
    """Mutable, lock-guarded collection of :class:`Order` objects."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._lock = threading.RLock()

    # -- creation ---------------------------------------------------------
    def create(self, intent: OrderIntent, now, *,
               connection_id: str | None = None,
               provider_id: str | None = None,
               provider_instance_id: str | None = None,
               status: OrderStatus = OrderStatus.NEW) -> Order:
        """Register a brand-new order. Raises if the id is already taken."""
        order = Order(
            intent=intent,
            status=status,
            created_at=now,
            connection_id=connection_id if connection_id is not None else intent.connection_id,
            provider_id=provider_id if provider_id is not None else intent.provider_id,
            provider_instance_id=provider_instance_id,
            updated_at=now,
        )
        with self._lock:
            if intent.client_order_id in self._orders:
                raise ValueError(f"duplicate order id: {intent.client_order_id}")
            self._orders[intent.client_order_id] = order
        return order

    def save(self, order: Order) -> Order:
        """Replace the stored record for ``order`` (upsert by order id)."""
        with self._lock:
            self._orders[order.client_order_id] = order
        return order

    # -- reads ------------------------------------------------------------
    def get(self, order_id: str | None) -> Order | None:
        if not order_id:
            return None
        with self._lock:
            return self._orders.get(order_id)

    def require(self, order_id: str) -> Order:
        order = self.get(order_id)
        if order is None:
            raise KeyError(f"unknown order: {order_id}")
        return order

    def list(self, *, open_only: bool = False, symbol: str | None = None) -> tuple[Order, ...]:
        with self._lock:
            orders = list(self._orders.values())
        if open_only:
            orders = [order for order in orders if not order.is_terminal]
        if symbol:
            wanted = symbol.strip().upper()
            orders = [order for order in orders if order.symbol == wanted]
        return tuple(sorted(orders, key=lambda order: (order.created_at, order.client_order_id)))

    def open_orders(self) -> tuple[Order, ...]:
        return self.list(open_only=True)

    def find_by_provider_order_id(self, provider_order_id: str | None) -> Order | None:
        if not provider_order_id:
            return None
        with self._lock:
            for order in self._orders.values():
                if order.provider_order_id == provider_order_id:
                    return order
        return None

    def find_all_by_provider_order_id(self, provider_order_id: str | None) -> tuple[Order, ...]:
        """Every local order carrying this provider number.

        A provider number is only unique for the instance that issued it, so
        more than one order can legitimately answer to it. Callers that must
        not guess - cancellation routing, the identity guard - see all of
        them instead of whichever the dictionary yielded first.
        """
        if not provider_order_id:
            return ()
        with self._lock:
            matches = [order for order in self._orders.values()
                       if order.provider_order_id == provider_order_id]
        return tuple(sorted(matches,
                            key=lambda order: (order.created_at, order.client_order_id)))

    def find_by_client_order_id(self, client_order_id: str | None) -> Order | None:
        return self.get(client_order_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._orders)

    def clear(self) -> None:
        with self._lock:
            self._orders.clear()

    # -- risk-facing projection ------------------------------------------
    def pending_reservations(self) -> tuple[PendingOrderReservation, ...]:
        """Capacity every not-yet-filled order still commits.

        Only open orders contribute, and only their *remaining* quantity, so
        an order that has partially filled reserves the unfilled part while
        its filled part shows up as an actual position.
        """
        reservations: list[PendingOrderReservation] = []
        for order in self.open_orders():
            remaining = order.remaining_quantity
            if remaining <= 0:
                continue
            reservations.append(PendingOrderReservation(
                order_id=order.client_order_id,
                symbol=order.symbol,
                direction=order.direction,
                remaining_quantity=remaining,
                reference_price=order.intent.reference_price or 0.0,
            ))
        return tuple(reservations)

    # -- transition helpers ----------------------------------------------
    def apply(self, order: Order, status: OrderStatus, now, **kwargs) -> Order:
        """Move ``order`` to ``status`` and store the result."""
        updated = with_status(order, status, now, **kwargs)
        return self.save(updated)

    # -- restart recovery -------------------------------------------------
    def restore_from_journal(self, journal) -> int:
        """Rebuild open order state from the journal after a restart.

        Later rows for the same order id win, because ``record_order``
        upserts. A row that cannot be parsed is skipped rather than crashing
        startup - but it is never silently dropped from the journal, so the
        order remains visible to reconciliation.
        """
        restored = 0
        if journal is None or not hasattr(journal, "rows"):
            return restored
        try:
            rows = journal.rows("orders")
        except Exception:
            return restored
        rebuilt: dict[str, Order] = {}
        for row in rows:
            context = row.get("context_json") if isinstance(row, dict) else None
            if not isinstance(context, dict):
                continue
            try:
                order = order_from_dict(context)
            except (ValueError, TypeError, KeyError):
                continue
            rebuilt[order.client_order_id] = order
        with self._lock:
            self._orders.update(rebuilt)
            restored = len(rebuilt)
        return restored
