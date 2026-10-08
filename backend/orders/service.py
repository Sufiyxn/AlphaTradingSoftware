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
    the provider and the journal disagree, the provider could not be read, or
    a provider order identity collided with one the journal already owns.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from backend.execution.fill_identity import normalize_fill
from backend.execution.models import OrderStatus
from backend.portfolio.models import Position

from .cancellation import CancellationResult, OrderCanceller
from .lifecycle import add_fill, status_for_fill, with_fills, with_status
from .models import Fill, Order, OrderIntent, can_transition, is_terminal
from .provider import ProviderStateSnapshot
from .reconcile import (
    DiscrepancyKind,
    OrderReconciler,
    ReconciliationReport,
    ReconcileStatus,
)
from .store import OrderStore

__all__ = [
    "ORDER_EVENTS",
    "FillPersistenceError",
    "OrderLifecycleService",
    "ProviderIdentityCollision",
]


class FillPersistenceError(RuntimeError):
    """A provider fill could not be written to the durable fill journal.

    Raised fail-closed: the fill is not merged into the canonical order and
    the caller is told the write did not happen, instead of the platform
    pretending an execution is durable when it is not. The identity of the
    fill and the order it belongs to travel with the error, so the failure
    is diagnosable without exposing anything secret.
    """

    def __init__(self, *, fill_id: str, order_id: str | None,
                 reason: str) -> None:
        self.fill_id = fill_id
        self.order_id = order_id
        self.reason = str(reason)
        super().__init__(
            f"provider fill {fill_id!r} for order {order_id!r} was not "
            f"persisted: {self.reason}")


