"""The canonical TradingPipeline - the single path from intent to order.

Every production order travels this sequence, fail closed at each step::

    validate input -> signal freshness -> resolve connection (ConnectionManager)
    -> verify readiness/capabilities -> evaluate risk (RiskEngine)
    -> evaluate execution safety (ExecutionSafetyGate) -> validate execution mode
    -> submit through the verified adapter -> normalize status
    -> record journal -> normalized result

Nothing else in the product calls ``place_order``: TradingView, the
orchestrator, and the paper engine all hand an ``ExecutionIntent`` (or a
pre-approved ``RiskDecision``) to this pipeline. The pipeline never fabricates
a price, never re-implements risk rules (it asks ``RiskEngine``), never bypasses
the safety gate, and never submits without a verified connection.

Two lifecycle guarantees live here:

* the whole path - risk evaluation, order creation, submission, and the
  recorded outcome - runs under one re-entrant ``submission_lock``, so two
  intents racing each other can never both size against the same free
  capacity;
* the order the pipeline returns is the platform's canonical order (internal
  id, lifecycle status, fills), never the provider's own record of it.
"""
from __future__ import annotations

import logging
import math
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime

from backend.connections.errors import (
    ExecutionAuthenticationError,
    ExecutionConnectionError,
    ExecutionProviderError,
    ExecutionRateLimitError,
    ExecutionTimeoutError,
)
from backend.connections.manager import ConnectionManager
from backend.connections.models import ConnectionRecord, ReadinessResult
from backend.execution.models import (
    AssetType,
    ExecutionMode,
    OrderRequest,
    OrderStatus,
    OrderType,
    normalize_order_status,
)
from backend.orders import (
    Order,
    OrderIntent,
    OrderLifecycleService,
    OrderStore,
    ProviderIdentityCollision,
)
from backend.portfolio import Portfolio
from backend.portfolio.models import PortfolioSnapshot
from backend.risk import RiskEngine
from backend.risk.models import RiskCode, RiskDecision, RiskStatus
from backend.strategies.models import Direction, TradeSetup

from .models import ExecutionIntent, PipelineCode, PipelineResult

logger = logging.getLogger(__name__)

#: At most one order per idempotency key; the window trims the oldest keys.
MAX_IDEMPOTENCY_KEYS = 512

