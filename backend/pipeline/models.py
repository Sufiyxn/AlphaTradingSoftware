"""Canonical execution-intent and pipeline-outcome models.

An ``ExecutionIntent`` is the only shape a caller may use to ask for an order:
it carries the trade setup plus the signal context the pipeline needs to fail
closed (freshness, quantity cap, connection, mode, idempotency). A
``PipelineResult`` is the normalized answer: one deterministic outcome code,
the risk decision when one was made, the provider order when one was
submitted, and the connection that was used. Results never carry credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.execution.models import ExecutionMode
from backend.orders.models import Order
from backend.risk.models import RiskDecision
from backend.strategies.models import TradeSetup


class PipelineCode:
    """Deterministic outcome codes for the canonical execution pipeline.

    ``ACCEPTED`` means the provider filled the order (in whole or in part);
    ``SUBMITTED`` means the provider took the order but has not filled it yet,
    so nothing is booked. Every other code is a fail-closed rejection with a
    stable machine-readable meaning.
    """

    ACCEPTED = "ACCEPTED"
    SUBMITTED = "SUBMITTED"
    # input and signal context
    INVALID_REQUEST = "INVALID_REQUEST"
    STALE_SIGNAL = "STALE_SIGNAL"
    DUPLICATE_INTENT = "DUPLICATE_INTENT"
    # risk and safety
    RISK_REJECTED = "RISK_REJECTED"
    KILL_SWITCH = "KILL_SWITCH"
    SAFETY_REJECTED = "SAFETY_REJECTED"
    # market session (Phase 11): the calendar refused the submission
    SESSION_BLOCKED = "SESSION_BLOCKED"
    # connection selection and readiness
    CONNECTION_NOT_FOUND = "CONNECTION_NOT_FOUND"
    CONNECTION_NOT_READY = "CONNECTION_NOT_READY"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    UNSUPPORTED_MODE = "UNSUPPORTED_MODE"
    # provider answers
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_AUTH_FAILURE = "PROVIDER_AUTH_FAILURE"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_RATE_LIMIT = "PROVIDER_RATE_LIMIT"
    PROVIDER_EXECUTION_REJECTION = "PROVIDER_EXECUTION_REJECTION"
    # provider identity
    PROVIDER_ID_COLLISION = "PROVIDER_ID_COLLISION"


ALL_CODES: tuple[str, ...] = tuple(
    value for name, value in vars(PipelineCode).items()
    if not name.startswith("_") and isinstance(value, str))


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """A provider-agnostic request to execute one trade setup.

    ``requested_quantity`` only ever reduces risk sizing - it is applied after
    the risk decision and never widens it. ``max_age_seconds`` together with
    ``occurred_at`` enables the pipeline's signal-freshness check; both are
    optional so internally generated setups (scanner cycles) can opt in.
    ``idempotency_key`` makes a retry safe: the pipeline submits at most one
    order per key.
    """

    setup: TradeSetup
    source: str = "internal"
    signal_id: str | None = None
    occurred_at: datetime | None = None
    max_age_seconds: float | None = None
    requested_quantity: int | float | None = None
    limit_price: float | None = None
    connection_id: str | None = None
    mode: ExecutionMode = ExecutionMode.PAPER
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Normalized pipeline outcome; safe to serialize without redaction.

    ``order`` is the canonical order the platform owns - its internal id, its
    lifecycle status, and the fills behind it - not the provider's own record.
    It is ``None`` exactly when no order exists: never submitted, or the
    provider failed before returning one.
    """

    code: str
    reason: str | None = None
    risk: RiskDecision | None = None
    order: Order | None = None
    setup: TradeSetup | None = None
    limit_price: float | None = None
    connection_id: str | None = None
    provider_id: str | None = None
    mode: ExecutionMode | None = None
    source: str = "internal"
    signal_id: str | None = None
    safety_approved: bool = False
    safety_reason: str | None = None
    submitted: bool = False
    duplicate: bool = False
    at: datetime | None = None

    @property
    def accepted(self) -> bool:
        return self.code == PipelineCode.ACCEPTED
