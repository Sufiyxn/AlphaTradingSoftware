"""The canonical order lifecycle service: state, journal, reconciliation.

This is the only component that moves an order and writes it down. The
pipeline asks it to create/submit/fill orders; the reconciler reads what it
stores. Everything it records is written to the journal in the same call that
changes in-memory state, so a restart can rebuild the open-order picture from
durable rows rather than from hope.

Journal event vocabulary (see :data:`ORDER_EVENTS`):

``ORDER_CREATED``
    the platform decided to execute; the provider has not been told yet.
``ORDER_SUBMITTED``
    the request was handed to the provider, which identified it.
``ORDER_STATUS_CHANGED``
    a lifecycle transition was applied from a provider answer.
``ORDER_FILL_RECEIVED`` / ``ORDER_PARTIAL_FILL``
    one more execution was recorded; quantity always follows the fills.
``ORDER_REJECTED`` / ``ORDER_CANCELLED``
    terminal outcomes.
``ORDER_SUBMISSION_FAILED``
    the provider could not be reached; no order state was invented.
``RECONCILIATION_DISCREPANCY``
    the provider and the journal disagree, or the provider could not be read.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from backend.execution.models import OrderStatus
from backend.portfolio.models import Position

from .cancellation import CancellationResult, OrderCanceller
from .lifecycle import add_fill, status_for_fill, with_fills, with_status
from .models import Fill, Order, OrderIntent, can_transition, is_terminal
from .provider import ProviderStateSnapshot
from .reconcile import OrderReconciler, ReconciliationReport, ReconcileStatus
from .store import OrderStore

__all__ = [
    "ORDER_EVENTS",
    "OrderLifecycleService",
]

ORDER_EVENTS: tuple[str, ...] = (
    "ORDER_CREATED",
    "ORDER_SUBMITTED",
    "ORDER_STATUS_CHANGED",
    "ORDER_FILL_RECEIVED",
    "ORDER_PARTIAL_FILL",
    "ORDER_REJECTED",
    "ORDER_CANCELLED",
    "ORDER_SUBMISSION_FAILED",
    "ORDER_CANCELLATION_FAILED",
    "RECONCILIATION_DISCREPANCY",
    "RECONCILIATION_CLEAN",
)


class OrderLifecycleService:
    """Owns order state transitions, their journaling, and reconciliation."""

    def __init__(self, store: OrderStore | None = None, *,
                 journal: Any | None = None,
                 executor: Any | None = None,
                 clock: Callable[[], datetime] | None = None,
                 restore: bool = True) -> None:
        self.store = store if store is not None else OrderStore()
        self._journal = journal
        self._executor = executor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.reconciler = OrderReconciler(self.store)
        self.canceller = OrderCanceller(self.store, executor=executor,
                                        journal=journal, clock=self._clock)
        self.restored_count = 0
        if restore and journal is not None:
            self.restored_count = self.restore()

    # -- lifecycle --------------------------------------------------------
    def create(self, intent: OrderIntent, *, now: datetime | None = None,
               connection_id: str | None = None,
               provider_id: str | None = None) -> Order:
        """Register a new order as ``NEW`` and journal the decision."""
        moment = now or self._clock()
        order = self.store.create(intent, moment, connection_id=connection_id,
                                  provider_id=provider_id)
        self._persist(order, moment, "ORDER_CREATED",
                      "order created locally and awaiting submission",
                      {"status": order.status.value})
        return order

    def mark_submitted(self, order_id: str, provider_order_id: str | None,
                       now: datetime | None = None) -> Order:
        """Record that the request reached the provider.

        ``provider_order_id`` is stored verbatim when the provider gave one
        and is left ``None`` when it did not - the platform never invents a
        provider identity to fill the gap.
        """
        moment = now or self._clock()
        order = self.store.require(order_id)
        updated = with_status(order, OrderStatus.SUBMITTED, moment,
                              provider_order_id=provider_order_id)
        self.store.save(updated)
        self._persist(updated, moment, "ORDER_SUBMITTED",
                      "order submitted to the provider",
                      {"provider_order_id": updated.provider_order_id,
                       "status": updated.status.value})
        return updated

    def apply_status(self, order_id: str, status: OrderStatus,
                     now: datetime | None = None, *,
                     provider_order_id: str | None = None,
                     rejection_reason: str | None = None) -> Order:
        """Apply one provider-reported status transition."""
        moment = now or self._clock()
        order = self.store.require(order_id)
        updated = with_status(order, status, moment,
                              provider_order_id=provider_order_id,
                              rejection_reason=rejection_reason)
        self.store.save(updated)
        if status is OrderStatus.REJECTED:
            event = "ORDER_REJECTED"
        elif status is OrderStatus.CANCELLED:
            event = "ORDER_CANCELLED"
        else:
            event = "ORDER_STATUS_CHANGED"
        self._persist(updated, moment, event,
                      f"order status is now {status.value}",
                      {"from": order.status.value, "to": status.value})
        return updated

    def apply_provider_order(self, order_id: str, provider_order: Any,
                             now: datetime | None = None) -> Order:
        """Adopt a status (and any fills) the provider returned."""
        moment = now or self._clock()
        order = self.store.require(order_id)
        fills = tuple(getattr(provider_order, "fills", ()) or ())
        if fills:
            order = with_fills(order, fills, moment,
                               provider_order_id=_provider_id(provider_order)
                               or order.provider_order_id)
            self.store.save(order)
            self._persist_fills(order)
        status = getattr(provider_order, "status", None)
        if isinstance(status, OrderStatus) and status is not order.status:
            order = self.apply_status(order_id, status, moment,
                                      provider_order_id=_provider_id(provider_order),
                                      rejection_reason=getattr(provider_order,
                                                               "rejection_reason", None))
        if fills:
            # The executions arrive with the acknowledgement, so they are
            # journaled here too - the pipeline never calls ``record_fill``.
            event = ("ORDER_PARTIAL_FILL"
                     if order.status is OrderStatus.PARTIALLY_FILLED
                     else "ORDER_FILL_RECEIVED")
            self._event(moment, event, "provider reported an execution",
                        symbol=order.symbol,
                        context={"order_id": order.client_order_id,
                                 "fill_count": len(order.fills),
                                 "filled_quantity": order.filled_quantity,
                                 "status": order.status.value})
        return order

    def record_fill(self, order_id: str, fill: Fill, now: datetime | None = None) -> Order:
        """Record one provider-reported execution and follow it to a status.

        The executed quantity is recomputed from the fills themselves; the
        order is never told it executed more than the executions it carries.
        """
        moment = now or self._clock()
        order = self.store.require(order_id)
        updated = add_fill(order, fill, moment)
        implied = status_for_fill(updated, updated.filled_quantity)
        transition_ok = implied is None or can_transition(order.status, implied)
        if transition_ok and implied is not None:
            updated = with_status(updated, implied, moment)
        self.store.save(updated)

        recorded_new_fill = len(updated.fills) > len(order.fills)
        if recorded_new_fill:
            event = ("ORDER_PARTIAL_FILL"
                     if updated.status is OrderStatus.PARTIALLY_FILLED
                     else "ORDER_FILL_RECEIVED")
            self._persist(updated, moment, event,
                          "provider reported an execution",
                          {"fill_id": fill.fill_id,
                           "provider_fill_id": fill.provider_fill_id,
                           "quantity": fill.quantity,
                           "price": fill.price,
                           "filled_quantity": updated.filled_quantity,
                           "status": updated.status.value})
        blocked = implied is not None and not transition_ok
        late = is_terminal(order.status) and order.status is not OrderStatus.FILLED
        if recorded_new_fill and (blocked or late):
            self._event(moment, "RECONCILIATION_DISCREPANCY",
                        "an execution arrived that the order's current status "
                        "cannot account for",
                        symbol=updated.symbol,
                        context={"order_id": updated.client_order_id,
                                 "status": updated.status.value,
                                 "quantity": fill.quantity})
        return updated

    def mark_submission_failed(self, order_id: str, reason: str,
                               now: datetime | None = None) -> Order | None:
        """Journal a failed submission without inventing any order state.

        The order stays exactly where it was - the provider may or may not
        have received it, and pretending otherwise would hide the order from
        reconciliation.
        """
        moment = now or self._clock()
        order = self.store.get(order_id)
        if order is None:
            return None
        self._event(moment, "ORDER_SUBMISSION_FAILED", reason, symbol=order.symbol,
                    context={"order_id": order.client_order_id,
                             "status": order.status.value})
        return order

    def note_submission_failure(self, intent: OrderIntent, reason: str,
                                now: datetime | None = None, *,
                                connection_id: str | None = None,
                                provider_id: str | None = None) -> None:
        """Journal a submission that produced no order at all.

        Nothing is added to the store: the platform never records an order it
        does not have. The intent travels with the event so a human can see
        exactly what was attempted.
        """
        moment = now or self._clock()
        self._event(moment, "ORDER_SUBMISSION_FAILED", reason,
                    symbol=intent.symbol,
                    context={"order_id": intent.client_order_id,
                             "symbol": intent.symbol,
                             "direction": intent.direction.value,
                             "quantity": intent.quantity,
                             "connection_id": connection_id or intent.connection_id,
                             "provider_id": provider_id or intent.provider_id})

    def note_fill_gap(self, order_id: str, reported_quantity: float,
                      now: datetime | None = None) -> None:
        """Journal a provider that claims more executed quantity than fills.

        The extra quantity is *not* turned into a fill: an execution the
        provider will not describe cannot be priced, and inventing one would
        corrupt the average price and every position booked from it.
        """
        moment = now or self._clock()
        order = self.store.get(order_id)
        if order is None:
            return
        self._event(moment, "RECONCILIATION_DISCREPANCY",
                    "the provider reports more executed quantity than the "
                    "executions it described",
                    symbol=order.symbol,
                    context={"order_id": order.client_order_id,
                             "reported_filled_quantity": float(reported_quantity),
                             "recorded_fill_quantity": float(order.filled_quantity),
                             "fill_count": len(order.fills)})

    # -- reconciliation ---------------------------------------------------
    def reconcile(self, snapshot: ProviderStateSnapshot, *,
                  positions: tuple[Position, ...] = (),
                  now: datetime | None = None) -> ReconciliationReport:
        """Compare the journal with the provider and journal every finding."""
        moment = now or self._clock()
        report = self.reconciler.reconcile(snapshot, positions=positions, now=moment)
        if report.status is ReconcileStatus.MATCH:
            self._event(moment, "RECONCILIATION_CLEAN", report.message,
                        context=report.to_dict())
        elif report.status in (ReconcileStatus.PROVIDER_ERROR,
                               ReconcileStatus.INCOMPLETE):
            self._event(moment, "RECONCILIATION_DISCREPANCY", report.message,
                        context=report.to_dict())
        else:
            for item in report.discrepancies:
                self._event(moment, "RECONCILIATION_DISCREPANCY", item.message,
                            symbol=item.symbol, context=item.to_dict())
        return report

    # -- cancellation -----------------------------------------------------
    def cancel(self, order_id: str) -> CancellationResult:
        """Attempt a cancellation and journal the outcome."""
        return self.canceller.cancel(order_id)

    # -- recovery ---------------------------------------------------------
    def attach_journal(self, journal: Any | None) -> None:
        """Point the lifecycle at an audit journal and rebuild from it.

        The pipeline is often constructed before a journal exists (the host
        attaches one later), so this is the moment open orders become
        durable again after a restart.
        """
        self._journal = journal
        self.canceller = OrderCanceller(self.store, executor=self._executor,
                                        journal=journal, clock=self._clock)
        self.restored_count = self.restore() if journal is not None else 0

    def restore(self) -> int:
        """Rebuild open orders from the journal after a restart."""
        self.restored_count = self.store.restore_from_journal(self._journal)
        return self.restored_count

    # -- views ------------------------------------------------------------
    def get(self, order_id: str) -> Order | None:
        return self.store.get(order_id)

    def list(self, *, open_only: bool = False, symbol: str | None = None) -> tuple[Order, ...]:
        return self.store.list(open_only=open_only, symbol=symbol)

    def pending_reservations(self):
        return self.store.pending_reservations()

    def describe(self, *, open_only: bool = False) -> tuple[dict, ...]:
        return tuple(order.describe() for order in self.list(open_only=open_only))

    # -- journal helpers --------------------------------------------------
    def _persist(self, order: Order, now: datetime, event_type: str, message: str,
                 context: dict | None = None) -> None:
        if self._journal is None:
            return
        payload = {"order_id": order.client_order_id}
        if context:
            payload.update(context)
        try:
            self._journal.record_order(order)
        except Exception:  # noqa: BLE001 - never let persistence mask a transition
            pass
        self._persist_fills(order)
        self._event(now, event_type, message, symbol=order.symbol, context=payload)

    def _persist_fills(self, order: Order) -> None:
        if self._journal is None or not order.fills:
            return
        record = getattr(self._journal, "record_order_fill", None)
        if record is None:
            return
        for fill in order.fills:
            try:
                record(fill, order_id=order.client_order_id, symbol=order.symbol)
            except Exception:  # noqa: BLE001 - a fill row must never block a fill
                pass

    def _event(self, now: datetime, event_type: str, message: str, *,
               symbol: str | None = None, context: dict | None = None) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record_event(now, event_type, message, symbol=symbol,
                                       context=context or {})
        except Exception:  # noqa: BLE001 - a journal failure must stay invisible
            pass


def _provider_id(provider_order: Any) -> str | None:
    value = getattr(provider_order, "order_id", None)
    return value if isinstance(value, str) and value else None

