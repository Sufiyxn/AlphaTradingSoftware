"""Order cancellation: one honest outcome per attempt.

Cancelling is a request, not a command. The provider may already have filled
the order, may refuse, may not support cancellation at all, or may fail
outright - and every one of those has its own outcome instead of being folded
into a generic "failed".

Two invariants:

* a provider failure never changes local state, so an order that could not be
  cancelled is still visible as open and still reserves capacity;
* an idempotent second cancel of an already-cancelled order is reported as
  ``ALREADY_CANCELLED`` (success) rather than as an error.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from backend.execution.models import OrderStatus

from .lifecycle import InvalidOrderTransition, with_fills, with_status
from .models import Order, is_terminal
from .store import OrderStore

__all__ = ["CancellationOutcome", "CancellationResult", "OrderCanceller"]


class CancellationOutcome(str, Enum):
    CANCELLED = "CANCELLED"
    ALREADY_CANCELLED = "ALREADY_CANCELLED"
    NOT_CANCELABLE = "NOT_CANCELABLE"
    REJECTED_BY_PROVIDER = "REJECTED_BY_PROVIDER"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"


_SUCCESS_OUTCOMES = frozenset({CancellationOutcome.CANCELLED,
                               CancellationOutcome.ALREADY_CANCELLED})


@dataclass(frozen=True, slots=True)
class CancellationResult:
    """What happened when one cancellation was attempted."""

    outcome: CancellationOutcome
    order_id: str
    message: str
    status: OrderStatus | None = None
    provider_order_id: str | None = None
    symbol: str | None = None
    attempted_at: datetime | None = None

    @property
    def success(self) -> bool:
        """True only when the order is now (or already was) cancelled."""
        return self.outcome in _SUCCESS_OUTCOMES

    @property
    def visible_failure(self) -> bool:
        """True for every attempt that did not result in a cancellation."""
        return not self.success

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "success": self.success,
            "order_id": self.order_id,
            "status": self.status.value if self.status else None,
            "provider_order_id": self.provider_order_id,
            "symbol": self.symbol,
            "message": self.message,
            "attempted_at": self.attempted_at.isoformat() if self.attempted_at else None,
        }


class OrderCanceller:
    """Performs cancellations against a provider and records the outcome.

    ``executor`` is any object exposing ``cancel_order(provider_order_id)``
    (the execution router or a single adapter). It is optional: without one
    every attempt reports ``PROVIDER_ERROR`` and local state is untouched,
    which is the honest answer for a process that is not connected.
    """

    def __init__(self, store: OrderStore, *, executor: Any | None = None,
                 journal: Any | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._store = store
        self._executor = executor
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def cancel(self, order_id: str) -> CancellationResult:
        now = self._clock()
        order = self._store.get(order_id)
        if order is None:
            return self._record(CancellationResult(
                outcome=CancellationOutcome.ORDER_NOT_FOUND,
                order_id=str(order_id or ""),
                message="no such order in the local journal",
                attempted_at=now))

        if is_terminal(order.status):
            if order.status is OrderStatus.CANCELLED:
                return self._record(CancellationResult(
                    outcome=CancellationOutcome.ALREADY_CANCELLED,
                    order_id=order.client_order_id,
                    status=order.status,
                    provider_order_id=order.provider_order_id,
                    symbol=order.symbol,
                    message="order is already cancelled",
                    attempted_at=now))
            return self._record(CancellationResult(
                outcome=CancellationOutcome.NOT_CANCELABLE,
                order_id=order.client_order_id,
                status=order.status,
                provider_order_id=order.provider_order_id,
                symbol=order.symbol,
                message=f"order is {order.status.value} and can no longer be cancelled",
                attempted_at=now))

        if order.status is OrderStatus.NEW:
            # Nothing was ever handed to a provider, so there is nothing to
            # cancel remotely - closing it locally is the whole operation.
            cancelled = self._store.apply(order, OrderStatus.CANCELLED, now)
            return self._record(CancellationResult(
                outcome=CancellationOutcome.CANCELLED,
                order_id=cancelled.client_order_id,
                status=cancelled.status,
                provider_order_id=cancelled.provider_order_id,
                symbol=cancelled.symbol,
                message="order had not been submitted; closed locally without a provider call",
                attempted_at=now))

        if self._executor is None:
            return self._failure(CancellationOutcome.PROVIDER_ERROR, order,
                                 "no execution connection is configured", now)

        if not order.provider_order_id:
            return self._failure(CancellationOutcome.PROVIDER_ERROR, order,
                                 "the provider never identified this order, so it "
                                 "cannot be cancelled remotely", now)

        try:
            provider_order = self._executor.cancel_order(order.provider_order_id)
        except NotImplementedError:
            return self._failure(CancellationOutcome.REJECTED_BY_PROVIDER, order,
                                 "this connection does not support order cancellation",
                                 now)
        except Exception as exc:  # noqa: BLE001 - the failure must stay visible
            return self._failure(CancellationOutcome.PROVIDER_ERROR, order,
                                 f"the provider could not be reached: {exc}", now)

        return self._adopt(order, provider_order, now)

    # -- internals --------------------------------------------------------
    def _adopt(self, order: Order, provider_order: Any, now: datetime) -> CancellationResult:
        status = _status_of(provider_order)
        if status is None:
            return self._failure(CancellationOutcome.REJECTED_BY_PROVIDER, order,
                                 "the provider did not report a usable status", now)

        try:
            updated = with_fills(order, tuple(getattr(provider_order, "fills", ()) or order.fills),
                                 now, provider_order_id=_provider_id(provider_order)
                                 or order.provider_order_id)
            updated = with_status(updated, status, now)
        except (InvalidOrderTransition, ValueError):
            return self._failure(CancellationOutcome.REJECTED_BY_PROVIDER, order,
                                 f"the provider answered {status.value}, which this "
                                 f"order cannot move to from {order.status.value}",
                                 now)
        self._store.save(updated)

        if status is OrderStatus.CANCELLED:
            outcome = CancellationOutcome.CANCELLED
            message = "order cancelled at the provider"
        elif status is OrderStatus.FILLED:
            outcome = CancellationOutcome.NOT_CANCELABLE
            message = "the provider filled the order before the cancellation took effect"
        else:
            outcome = CancellationOutcome.REJECTED_BY_PROVIDER
            message = f"the provider answered {status.value}"
        return self._record(CancellationResult(
            outcome=outcome,
            order_id=updated.client_order_id,
            status=updated.status,
            provider_order_id=updated.provider_order_id,
            symbol=updated.symbol,
            message=message,
            attempted_at=now))

    def _failure(self, outcome: CancellationOutcome, order: Order, message: str,
                 now: datetime) -> CancellationResult:
        # Local state is deliberately left exactly as it was: the order is
        # still open, still reserved, and still visible as an open order.
        return self._record(CancellationResult(
            outcome=outcome,
            order_id=order.client_order_id,
            status=order.status,
            provider_order_id=order.provider_order_id,
            symbol=order.symbol,
            message=message,
            attempted_at=now))

    def _record(self, result: CancellationResult) -> CancellationResult:
        if self._journal is None:
            return result
        try:
            self._journal.record_event(
                result.attempted_at or self._clock(),
                "ORDER_CANCELLED" if result.success else "ORDER_CANCELLATION_FAILED",
                result.message,
                symbol=result.symbol,
                context=result.to_dict())
        except Exception:  # noqa: BLE001 - journaling must not mask the outcome
            pass
        return result


def _status_of(provider_order: Any) -> OrderStatus | None:
    status = getattr(provider_order, "status", None)
    if isinstance(status, OrderStatus):
        return status
    if isinstance(status, str):
        from backend.execution.models import normalize_order_status
        normalized = normalize_order_status(status)
        return normalized if normalized is not OrderStatus.UNKNOWN else None
    return None


def _provider_id(provider_order: Any) -> str | None:
    return getattr(provider_order, "order_id", None)