_FILL_STATES = (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
#: The provider has the order but has not executed it yet.
_OPEN_STATES = (OrderStatus.SUBMITTED, OrderStatus.PENDING, OrderStatus.ACCEPTED,
                OrderStatus.PARTIALLY_FILLED)


@dataclass(frozen=True, slots=True)
class _Request:
    """Validated, internal view of one execution request."""

    setup: TradeSetup
    source: str
    signal_id: str | None
    connection_id: str | None
    mode: ExecutionMode
    limit_price: float | None
    occurred_at: datetime | None
    max_age_seconds: float | None
    requested_quantity: int | float | None
    idempotency_key: str | None


class _ConnectionExecutor:
    """The production answer to the canceller's ``cancel_order`` protocol.

    ``OrderCanceller`` only knows a provider order id; this resolves which
    connection that order lives on (through the shared store), asks the
    ``ConnectionManager`` for that connection's verified adapter, and hands the
    cancellation over - so cancelling works on every connection the manager
    holds, not just the pipeline's default. An order whose connection has no
    verified adapter yet raises, which the canceller reports honestly as a
    ``PROVIDER_ERROR`` instead of a silent local close.

    A provider number is only unique for the instance that issued it, so the
    store is asked for *every* order carrying it. More than one connection
    answering means the number no longer identifies a single order, and that
    raises rather than cancelling whichever one the store yielded first.
    """

    def __init__(self, store: OrderStore, connections: ConnectionManager,
                 default_connection_id: str) -> None:
        self._store = store
        self._connections = connections
        self._default_connection_id = default_connection_id

    def cancel_order(self, provider_order_id: str):
        matches = self._store.find_all_by_provider_order_id(provider_order_id)
        connection_ids = {order.connection_id for order in matches}
        if len(connection_ids) > 1:
            raise RuntimeError(
                f"provider order identity {provider_order_id!r} is claimed by "
                f"more than one connection; refusing to cancel an order that "
                f"cannot be identified")
        order = matches[0] if matches else None
        connection_id = ((order.connection_id if order is not None else None)
                         or self._default_connection_id)
        adapter = self._connections.adapter_for(connection_id)
        if adapter is None:
            raise RuntimeError(
                f"connection {connection_id} has no verified execution adapter")
        return adapter.cancel_order(provider_order_id)


class TradingPipeline:
    """Evaluates risk, safety, and readiness, then submits through one adapter."""

    def __init__(self, *, connections: ConnectionManager, risk_engine: RiskEngine,
                 portfolio: Portfolio, journal=None, clock,
                 connection_id: str = "paper") -> None:
        self._connections = connections
        self._risk_engine = risk_engine
        self._portfolio = portfolio
        self._clock = clock
        self._default_connection_id = str(connection_id or "paper")
        self._lock = threading.Lock()
        #: One re-entrant lock for the whole risk -> order -> submit -> record
        #: path. The paper engine re-enters it while booking the fill, so a
        #: concurrent intent can never size itself against capacity that the
        #: intent in front of it has already committed.
        self.submission_lock = threading.RLock()
        self._completed: dict[str, PipelineResult] = {}
        store = OrderStore()
        self.orders = OrderLifecycleService(
            store, journal=None, clock=clock, restore=False,
            executor=_ConnectionExecutor(store, self._connections,
                                         self._default_connection_id))
        self._journal = None
        self.journal = journal

    @property
    def connections(self) -> ConnectionManager:
        return self._connections

    @property
    def journal(self):
        """The audit journal shared by the pipeline and the order lifecycle."""
        return self._journal

    @journal.setter
    def journal(self, value) -> None:
        self._journal = value
        # A host (the paper engine, the orchestrator) may attach the journal
        # after construction; the order store is rebuilt the moment it lands.
        self.orders.attach_journal(value)

    @property
    def gate(self):
        """The execution safety gate shared with connection readiness."""
        return self._connections.gate

    @property
    def default_connection_id(self) -> str:
        return self._default_connection_id

    # ------------------------------------------------------------------ entry
    def execute(self, intent: ExecutionIntent) -> PipelineResult:
        """Run the full canonical path for a normalized execution intent."""
        if not isinstance(intent, ExecutionIntent):
            return self._outcome(
                self._fallback_request(),
                code=PipelineCode.INVALID_REQUEST,
                reason="an ExecutionIntent is required to execute a trade")
        request = _Request(
            setup=intent.setup,
            source=str(intent.source or "internal"),
            signal_id=intent.signal_id,
            connection_id=intent.connection_id,
            mode=intent.mode,
            limit_price=intent.limit_price,
            occurred_at=intent.occurred_at,
            max_age_seconds=intent.max_age_seconds,
            requested_quantity=intent.requested_quantity,
            idempotency_key=intent.idempotency_key)
        return self._run(request)

    def execute_decision(self, decision: RiskDecision, *, limit_price: float | None = None,
                         source: str = "internal", connection_id: str | None = None,
                         signal_id: str | None = None,
                         idempotency_key: str | None = None) -> PipelineResult:
        """Submit an already-approved risk decision.

        Risk is never re-evaluated and never weakened here: the decision must
        be APPROVED with a positive quantity, and the kill switch, safety gate,
        connection readiness, and execution mode are still enforced.
        """
        if not isinstance(decision, RiskDecision):
            return self._outcome(
                self._fallback_request(),
                code=PipelineCode.INVALID_REQUEST,
                reason="a RiskDecision is required to submit a decision")
        request = _Request(
            setup=decision.setup,
            source=str(source or "internal"),
            signal_id=signal_id,
            connection_id=connection_id,
            mode=ExecutionMode.PAPER,
            limit_price=limit_price,
            occurred_at=None,
            max_age_seconds=None,
            requested_quantity=None,
            idempotency_key=idempotency_key)
        return self._run(request, decision)

    # ------------------------------------------------------------------ steps
    def _run(self, request: _Request, decision: RiskDecision | None = None) -> PipelineResult:
        # The whole path runs under one re-entrant lock: risk sizing reads the
        # portfolio and the open-order reservations together, so no second
        # intent can size itself against capacity this one is about to take.
        with self.submission_lock:
            return self._run_locked(request, decision)

    def _run_locked(self, request: _Request, decision: RiskDecision | None) -> PipelineResult:
        key = request.idempotency_key
        if key is not None:
            with self._lock:
                cached = self._completed.get(key)
            if cached is not None:
                duplicate = replace(
                    cached, duplicate=True, code=PipelineCode.DUPLICATE_INTENT,
                    reason=("this idempotency key was already executed; "
                            "no new order was submitted"))
                self._record_event(
                    "EXECUTION_DUPLICATE",
                    "execution pipeline refused a duplicate idempotency key",
                    request=request, code=duplicate.code, reason=duplicate.reason,
                    order=duplicate.order)
                return duplicate
        result = self._authorize_and_submit(request, decision)
        if key is not None and result.order is not None:
            with self._lock:
                self._completed[key] = result
                while len(self._completed) > MAX_IDEMPOTENCY_KEYS:
                    self._completed.pop(next(iter(self._completed)))
        return result

    def _authorize_and_submit(self, request: _Request,
                              decision: RiskDecision | None) -> PipelineResult:
        # 1. validate input -------------------------------------------------
        invalid = self._validation_failure(request)
        if invalid is not None:
            code, reason = invalid
            return self._outcome(request, code=code, reason=reason)

        # 2. signal freshness ----------------------------------------------
        stale = self._freshness_failure(request)
        if stale is not None:
            return self._outcome(request, code=PipelineCode.STALE_SIGNAL, reason=stale)

        # 3. resolve the connection ----------------------------------------
        connection_id = request.connection_id or self._default_connection_id
        try:
            record = self._connections.get(connection_id)
        except KeyError:
            return self._outcome(
                request, code=PipelineCode.CONNECTION_NOT_FOUND,
                reason=f"no execution connection named {connection_id!r}")

        # 4. verify readiness and capabilities ------------------------------
        #    A gate block (kill switch) is not a connection fault: it falls
        #    through so risk is still evaluated, and the gate step rejects.
        gate_allowed, _ = self.gate.authorize(request.mode)
        readiness = self._connections.verify(connection_id)
        if not readiness.ready and gate_allowed:
            code, reason = self._connection_rejection(readiness)
            return self._outcome(request, code=code, reason=reason,
                                 connection=record)

        # 5. evaluate risk ---------------------------------------------------
        if decision is None:
            # Exactly one RiskEngine call per intent. The requested quantity
            # travels with the request so the approved quantity is
            # min(safe, requested) - a request is only ever reduced, never
            # widened - and the decision records both values for audit.
            decision = self._risk_engine.evaluate(
                request.setup, self._risk_snapshot(),
                requested_quantity=request.requested_quantity)
            decision = self._apply_quantity_cap(request, decision)
            self._record_risk(decision)
            if not decision.approved or decision.quantity <= 0:
                engaged = (self.gate.kill_switch_engaged
                           or bool(self._risk_engine.kill_switch_enabled))
                code = (PipelineCode.KILL_SWITCH if engaged
                        else PipelineCode.RISK_REJECTED)
                return self._outcome(
                    request, code=code, risk=decision, connection=record,
                    reason="; ".join(decision.reasons) or
                           "the risk engine rejected the decision")
        elif decision.status is not RiskStatus.APPROVED or decision.quantity <= 0:
            return self._outcome(
                request, code=PipelineCode.RISK_REJECTED, risk=decision,
                connection=record,
                reason="; ".join(decision.reasons) or
                       "the decision is not an approved risk decision")
        elif self._risk_engine.kill_switch_enabled:
            stopped = replace(
                decision, status=RiskStatus.REJECTED, quantity=0, risk_amount=0.0,
                reasons=decision.reasons + ("kill switch enabled",),
                code=RiskCode.KILL_SWITCH)
            self._record_risk(stopped)
            return self._outcome(
                request, code=PipelineCode.KILL_SWITCH, risk=stopped,
                connection=record, reason="the risk kill switch blocks execution")

        # 6. evaluate the execution safety gate ------------------------------
        allowed, safety_reason = self.gate.authorize(request.mode)
        if not allowed:
            code = (PipelineCode.KILL_SWITCH if self.gate.kill_switch_engaged
                    else PipelineCode.SAFETY_REJECTED)
            return self._outcome(request, code=code, reason=safety_reason,
                                 risk=decision, connection=record,
                                 safety_reason=safety_reason)

        # 7. validate execution mode and capabilities -------------------------
        mode_failure = self._mode_failure(request, record)
        if mode_failure is not None:
            code, reason = mode_failure
            return self._outcome(request, code=code, reason=reason, risk=decision,
                                 connection=record, safety_reason=safety_reason)
        adapter = self._connections.adapter_for(connection_id)
        if adapter is None:
            return self._outcome(
                request, code=PipelineCode.CONNECTION_NOT_READY,
                reason=f"connection {connection_id!r} has no verified adapter",
                risk=decision, connection=record, safety_reason=safety_reason)
        try:
            capabilities = adapter.capabilities()
            limitation = self._capability_limitation(capabilities, request, decision)
        except Exception as error:  # noqa: BLE001 - provider bugs surface honestly
            return self._outcome(
                request, code=PipelineCode.PROVIDER_UNAVAILABLE,
                reason=f"capability discovery failed: {self._redact(record, error)}",
                risk=decision, connection=record, safety_reason=safety_reason)
        if limitation is not None:
            return self._outcome(
                request, code=PipelineCode.CAPABILITY_UNAVAILABLE, reason=limitation,
                risk=decision, connection=record, safety_reason=safety_reason)

        # 8. submit through the verified adapter ------------------------------
        #    The canonical order is only registered once the provider has
        #    actually answered: a failed submission must never leave behind an
        #    order the platform cannot prove exists.
        intent = self._order_intent(request, decision, record, connection_id)
        submitted = self._submit(request, decision, adapter, record,
                                 intent.client_order_id)
        if isinstance(submitted, PipelineResult):
            self.orders.note_submission_failure(
                intent, submitted.reason or submitted.code, now=self._clock(),
                connection_id=record.connection_id, provider_id=record.provider_id)
            return replace(submitted, risk=decision,
                           connection_id=record.connection_id,
                           provider_id=record.provider_id)
        order = self._register_order(intent, submitted, record, adapter,
                                     request=request, decision=decision)
        if isinstance(order, PipelineResult):
            return order
        self._record_order(order)

        # 9. normalize the provider's status against the canonical order ------
        status = normalize_order_status(getattr(submitted, "status", None))
        filled = float(getattr(order, "filled_quantity", 0) or 0)

        # 10./11. journal the outcome and return the normalized result --------
        if status in _FILL_STATES and filled > 0:
            return self._outcome(
                request, code=PipelineCode.ACCEPTED, risk=decision, order=order,
                connection=record, safety_reason=safety_reason, submitted=True)
        if status in _OPEN_STATES:
            # The provider owns the order but has executed nothing yet: a real
            # outcome, not a rejection, and nothing is booked from it.
            return self._outcome(
                request, code=PipelineCode.SUBMITTED, risk=decision, order=order,
                connection=record, safety_reason=safety_reason, submitted=True,
                reason=(f"the provider accepted the order but has not filled it "
                        f"({status.value})"))
        reason = f"the provider did not fill the order ({status.value}"
        rejection = getattr(order, "rejection_reason", None)
        if rejection:
            reason = f"{reason}: {rejection})"
        else:
            reason = f"{reason})"
        return self._outcome(
            request, code=PipelineCode.PROVIDER_EXECUTION_REJECTION, risk=decision,
            order=order, connection=record, safety_reason=safety_reason,
            submitted=True, reason=reason)

    # ------------------------------------------------------------ step helpers
    def _risk_snapshot(self):
        """The portfolio snapshot plus the capacity open orders already commit.

        Reservations are attached here rather than inside ``RiskEngine`` so a
        snapshot handed straight to risk in a test stays exactly what the
        portfolio produced; only the canonical pipeline carries open orders
        into a risk evaluation.
        """
        snapshot = self._portfolio.snapshot()
        reservations = self.orders.pending_reservations()
        if reservations and isinstance(snapshot, PortfolioSnapshot):
            return replace(snapshot, pending_reservations=reservations)
        return snapshot

    def _order_intent(self, request: _Request, decision: RiskDecision,
                      record: ConnectionRecord, connection_id: str) -> OrderIntent:
        """Build the platform's request. The id is ours, never a symbol/timestamp."""
        risk_code = getattr(decision, "code", None)
        if isinstance(risk_code, RiskCode):
            risk_code = risk_code.value
        elif not isinstance(risk_code, str):
            risk_code = None
        return OrderIntent(
            client_order_id=uuid.uuid4().hex,
            symbol=str(request.setup.symbol).strip().upper(),
            direction=request.setup.direction,
            quantity=float(decision.quantity),
            order_type=OrderType.LIMIT,
            limit_price=request.limit_price,
            time_in_force="DAY",
            execution_mode=request.mode,
            connection_id=connection_id,
            provider_id=record.provider_id,
            source=request.source,
            signal_id=request.signal_id,
            risk_code=risk_code,
        )

    def _register_order(self, intent: OrderIntent, provider_order, record: ConnectionRecord,
                        adapter, *, request: _Request,
                        decision: RiskDecision) -> object:
        """Record the canonical order the provider just acknowledged.

        The identity the provider answered with is adopted only if no other
        local order already owns it. A collision is its own fail-closed
        outcome: the order that owns the identity keeps it, the order that
        was just submitted is closed as FAILED with a reason a human can act
        on, and neither identity is rewritten to make the numbers fit.
        """
        now = self._clock()
        provider_instance_id = (getattr(adapter, "provider_instance_id", None)
                                or record.provider_instance_id)
        try:
            order = self.orders.create(intent, now=now,
                                       connection_id=record.connection_id,
                                       provider_id=record.provider_id,
                                       provider_instance_id=provider_instance_id)
            order = self.orders.mark_submitted(
                order.client_order_id, getattr(provider_order, "order_id", None),
                now=now, provider_instance_id=provider_instance_id)
            order = self.orders.apply_provider_order(
                order.client_order_id, provider_order, now=now)
        except ProviderIdentityCollision as collision:
            logger.error("Refusing a colliding provider order identity: %s", collision)
            return self._outcome(
                request, code=PipelineCode.PROVIDER_ID_COLLISION,
                reason=str(collision), risk=decision, connection=record,
                order=self._close_collided_order(collision.order_id,
                                                 str(collision), now),
                submitted=True)
        except Exception:  # noqa: BLE001 - a store bug must not hide the provider's answer
            logger.exception("Unable to record the canonical order lifecycle")
            return provider_order
        reported = float(getattr(provider_order, "filled_quantity", 0) or 0)
        if reported > float(order.filled_quantity) + 1e-9:
            self.orders.note_fill_gap(order.client_order_id, reported, now=now)
        return order

    def _close_collided_order(self, order_id: str, reason: str, now: datetime) -> Order | None:
        """Close an order that could not be registered, without losing it."""
        try:
            return self.orders.apply_status(
                order_id, OrderStatus.FAILED, now, rejection_reason=reason)
        except Exception:  # noqa: BLE001 - the collision itself must stay visible
            logger.exception("Unable to close the collided order %s", order_id)
            return self.orders.get(order_id)

    def _validation_failure(self, request: _Request) -> tuple[str, str] | None:
        setup = request.setup
        if not isinstance(setup, TradeSetup):
            return (PipelineCode.INVALID_REQUEST,
                    "the execution request carries no trade setup")
        if not isinstance(setup.symbol, str) or not setup.symbol.strip():
            return (PipelineCode.INVALID_REQUEST, "symbol must be a non-empty string")
        if not isinstance(request.mode, ExecutionMode):
            return (PipelineCode.UNSUPPORTED_MODE,
                    f"unsupported execution mode {request.mode!r}")
        if request.limit_price is not None and not self._positive_number(request.limit_price):
            return (PipelineCode.INVALID_REQUEST,
                    "the order limit price must be a positive finite number")
        if request.requested_quantity is not None and not self._positive_number(request.requested_quantity):
            return (PipelineCode.INVALID_REQUEST,
                    "the requested quantity must be a positive finite number")
        if request.max_age_seconds is not None:
            if not self._positive_number(request.max_age_seconds):
                return (PipelineCode.INVALID_REQUEST,
                        "the maximum signal age must be a positive finite number")
            if request.occurred_at is None:
                return (PipelineCode.INVALID_REQUEST,
                        "freshness was required without a signal timestamp")
        if request.occurred_at is not None and not isinstance(request.occurred_at, datetime):
            return (PipelineCode.INVALID_REQUEST,
                    "the signal timestamp must be a date/time value")
        return None

    def _freshness_failure(self, request: _Request) -> str | None:
        if request.occurred_at is None or request.max_age_seconds is None:
            return None
        age = (self._clock() - request.occurred_at).total_seconds()
        if age > float(request.max_age_seconds):
            return (f"the signal is {age:g} seconds old; the configured maximum "
                    f"signal age is {float(request.max_age_seconds):g} seconds")
        return None

    @staticmethod
    def _positive_number(value: object) -> bool:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) and float(value) > 0)

    @staticmethod
    def _connection_rejection(readiness: ReadinessResult) -> tuple[str, str]:
        reason = readiness.failure_reason or "the execution connection is not ready"
        state = str(readiness.state or "")
        if state == "ERROR":
            if "authentication failed" in reason:
                return PipelineCode.PROVIDER_AUTH_FAILURE, reason
            if "rate limit" in reason:
                return PipelineCode.PROVIDER_RATE_LIMIT, reason
            return PipelineCode.PROVIDER_UNAVAILABLE, reason
        if state == "DEGRADED":
            code = (PipelineCode.PROVIDER_RATE_LIMIT if "rate limit" in reason
                    else PipelineCode.PROVIDER_UNAVAILABLE)
            return code, reason
        if state == "UNAVAILABLE":
            return PipelineCode.PROVIDER_UNAVAILABLE, reason
        if "rate limit" in reason:
            return PipelineCode.PROVIDER_RATE_LIMIT, reason
        if "permission" in reason or "does not support" in reason:
            return PipelineCode.CAPABILITY_UNAVAILABLE, reason
        return PipelineCode.CONNECTION_NOT_READY, reason

    @staticmethod
    def _apply_quantity_cap(request: _Request, decision: RiskDecision) -> RiskDecision:
        """The caller may ask for a smaller size; risk is never widened."""
        if (request.requested_quantity and decision.approved
                and decision.quantity > request.requested_quantity):
            return replace(
                decision, quantity=request.requested_quantity,
                risk_amount=abs(request.setup.entry_price - request.setup.stop_price)
                * request.requested_quantity)
        return decision

    @staticmethod
    def _mode_failure(request: _Request, record: ConnectionRecord) -> tuple[str, str] | None:
        if request.mode is not record.mode:
            return (PipelineCode.UNSUPPORTED_MODE,
                    f"connection {record.connection_id!r} executes "
                    f"{record.mode.value} only; {request.mode.value} was requested")
        return None

    @staticmethod
    def _capability_limitation(capabilities, request: _Request,
                               decision: RiskDecision) -> str | None:
        supported = (capabilities.paper_trading if request.mode is ExecutionMode.PAPER
                     else capabilities.live_trading)
        if not supported:
            return f"the execution provider does not support {request.mode.value.lower()} trading"
        if request.limit_price is not None and OrderType.LIMIT not in capabilities.order_types:
            return "the execution provider does not support limit orders"
        if request.setup.direction is Direction.SHORT and not capabilities.short_selling:
            return "the execution provider does not support short selling"
        if AssetType.STOCK not in capabilities.asset_types:
            return "the execution provider does not support stock orders"
        if not float(decision.quantity).is_integer() and not capabilities.fractional_shares:
            return "the execution provider does not support fractional shares"
        return None

    def _submit(self, request: _Request, decision: RiskDecision, adapter,
                record: ConnectionRecord, client_order_id: str) -> object:
        """Hand the order to the adapter; a provider failure maps to a result.

        The platform's own ``client_order_id`` travels with the request: a
        provider that echoes it lets reconciliation tie its record to the
        order we own without guessing, and one that ignores it loses nothing.
        """
        setup = request.setup
        order_request = OrderRequest(
            symbol=str(setup.symbol).strip().upper(),
            direction=setup.direction,
            quantity=decision.quantity,
            limit_price=request.limit_price,
            order_type=OrderType.LIMIT,
            execution_mode=request.mode,
            client_order_id=client_order_id,
        )
        try:
            return adapter.place_order(order_request)
        except ExecutionAuthenticationError as exc:
            code, failure = PipelineCode.PROVIDER_AUTH_FAILURE, exc
        except ExecutionTimeoutError as exc:
            code, failure = PipelineCode.PROVIDER_TIMEOUT, exc
        except ExecutionRateLimitError as exc:
            code, failure = PipelineCode.PROVIDER_RATE_LIMIT, exc
        except (ExecutionConnectionError, ExecutionProviderError) as exc:
            code, failure = PipelineCode.PROVIDER_UNAVAILABLE, exc
        except Exception as exc:  # noqa: BLE001 - provider bugs surface honestly
            code, failure = PipelineCode.PROVIDER_UNAVAILABLE, exc
        return self._outcome(
            request, code=code,
            reason=f"the execution provider failed: {self._redact(record, failure)}",
            connection=record)

    # ---------------------------------------------------------------- outcomes
    def _outcome(self, request: _Request, *, code: str, reason: str | None = None,
                 risk: RiskDecision | None = None, order=None,
                 connection: ConnectionRecord | None = None,
                 safety_reason: str | None = None,
                 submitted: bool = False) -> PipelineResult:
        gate_allowed, gate_reason = self.gate.authorize(request.mode)
        result = PipelineResult(
            code=code,
            reason=reason,
            risk=risk,
            order=order,
            setup=request.setup if isinstance(request.setup, TradeSetup) else None,
            limit_price=request.limit_price,
            connection_id=(connection.connection_id if connection is not None
                           else (request.connection_id or self._default_connection_id)),
            provider_id=connection.provider_id if connection is not None else None,
            mode=request.mode,
            source=request.source,
            signal_id=request.signal_id,
            safety_approved=bool(gate_allowed),
            safety_reason=safety_reason if safety_reason is not None else gate_reason,
            submitted=submitted,
            at=self._clock(),
        )
        if code in (PipelineCode.ACCEPTED, PipelineCode.SUBMITTED):
            symbol = request.setup.symbol if isinstance(request.setup, TradeSetup) else "the request"
            self._record_event(
                "EXECUTION_SUBMITTED",
                f"execution pipeline submitted {symbol} via {result.provider_id}",
                request=request, code=code, reason=None, order=order, risk=risk)
        else:
            symbol = request.setup.symbol if isinstance(request.setup, TradeSetup) else None
            self._record_event(
                "EXECUTION_REJECTED",
                f"execution pipeline rejected {symbol or 'the request'}: {code}",
                request=request, code=code, reason=reason, order=order, risk=risk)
        return result

    # ---------------------------------------------------------------- journal
    def _record_risk(self, decision: RiskDecision) -> None:
        self._journal_call("record_risk_decision", decision)

    def _record_order(self, order) -> None:
        self._journal_call("record_order", order)

    def _record_event(self, event_type: str, message: str, *, request: _Request,
                      code: str, reason: str | None, order=None,
                      risk: RiskDecision | None = None) -> None:
        context: dict = {
            "code": code,
            "source": request.source,
            "connection_id": (request.connection_id or self._default_connection_id),
            "mode": (request.mode.value if isinstance(request.mode, ExecutionMode)
                     else str(request.mode)),
        }
        if request.signal_id is not None:
            context["signal_id"] = request.signal_id
        if reason:
            context["reason"] = reason
        if order is not None:
            context["order_id"] = getattr(order, "order_id", None)
            context["order_status"] = str(getattr(order, "status", "") or "")
        if risk is not None:
            # Auditability: which risk rule decided, what was asked for, what
            # was approved - never a secret and never a fabricated value.
            context["risk_code"] = risk.code
            context["risk_approved"] = bool(risk.approved)
            context["risk_requested_quantity"] = risk.requested_quantity
            context["risk_approved_quantity"] = risk.quantity
            if not risk.approved and risk.reasons:
                context["risk_reasons"] = list(risk.reasons)
        symbol = request.setup.symbol if isinstance(request.setup, TradeSetup) else None
        self._journal_call("record_event", self._clock(), event_type, message,
                           symbol=symbol, context=context)

    def _journal_call(self, method: str, *args, **kwargs) -> None:
        if self.journal is None:
            return
        try:
            getattr(self.journal, method)(*args, **kwargs)
        except Exception:  # noqa: BLE001 - journaling never blocks execution
            logger.exception("Unable to persist pipeline journal entry using %s", method)

    @staticmethod
    def _redact(record: ConnectionRecord | None, error: object) -> str:
        text = str(error)
        handle = record.credentials if record is not None else None
        return handle.redact(text) if handle is not None else text

    def _fallback_request(self) -> _Request:
        return _Request(setup=None, source="internal", signal_id=None,
                        connection_id=None, mode=ExecutionMode.PAPER,
                        limit_price=None, occurred_at=None, max_age_seconds=None,
                        requested_quantity=None, idempotency_key=None)
