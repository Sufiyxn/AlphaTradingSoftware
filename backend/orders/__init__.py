"""Canonical order lifecycle, reconciliation, and cancellation.

This package owns what an order *is* and how it may move:

* :mod:`backend.orders.models` - the intent/order/status/fill vocabulary and
  the state machine itself;
* :mod:`backend.orders.lifecycle` - pure transitions that recompute executed
  quantity from the fills actually recorded;
* :mod:`backend.orders.store` - the thread-safe home for open orders and the
  pending-order capacity they reserve for risk;
* :mod:`backend.orders.provider` - the trust-carrying snapshot a connection
  produces for reconciliation;
* :mod:`backend.orders.reconcile` - read-only drift detection;
* :mod:`backend.orders.cancellation` - one honest outcome per cancel attempt;
* :mod:`backend.orders.service` - the single writer: state + journal together.

Nothing here imports a broker SDK, a price feed, or a strategy. The pipeline
calls in; reconciliation reads out.
"""
from .cancellation import CancellationOutcome, CancellationResult, OrderCanceller
from .lifecycle import add_fill, fill_totals, status_for_fill, with_fills, with_status
from .models import (
    Fill,
    InvalidOrderTransition,
    Order,
    OrderIntent,
    OrderStatus,
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
    can_transition,
    is_terminal,
    next_states,
    order_from_dict,
)
from .provider import (
    ProviderStateClient,
    ProviderStateSnapshot,
    RemoteFill,
    RemoteOrder,
    RemotePosition,
)
from .reconcile import (
    DiscrepancyKind,
    OrderReconciler,
    ReconcileDiscrepancy,
    ReconcileStatus,
    ReconciliationReport,
)
from .service import (
    ORDER_EVENTS,
    FillPersistenceError,
    OrderLifecycleService,
    ProviderIdentityCollision,
)
from .store import OrderStore

__all__ = [
    "ORDER_EVENTS",
    "CancellationOutcome",
    "CancellationResult",
    "DiscrepancyKind",
    "Fill",
    "FillPersistenceError",
    "InvalidOrderTransition",
    "Order",
    "OrderCanceller",
    "OrderIntent",
    "OrderLifecycleService",
    "OrderReconciler",
    "OrderStatus",
    "OrderStore",
    "ProviderIdentityCollision",
    "ProviderStateClient",
    "ProviderStateSnapshot",
    "ReconcileDiscrepancy",
    "ReconcileStatus",
    "ReconciliationReport",
    "RemoteFill",
    "RemoteOrder",
    "RemotePosition",
    "TERMINAL_STATUSES",
    "VALID_TRANSITIONS",
    "add_fill",
    "can_transition",
    "fill_totals",
    "is_terminal",
    "next_states",
    "order_from_dict",
    "status_for_fill",
    "with_fills",
    "with_status",
]
