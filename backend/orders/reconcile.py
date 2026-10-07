"""Order/fill/position reconciliation between local state and a provider.

The reconciler is read-only: it compares and reports, it never rewrites an
order, never books a fill, and never "fixes" a position. Repairing drift is a
decision made with full information, not something a background comparison
should do silently.

An answer is only allowed to be clean when the provider's snapshot said it was
complete and error-free:

* ``PROVIDER_ERROR`` / ``INCOMPLETE`` - the provider did not give us
  everything, so ``discrepancies`` is empty and the status is *not* ``MATCH``.
  Absence of findings here means "unknown", never "fine".
* ``MATCH`` - complete, error-free, and no differences found.
* ``DISCREPANCIES`` - complete, error-free, and at least one difference.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from backend.execution.models import OrderStatus, normalize_order_status
from backend.portfolio.models import Position

from .models import Order
from .provider import ProviderStateSnapshot, RemoteFill, RemoteOrder
from .store import OrderStore

__all__ = [
    "DiscrepancyKind",
    "OrderReconciler",
    "ReconcileDiscrepancy",
    "ReconcileStatus",
    "ReconciliationReport",
]

_QUANTITY_EPSILON = 1e-9


class DiscrepancyKind(str, Enum):
    """Machine-readable difference classes the reconciler can report."""

    LOCAL_ORDER_MISSING_REMOTELY = "LOCAL_ORDER_MISSING_REMOTELY"
    REMOTE_ORDER_UNKNOWN_LOCALLY = "REMOTE_ORDER_UNKNOWN_LOCALLY"
    STATUS_MISMATCH = "STATUS_MISMATCH"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    PARTIAL_FILL_MISMATCH = "PARTIAL_FILL_MISMATCH"
    FILL_MISSING_LOCALLY = "FILL_MISSING_LOCALLY"
    DUPLICATE_FILL = "DUPLICATE_FILL"
    DUPLICATE_PROVIDER_ORDER = "DUPLICATE_PROVIDER_ORDER"
    TERMINAL_LOCAL_OPEN_REMOTELY = "TERMINAL_LOCAL_OPEN_REMOTELY"
    POSITION_QUANTITY_MISMATCH = "POSITION_QUANTITY_MISMATCH"
    POSITION_SIDE_MISMATCH = "POSITION_SIDE_MISMATCH"
    POSITION_UNKNOWN_LOCALLY = "POSITION_UNKNOWN_LOCALLY"
    POSITION_MISSING_REMOTELY = "POSITION_MISSING_REMOTELY"


class ReconcileStatus(str, Enum):
    MATCH = "MATCH"
    DISCREPANCIES = "DISCREPANCIES"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True, slots=True)
class ReconcileDiscrepancy:
    """One difference between the local journal and the provider."""

    kind: DiscrepancyKind
    message: str
    order_id: str | None = None
    symbol: str | None = None
    local_value: str | None = None
    remote_value: str | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "local_value": self.local_value,
            "remote_value": self.remote_value,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """The reconciler's verdict for one provider snapshot."""

    status: ReconcileStatus
    discrepancies: tuple[ReconcileDiscrepancy, ...]
    checked_at: datetime
    connection_id: str | None = None
    provider_id: str | None = None
    local_order_count: int = 0
    remote_order_count: int = 0
    matched_order_count: int = 0
    detail: str | None = None

    @property
    def clean(self) -> bool:
        """True only for a complete, error-free, difference-free answer."""
        return self.status is ReconcileStatus.MATCH

    @property
    def unresolved(self) -> bool:
        """True when we could not get a trustworthy answer at all."""
        return self.status in {ReconcileStatus.PROVIDER_ERROR,
                               ReconcileStatus.INCOMPLETE}

    @property
    def message(self) -> str:
        if self.detail:
            return self.detail
        if self.status is ReconcileStatus.MATCH:
            return "local journal and provider agree"
        if self.status is ReconcileStatus.INCOMPLETE:
            return "provider snapshot was incomplete; state unresolved"
        if self.status is ReconcileStatus.PROVIDER_ERROR:
            return "provider could not be read; state unresolved"
        return f"{len(self.discrepancies)} discrepancy(ies) detected"

    @property
    def kinds(self) -> tuple[DiscrepancyKind, ...]:
        return tuple(item.kind for item in self.discrepancies)

    def has(self, kind: DiscrepancyKind) -> bool:
        return kind in self.kinds

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "clean": self.clean,
            "unresolved": self.unresolved,
            "message": self.message,
            "checked_at": self.checked_at.isoformat(),
            "connection_id": self.connection_id,
            "provider_id": self.provider_id,
            "local_order_count": self.local_order_count,
            "remote_order_count": self.remote_order_count,
            "matched_order_count": self.matched_order_count,
            "discrepancy_count": len(self.discrepancies),
            "discrepancies": [item.to_dict() for item in self.discrepancies],
        }