class ProviderIdentityCollision(Exception):
    """A provider handed back an order identity another local order owns.

    Raised fail-closed: the platform refuses to register the number rather
    than adopt an identity it cannot prove unique, and journals why. The
    order that already owns the identity keeps it; the order being submitted
    is never allowed to take it over.
    """

    def __init__(self, *, provider_order_id: str,
                 provider_instance_id: str | None, connection_id: str | None,
                 order_id: str, conflicting_order_id: str) -> None:
        self.provider_order_id = provider_order_id
        self.provider_instance_id = provider_instance_id
        self.connection_id = connection_id
        self.order_id = order_id
        self.conflicting_order_id = conflicting_order_id
        super().__init__(
            f"provider order identity {provider_order_id!r} (connection "
            f"{connection_id!r}, instance {provider_instance_id!r}) is already "
            f"owned by order {conflicting_order_id!r}; order {order_id!r} was "
            f"not registered with it")

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
                                        journal=journal, clock=self._clock,
                                        persist=self._persist_order)
        self.restored_count = 0
        if restore and journal is not None:
            self.restored_count = self.restore()

    # -- lifecycle --------------------------------------------------------
    def create(self, intent: OrderIntent, *, now: datetime | None = None,
               connection_id: str | None = None,
               provider_id: str | None = None,
               provider_instance_id: str | None = None) -> Order:
        """Register a new order as ``NEW`` and journal the decision."""
        moment = now or self._clock()
        order = self.store.create(intent, moment, connection_id=connection_id,
                                  provider_id=provider_id,
                                  provider_instance_id=provider_instance_id)
        self._persist(order, moment, "ORDER_CREATED",
                      "order created locally and awaiting submission",
                      {"status": order.status.value})
        return order

    def mark_submitted(self, order_id: str, provider_order_id: str | None,
                       now: datetime | None = None, *,
                       provider_instance_id: str | None = None) -> Order:
        """Record that the request reached the provider.

        ``provider_order_id`` is stored verbatim when the provider gave one
        and is left ``None`` when it did not - the platform never invents a
        provider identity to fill the gap. Registering one also registers
        which provider instance issued it, and refuses the registration
        outright when another local order already owns that identity.
        """
        moment = now or self._clock()
        order = self.store.require(order_id)
        instance = (provider_instance_id if provider_instance_id is not None
                    else order.provider_instance_id)
        conflict = self._identity_conflict(order, provider_order_id, instance)
        if conflict is not None:
            collision = ProviderIdentityCollision(
                provider_order_id=provider_order_id,
                provider_instance_id=instance,
                connection_id=order.connection_id,
                order_id=order.client_order_id,
                conflicting_order_id=conflict.client_order_id)
            self._event(moment, "RECONCILIATION_DISCREPANCY", str(collision),
                        symbol=order.symbol,
                        context={"kind": DiscrepancyKind.PROVIDER_ORDER_ID_COLLISION.value,
                                 "order_id": order.client_order_id,
                                 "conflicting_order_id": conflict.client_order_id,
                                 "connection_id": order.connection_id,
                                 "provider_instance_id": instance,
                                 "provider_order_id": provider_order_id,
                                 "status": order.status.value})
            raise collision
        updated = with_status(order, OrderStatus.SUBMITTED, moment,
                              provider_order_id=provider_order_id,
                              provider_instance_id=instance)
        self.store.save(updated)
        self._persist(updated, moment, "ORDER_SUBMITTED",
                      "order submitted to the provider",
                      {"provider_order_id": updated.provider_order_id,
                       "provider_instance_id": updated.provider_instance_id,
                       "status": updated.status.value})
        return updated

    def _identity_conflict(self, order: Order, provider_order_id: str | None,
                           provider_instance_id: str | None) -> Order | None:
        """The other local order that already owns this provider identity.

        Scope decides. Orders on provably different connections, or issued by
        provably different provider instances, are different identities and
        may share a number. Anything the platform cannot tell apart is
        treated as the same identity, so an unknown scope fails closed
        rather than quietly letting two orders claim one number.
        """
        if not provider_order_id:
            return None
        for candidate in self.store.find_all_by_provider_order_id(provider_order_id):
            if candidate.client_order_id == order.client_order_id:
                continue  # re-stating our own identity is not a conflict
            if (order.connection_id and candidate.connection_id
                    and order.connection_id != candidate.connection_id):
                continue
            if (provider_instance_id and candidate.provider_instance_id
                    and provider_instance_id != candidate.provider_instance_id):
                continue
            return candidate
        return None

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
        """Adopt a status (and any fills) the provider returned.

        Fills are identified and written to the durable fill journal *before*
        they are merged: a fill that cannot be persisted never appears in the
        canonical order as if it had been. A replayed fill is one harmless
        ``INSERT OR IGNORE``, so reprocessing the same provider answer stays
        one row.
        """
        moment = now or self._clock()
        order = self.store.require(order_id)
        fills = tuple(getattr(provider_order, "fills", ()) or ())
        if fills:
            identity = _provider_id(provider_order) or order.provider_order_id
            fills = tuple(self._identify(order, fill, provider_order_id=identity)
                          for fill in fills)
            self._persist_fills(order, fills)
            order = with_fills(order, fills, moment, provider_order_id=identity)
            self.store.save(order)
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
        The fill is identified and persisted before it is merged, so a write
        that fails surfaces as :class:`FillPersistenceError` instead of a
        fill that only exists in memory.
        """
        moment = now or self._clock()
        order = self.store.require(order_id)
        fill = self._identify(order, fill)
        self._persist_fills(order, (fill,))
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
                                        journal=journal, clock=self._clock,
                                        persist=self._persist_order)
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
        self._persist_order(order)
        self._event(now, event_type, message, symbol=order.symbol, context=payload)

    def _persist_order(self, order: Order) -> None:
        """Write the order row and its fills.

        The row write never raises: an audit write must not mask a state
        transition. The fill write is different - it is durability for real
        executions, so its failures are propagated by :meth:`_persist_fills`
        rather than swallowed. Fills are normally already durable from the
        pre-merge write; this re-asserts them for callers (events, status
        changes, cancellation) that persist a whole order at once.

        This is the durability half of a state change; the event half is
        journaled separately, so a component that journals its own outcome
        (the canceller) writes its row exactly once and its event exactly
        once instead of doubling either.
        """
        if self._journal is None:
            return
        # The fill write comes first on purpose: the order row serializes the
        # fills too, and ``restore`` rebuilds from those rows - so a row
        # written ahead of a failed fill write would resurrect an execution
        # the fill journal refused. When the fills cannot be durable, the row
        # that would carry them is never written.
        self._persist_fills(order)
        try:
            self._journal.record_order(order)
        except Exception:  # noqa: BLE001 - never let a row write mask a transition
            pass

    def _persist_fills(self, order: Order,
                       fills: tuple[Fill, ...] | None = None) -> None:
        """Write provider fills to the durable fill journal, fail closed.

        This is the only place a fill becomes durable. It raises
        :class:`FillPersistenceError` rather than swallowing a journal
        failure: a fill that could not be written must never look like one
        that could. An already-written fill is simply ignored by the
        journal's idempotent insert, so re-persisting an order's fills is
        harmless. With no journal attached nothing is durable and nothing
        pretends to be - the write is skipped, exactly like every other
        persistence this service does.
        """
        pending = order.fills if fills is None else tuple(fills)
        if self._journal is None or not pending:
            return
        record = getattr(self._journal, "record_provider_fill", None)
        if record is None:
            raise FillPersistenceError(
                fill_id=pending[0].fill_id or "", order_id=order.client_order_id,
                reason="this journal cannot store durable provider fills")
        for fill in pending:
            try:
                record(fill, order_id=order.client_order_id,
                       client_order_id=order.client_order_id,
                       symbol=order.symbol,
                       connection_id=order.connection_id,
                       provider_instance_id=order.provider_instance_id)
            except FillPersistenceError:
                raise
            except Exception as error:  # noqa: BLE001 - re-raised, never hidden
                raise FillPersistenceError(
                    fill_id=fill.fill_id or "", order_id=order.client_order_id,
                    reason=error) from error

    def _identify(self, order: Order, fill: Fill, *,
                  provider_order_id: str | None = None) -> Fill:
        """The fill with its durable identity, scoped to the owning order."""
        return normalize_fill(fill,
                              connection_id=order.connection_id,
                              provider_instance_id=order.provider_instance_id,
                              client_order_id=order.client_order_id,
                              provider_order_id=(provider_order_id
                                                 if provider_order_id is not None
                                                 else order.provider_order_id))

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