class OrderReconciler:
    """Compares the :class:`OrderStore` against a provider snapshot."""

    def __init__(self, store: OrderStore) -> None:
        self._store = store

    def reconcile(self, snapshot: ProviderStateSnapshot, *, now: datetime | None = None,
                  positions: tuple[Position, ...] = ()) -> ReconciliationReport:
        checked_at = now or datetime.now(timezone.utc)
        local_orders = list(self._store.list())
        base: dict = {
            "checked_at": checked_at,
            "connection_id": snapshot.connection_id,
            "provider_id": snapshot.provider_id,
            "local_order_count": len(local_orders),
            "remote_order_count": len(snapshot.orders),
        }

        if snapshot.error is not None:
            return ReconciliationReport(
                status=ReconcileStatus.PROVIDER_ERROR,
                discrepancies=(),
                detail=f"provider snapshot failed: {snapshot.error}",
                **base)
        if not snapshot.complete:
            return ReconciliationReport(
                status=ReconcileStatus.INCOMPLETE,
                discrepancies=(),
                detail="provider snapshot was not complete; refusing to claim a match",
                **base)

        findings: list[ReconcileDiscrepancy] = []
        findings.extend(_duplicate_remote_orders(snapshot.orders))
        matched = self._compare_orders(local_orders, snapshot, findings)
        findings.extend(_compare_fills(local_orders, snapshot))
        findings.extend(_compare_positions(positions, snapshot))

        return ReconciliationReport(
            status=ReconcileStatus.DISCREPANCIES if findings else ReconcileStatus.MATCH,
            discrepancies=tuple(findings),
            matched_order_count=matched,
            **base)

    # -- orders -----------------------------------------------------------
    def _compare_orders(self, local_orders: list[Order], snapshot: ProviderStateSnapshot,
                        findings: list[ReconcileDiscrepancy]) -> int:
        remote_orders = list(snapshot.orders)
        by_client: dict[str, int] = {}
        by_provider: dict[str, int] = {}
        for index, remote in enumerate(remote_orders):
            if remote.client_order_id and remote.client_order_id not in by_client:
                by_client[remote.client_order_id] = index
            if remote.provider_order_id and remote.provider_order_id not in by_provider:
                by_provider[remote.provider_order_id] = index

        consumed: set[int] = set()
        matched = 0
        for order in local_orders:
            index = _match_local(order, by_client, by_provider)
            if index is None:
                # Only an order we actually handed to the provider and that
                # still needs to exist there can be missing: a locally
                # created but never submitted order is not expected remotely,
                # and a terminal one being absent costs nothing to act on.
                if not order.is_terminal and order.status is not OrderStatus.NEW:
                    findings.append(ReconcileDiscrepancy(
                        kind=DiscrepancyKind.LOCAL_ORDER_MISSING_REMOTELY,
                        message=("open local order is absent from the "
                                 "provider's order list"),
                        order_id=order.client_order_id,
                        symbol=order.symbol,
                        local_value=order.status.value,
                        remote_value=None))
                continue
            consumed.add(index)
            matched += 1
            findings.extend(_compare_order_pair(order, remote_orders[index]))

        for index, remote in enumerate(remote_orders):
            if index in consumed:
                continue
            findings.append(ReconcileDiscrepancy(
                kind=DiscrepancyKind.REMOTE_ORDER_UNKNOWN_LOCALLY,
                message="provider reports an order the local journal does not know",
                order_id=remote.client_order_id or remote.provider_order_id,
                symbol=remote.symbol,
                local_value=None,
                remote_value=remote.status.value))
        return matched


def _duplicate_remote_orders(remote_orders: list[RemoteOrder]) -> list[ReconcileDiscrepancy]:
    findings: list[ReconcileDiscrepancy] = []
    seen: dict[str, int] = {}
    for remote in remote_orders:
        if remote.provider_order_id:
            seen[remote.provider_order_id] = seen.get(remote.provider_order_id, 0) + 1
    for key, count in sorted(seen.items()):
        if count > 1:
            findings.append(ReconcileDiscrepancy(
                kind=DiscrepancyKind.DUPLICATE_PROVIDER_ORDER,
                message="provider reported the same order identifier more than once",
                order_id=key,
                local_value=None,
                remote_value=str(count)))
    return findings


def _match_local(order: Order, by_client: dict[str, int],
                 by_provider: dict[str, int]) -> int | None:
    index = by_client.get(order.client_order_id)
    if index is not None:
        return index
    if order.provider_order_id:
        index = by_provider.get(order.provider_order_id)
        if index is not None:
            return index
    return None


def _compare_order_pair(order: Order, remote: RemoteOrder) -> list[ReconcileDiscrepancy]:
    findings: list[ReconcileDiscrepancy] = []
    remote_status = _coerce_status(remote.status)

    if (order.is_terminal and remote_status is not None
            and remote_status is not OrderStatus.UNKNOWN and not remote.is_terminal):
        findings.append(ReconcileDiscrepancy(
            kind=DiscrepancyKind.TERMINAL_LOCAL_OPEN_REMOTELY,
            message="local order is terminal but the provider still shows it open",
            order_id=order.client_order_id,
            symbol=order.symbol,
            local_value=order.status.value,
            remote_value=remote_status.value))
    elif (order.status is not OrderStatus.UNKNOWN and remote_status is not None
          and order.status is not remote_status):
        findings.append(ReconcileDiscrepancy(
            kind=DiscrepancyKind.STATUS_MISMATCH,
            message="local and provider order statuses differ",
            order_id=order.client_order_id,
            symbol=order.symbol,
            local_value=order.status.value,
            remote_value=remote_status.value))

    local_filled = float(order.filled_quantity)
    remote_filled = float(remote.filled_quantity or 0.0)
    if abs(remote_filled - local_filled) > _QUANTITY_EPSILON:
        findings.append(ReconcileDiscrepancy(
            kind=DiscrepancyKind.QUANTITY_MISMATCH,
            message="local and provider executed quantities differ",
            order_id=order.client_order_id,
            symbol=order.symbol,
            local_value=str(local_filled),
            remote_value=str(remote_filled)))

    local_partial = order.status is OrderStatus.PARTIALLY_FILLED
    remote_partial = remote_status is OrderStatus.PARTIALLY_FILLED
    if local_partial != remote_partial and abs(remote_filled - local_filled) <= _QUANTITY_EPSILON:
        findings.append(ReconcileDiscrepancy(
            kind=DiscrepancyKind.PARTIAL_FILL_MISMATCH,
            message="the two sides disagree about whether the order is partially filled",
            order_id=order.client_order_id,
            symbol=order.symbol,
            local_value=order.status.value,
            remote_value=remote_status.value if remote_status else None))
    return findings


def _compare_fills(local_orders: list[Order],
                   snapshot: ProviderStateSnapshot) -> list[ReconcileDiscrepancy]:
    findings: list[ReconcileDiscrepancy] = []
    recorded: set[str] = set()
    for order in local_orders:
        for fill in order.fills:
            if fill.fill_id:
                recorded.add(fill.fill_id)
            if fill.provider_fill_id:
                recorded.add(fill.provider_fill_id)

    local_by_provider_id = {order.provider_order_id: order
                            for order in local_orders if order.provider_order_id}
    local_ids = {order.client_order_id for order in local_orders}

    seen: dict[str, int] = {}
    for remote in snapshot.fills:
        if remote.provider_fill_id:
            seen[remote.provider_fill_id] = seen.get(remote.provider_fill_id, 0) + 1

    duplicates_reported: set[str] = set()
    for key, count in sorted(seen.items()):
        if count > 1:
            duplicates_reported.add(key)
            findings.append(ReconcileDiscrepancy(
                kind=DiscrepancyKind.DUPLICATE_FILL,
                message="provider reported the same fill more than once",
                local_value=str(count),
                remote_value=key))

    for remote in snapshot.fills:
        owner = _fill_owner(remote, local_by_provider_id, local_ids)
        if owner is None:
            # The order itself is already flagged as unknown locally, so the
            # fill adds no new information.
            continue
        key = remote.provider_fill_id
        if key and key in recorded:
            continue
        if key and key in duplicates_reported:
            continue
        if not key and _fill_already_recorded(owner, remote, local_orders):
            continue
        findings.append(ReconcileDiscrepancy(
            kind=DiscrepancyKind.FILL_MISSING_LOCALLY,
            message="provider reports an execution the local journal has not recorded",
            order_id=owner.client_order_id,
            symbol=owner.symbol,
            local_value=None,
            remote_value=key or _fill_signature(remote)))

    for order in local_orders:
        keys = [fill.fill_id for fill in order.fills if fill.fill_id]
        counted: set[str] = set()
        for key in keys:
            if key in counted:
                continue
            counted.add(key)
            if keys.count(key) > 1:
                findings.append(ReconcileDiscrepancy(
                    kind=DiscrepancyKind.DUPLICATE_FILL,
                    message="the local journal recorded the same fill more than once",
                    order_id=order.client_order_id,
                    symbol=order.symbol,
                    local_value=key,
                    remote_value=None))
    return findings


def _fill_owner(remote: RemoteFill, local_by_provider_id: dict[str, Order],
                local_ids: set[str]) -> Order | None:
    if remote.provider_order_id and remote.provider_order_id in local_by_provider_id:
        return local_by_provider_id[remote.provider_order_id]
    return None


def _fill_already_recorded(owner: Order, remote: RemoteFill,
                           local_orders: list[Order]) -> bool:
    for fill in owner.fills:
        if (abs(float(fill.quantity) - float(remote.quantity)) <= _QUANTITY_EPSILON
                and abs(float(fill.price) - float(remote.price)) <= _QUANTITY_EPSILON):
            return True
    return False


def _fill_signature(remote: RemoteFill) -> str:
    return f"{remote.quantity}@{remote.price}"


def _compare_positions(local_positions: tuple[Position, ...],
                       snapshot: ProviderStateSnapshot) -> list[ReconcileDiscrepancy]:
    # A provider that returns no position book at all tells us nothing: paper
    # accounts in this platform keep holdings in the portfolio subsystem, not
    # in the adapter. Position drift is only reported once the provider
    # actually gives us a book to compare against - never from its absence.
    if not snapshot.positions:
        return []
    findings: list[ReconcileDiscrepancy] = []
    local_map = {position.symbol.strip().upper(): position
                 for position in local_positions
                 if abs(float(position.quantity)) > _QUANTITY_EPSILON}
    remote_map = {remote.symbol.strip().upper(): remote
                  for remote in snapshot.positions
                  if not remote.is_flat}

    for symbol, local in sorted(local_map.items()):
        remote = remote_map.get(symbol)
        if remote is None:
            findings.append(ReconcileDiscrepancy(
                kind=DiscrepancyKind.POSITION_MISSING_REMOTELY,
                message="local position is not reported by the provider",
                symbol=symbol,
                local_value=str(local.quantity),
                remote_value=None))
            continue
        if abs(float(remote.quantity) - float(local.quantity)) > _QUANTITY_EPSILON:
            findings.append(ReconcileDiscrepancy(
                kind=DiscrepancyKind.POSITION_QUANTITY_MISMATCH,
                message="local and provider position quantities differ",
                symbol=symbol,
                local_value=str(local.quantity),
                remote_value=str(remote.quantity)))
        if (local.direction is not None and remote.direction is not None
                and local.direction is not remote.direction):
            findings.append(ReconcileDiscrepancy(
                kind=DiscrepancyKind.POSITION_SIDE_MISMATCH,
                message="local and provider position sides differ",
                symbol=symbol,
                local_value=local.direction.value,
                remote_value=remote.direction.value))

    for symbol, remote in sorted(remote_map.items()):
        if symbol not in local_map:
            findings.append(ReconcileDiscrepancy(
                kind=DiscrepancyKind.POSITION_UNKNOWN_LOCALLY,
                message="provider reports a position the local journal does not hold",
                symbol=symbol,
                local_value=None,
                remote_value=str(remote.quantity)))
    return findings


def _coerce_status(value) -> OrderStatus | None:
    if isinstance(value, OrderStatus):
        return value
    if value is None:
        return None
    return normalize_order_status(value)
