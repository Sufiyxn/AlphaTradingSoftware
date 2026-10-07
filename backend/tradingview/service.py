"""TradingView webhook ingestion: authenticate, validate, normalize, execute.

Request path (fail closed at every step)::

    POST /api/webhooks/tradingview
        -> dedicated webhook authentication (HMAC header or shared secret)
        -> payload decode with a hard size limit
        -> field validation and normalization (backend.signals)
        -> deterministic duplicate / replay check
        -> canonical TradingPipeline: connection verification, RiskEngine,
           execution safety gate, selected adapter, normalized result

The endpoint never requires the owner token, never logs or returns the webhook
secret, never fabricates a price, and never writes the alert price into the
position's market price. TradingView is a signal source only: it is not a
broker, not a market-data provider, and not a price source.
"""
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from backend.pipeline import ExecutionIntent, PipelineCode
from backend.signals import (
    NormalizedSignal,
    SignalAction,
    SignalReplayStore,
    SignalValidationError,
    normalize_signal,
)

from .auth import TradingViewAuthenticator, WebhookAuthenticationError
from .payload import body_digest, explicit_id, extract_candidate, parse_json_body

logger = logging.getLogger(__name__)

SOURCE = "tradingview"
ACCEPTED_EVENT = "TV_SIGNAL_RECEIVED"
REJECTED_EVENT = "TV_SIGNAL_REJECTED"
DUPLICATE_EVENT = "TV_SIGNAL_DUPLICATE"


class WebhookError(Exception):
    """A terminal webhook outcome with a stable status and machine code."""

    def __init__(self, status: int, code: str, message: str, *,
                 outcome: str = "rejected", remember: bool = False,
                 reason: str | None = None, **details: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.outcome = outcome
        self.remember = remember
        self.reason = reason or code
        self.details = details


@dataclass(frozen=True, slots=True)
class WebhookResponse:
    status: int
    body: dict[str, Any]
    outcome: str
    signal_id: str | None = None
    symbol: str | None = None
    action: str | None = None


def _payload(status: int, state: str, code: str, message: str,
             **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"status": state, "code": code, "message": message}
    body.update(extra)
    return body


class TradingViewWebhookService:
    """Owns the whole webhook path for one application runtime."""

    def __init__(self, *, settings, paper, risk, portfolio, journal=None,
                 clock=None, allowed_symbols=(), max_candidate_price: float = 100.0):
        self.settings = settings
        self.paper = paper
        self.risk = risk
        self.portfolio = portfolio
        self.journal = journal
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.allowed_symbols = tuple(str(item).strip().upper() for item in allowed_symbols)
        self.max_candidate_price = float(max_candidate_price)
        self.authenticator = (TradingViewAuthenticator(settings.webhook_secret)
                              if settings.webhook_secret else None)
        self.replay = SignalReplayStore(journal, clock=self.clock)
        self._lock = threading.Lock()
        self._counters = {"accepted": 0, "rejected": 0, "duplicate": 0}
        self._last_accepted_at: datetime | None = None
        self._last_rejected_at: datetime | None = None
        self._last_rejection_code: str | None = None
        self._last_error_at: datetime | None = None
        self._last_error_code: str | None = None
        self._hydrate()

    # -- readiness ---------------------------------------------------------

    @property
    def ready(self) -> bool:
        return bool(self.settings.enabled and self.authenticator is not None)

    @property
    def max_body_bytes(self) -> int:
        return int(self.settings.max_body_bytes)

    @property
    def unavailable_reason(self) -> str | None:
        if not self.settings.enabled:
            return "the TradingView webhook is disabled; set TRADINGVIEW_ENABLED=true"
        if not self.settings.webhook_secret:
            return "no webhook secret is configured; set TRADINGVIEW_WEBHOOK_SECRET"
        return None

    # -- request handling --------------------------------------------------

    def handle(self, raw: bytes, headers: Mapping[str, Any] | None = None) -> WebhookResponse:
        """Process one webhook request; never raises, never leaks a secret."""
        try:
            response = self._handle(bytes(raw or b""), headers or {})
        except WebhookAuthenticationError as error:
            response = WebhookResponse(401, _payload(401, "rejected", "AUTH_FAILED",
                                                     error.message), "rejected")
        except SignalValidationError as error:
            response = WebhookResponse(400, _payload(400, "rejected", "INVALID_PAYLOAD",
                                                     error.message, reason=error.reason),
                                       "rejected")
        except WebhookError as error:
            response = WebhookResponse(
                error.status,
                _payload(error.status, error.outcome, error.code, error.message,
                         reason=error.reason, **error.details),
                error.outcome)
        except Exception:
            logger.exception("TradingView webhook failed with an internal error")
            response = WebhookResponse(500, _payload(500, "error", "INTERNAL_ERROR",
                                                     "the webhook failed to process the signal"),
                                       "error")
        self._record(response)
        return response

    def _handle(self, raw: bytes, headers: Mapping[str, Any]) -> WebhookResponse:
        if not self.ready:
            return WebhookResponse(
                503, _payload(503, "error", "NOT_CONFIGURED",
                              self.unavailable_reason or "the TradingView webhook is not configured"),
                "error")
        if len(raw) > self.max_body_bytes:
            return WebhookResponse(
                413, _payload(413, "error", "PAYLOAD_TOO_LARGE",
                              f"request body exceeds {self.max_body_bytes} bytes"),
                "error")

        signed = bool(self.authenticator.header_names(headers))
        if signed:
            self.authenticator.verify_header(raw, headers)
        payload = parse_json_body(raw)
        if not signed:
            self.authenticator.verify_payload(payload)

        candidate = extract_candidate(payload)
        supplied_id = explicit_id(candidate)
        digest = body_digest(raw)
        event_key = f"id:{supplied_id}" if supplied_id else None
        body_key = f"body:{digest}"
        keys = tuple(key for key in (event_key, body_key) if key)
        signal_id = event_key or f"sha256:{digest}"

        if "timestamp" not in candidate:
            raise SignalValidationError("missing required field: timestamp",
                                        reason="MISSING_FIELD")
        signal = normalize_signal(
            candidate, source=SOURCE, signal_id=signal_id, now=self.clock(),
            max_age_seconds=float(self.settings.max_signal_age_seconds),
            allowed_symbols=self.allowed_symbols or None,
            allowed_strategies=tuple(self.settings.allowed_strategies))

        with self._lock:
            prior = self.replay.record_for(keys)
            if prior is not None:
                return WebhookResponse(
                    409, _payload(409, "duplicate", "DUPLICATE_SIGNAL",
                                  "this signal was already processed; no duplicate order was created",
                                  signal_id=signal.signal_id, symbol=signal.symbol,
                                  action=signal.action.value,
                                  first_seen=prior.get("outcome")),
                    "duplicate", signal.signal_id, signal.symbol, signal.action.value)
            try:
                if signal.action is SignalAction.EXIT:
                    execution = self._execute_exit(signal)
                else:
                    execution = self._execute_entry(signal)
            except WebhookError as error:
                if error.remember:
                    self.replay.remember(keys, signal_id=signal.signal_id, source=SOURCE,
                                         outcome=error.code, symbol=signal.symbol,
                                         timestamp=self.clock())
                raise
            self.replay.remember(keys, signal_id=signal.signal_id, source=SOURCE,
                                 outcome="ACCEPTED", symbol=signal.symbol,
                                 timestamp=self.clock(),
                                 context={"action": signal.action.value})

        return WebhookResponse(
            202, _payload(202, "accepted", "ACCEPTED",
                          "signal accepted and processed through the risk and paper workflow",
                          signal_id=signal.signal_id, symbol=signal.symbol,
                          action=signal.action.value,
                          strategy=signal.strategy,
                          signal_timestamp=signal.timestamp.isoformat(),
                          **execution),
            "accepted", signal.signal_id, signal.symbol, signal.action.value)

    # -- trading workflows -------------------------------------------------

    def _execute_entry(self, signal: NormalizedSignal) -> dict[str, Any]:
        setup = signal.to_trade_setup()
        if setup.entry_price >= self.max_candidate_price:
            raise WebhookError(
                400, "INVALID_PAYLOAD",
                f"entry price {setup.entry_price:g} is not below the configured "
                f"maximum candidate price {self.max_candidate_price:g}",
                reason="PRICE_LIMIT_EXCEEDED")

        # One canonical path: the TradingPipeline validates the intent, checks
        # freshness, resolves and verifies the connection, evaluates risk
        # (reducing the size to the alert's quantity when the alert asks for
        # less), enforces the execution safety gate, and submits through the
        # selected adapter. This service only maps the outcome back onto the
        # webhook's stable HTTP contract.
        intent = ExecutionIntent(
            setup=setup, source=SOURCE, signal_id=signal.signal_id,
            occurred_at=signal.timestamp,
            max_age_seconds=float(self.settings.max_signal_age_seconds),
            requested_quantity=signal.quantity,
            idempotency_key=signal.signal_id)
        result = self.paper.submit_decision(intent)
        if result.code == PipelineCode.STALE_SIGNAL:
            raise WebhookError(
                400, "INVALID_PAYLOAD",
                result.reason or "the signal timestamp is too old",
                remember=True, reason="STALE_TIMESTAMP")
        if result.code == PipelineCode.INVALID_REQUEST:
            raise WebhookError(
                400, "INVALID_PAYLOAD",
                result.reason or "the execution request is invalid",
                remember=True, reason="INVALID_PAYLOAD")
        if result.risk is not None and not result.risk.approved:
            raise WebhookError(
                422, "RISK_REJECTED",
                "the signal was rejected by the risk engine",
                remember=True, reasons=list(result.risk.reasons))
        if not result.accepted or result.trade is None:
            order = result.order
            detail = f"{order.status.value}" if order is not None else (
                result.reason or "no order was placed")
            if order is not None and order.rejection_reason:
                detail = f"{detail}: {order.rejection_reason}"
            raise WebhookError(
                503, "EXECUTION_UNAVAILABLE",
                f"execution could not fill the signal ({detail})",
                outcome="error", order_status=(order.status.value if order else None),
                rejection_reason=(order.rejection_reason if order else None))

        trade = result.trade
        position = next((item for item in self.portfolio.positions
                         if item.symbol == trade.symbol), None)
        return {
            "order_id": trade.order_id,
            "direction": trade.direction.value,
            "quantity": trade.quantity,
            "entry_price": trade.entry_price,
            "stop_price": trade.stop_price,
            "target_price": trade.target_price,
            "risk_approved_quantity": result.risk.quantity,
            "position_price_state": (position.price_state.value if position else None),
            "position_market_price": (position.market_price if position else None),
        }

    def _execute_exit(self, signal: NormalizedSignal) -> dict[str, Any]:
        position = next((item for item in self.portfolio.positions
                         if item.symbol == signal.symbol), None)
        if position is None:
            raise WebhookError(422, "NO_OPEN_POSITION",
                               f"no open position exists for {signal.symbol}",
                               remember=True)
        if signal.quantity is not None and signal.quantity != position.quantity:
            raise WebhookError(
                422, "QUANTITY_MISMATCH",
                f"exit quantity {signal.quantity} does not match the open position "
                f"quantity {position.quantity}", remember=True,
                position_quantity=position.quantity)
        exit_price = float(signal.reported_price)
        try:
            realized = self.paper.close(signal.symbol, exit_price, timestamp=self.clock())
        except (KeyError, ValueError) as error:
            raise WebhookError(422, "INVALID_EXIT",
                               f"the position could not be closed: {error}") from error
        return {
            "closed": True,
            "exit_price": exit_price,
            "realized_pnl": realized,
            "quantity": position.quantity,
        }

    # -- journaling and counters ------------------------------------------

    def _record(self, response: WebhookResponse) -> None:
        now = self.clock()
        if response.outcome == "accepted":
            self._counters["accepted"] += 1
            self._last_accepted_at = now
            self._last_error_at = None
            self._last_error_code = None
            self._journal_signal_event(ACCEPTED_EVENT, self._accept_message(response),
                                symbol=response.symbol, context=_context(response))
        elif response.outcome == "duplicate":
            self._counters["duplicate"] += 1
            self._journal_signal_event(DUPLICATE_EVENT, "duplicate TradingView signal ignored",
                                symbol=response.symbol,
                                context={"signal_id": response.signal_id,
                                         "symbol": response.symbol,
                                         "action": response.action})
        elif response.outcome == "rejected":
            self._counters["rejected"] += 1
            self._last_rejected_at = now
            self._last_rejection_code = response.body.get("reason") or response.body.get("code")
            if response.body.get("code") != "NOT_CONFIGURED":
                self._journal_signal_event(
                    REJECTED_EVENT, "TradingView signal rejected",
                    symbol=response.symbol,
                    context={"code": response.body.get("code"),
                             "reason": self._last_rejection_code,
                             "message": response.body.get("message"),
                             "signal_id": response.signal_id,
                             "symbol": response.symbol, "action": response.action})
        elif response.status >= 500 and response.body.get("code") == "INTERNAL_ERROR":
            self._last_error_at = now
            self._last_error_code = "INTERNAL_ERROR"
            self._journal_error("the TradingView webhook failed with an internal error")

    @staticmethod
    def _accept_message(response: WebhookResponse) -> str:
        body = response.body
        if response.action == "EXIT":
            return (f"TradingView exit signal accepted for {response.symbol} at "
                    f"{body.get('exit_price')}")
        return (f"TradingView entry signal accepted for {response.symbol} "
                f"({body.get('quantity')} @ {body.get('entry_price')})")

    def _journal_signal_event(self, event_type: str, message: str, *,
                              symbol: str | None = None,
                              context: dict[str, Any] | None = None) -> None:
        if self.journal is None:
            return
        try:
            self.journal.record_event(self.clock(), event_type, message,
                                      symbol=symbol, context=context)
        except Exception:
            logger.exception("Unable to persist webhook event %s", event_type)

    def _journal_error(self, message: str) -> None:
        if self.journal is None:
            return
        try:
            self.journal.record_error(self.clock(), message)
        except Exception:
            logger.exception("Unable to persist webhook error")

    # -- connection centre reporting --------------------------------------

    def connection_state(self) -> dict[str, Any]:
        """Honest TradingView state; credentials alone are never CONNECTED."""
        ready = self.ready
        state = "NOT_CONFIGURED"
        reason = self.unavailable_reason
        if not ready:
            endpoint_status = "DISABLED" if not self.settings.enabled else "NOT_CONFIGURED"
        else:
            endpoint_status = "INSTALLED"
            if self._last_error_at is not None and (
                    self._last_accepted_at is None or self._last_error_at >= self._last_accepted_at):
                state = "ERROR"
                reason = "the most recent webhook delivery failed with an internal error"
            elif self._last_accepted_at is None:
                state = "UNAVAILABLE"
                reason = ("the endpoint is installed and listening, but no TradingView "
                          "signal has been authenticated yet")
            else:
                age = (self.clock() - self._last_accepted_at).total_seconds()
                stale_after = float(self.settings.stale_after_seconds)
                if age <= stale_after:
                    state = "CONNECTED"
                    reason = None
                else:
                    state = "STALE"
                    reason = (f"no TradingView signal has been accepted in the last "
                              f"{stale_after:g} seconds")
        return {
            "ready": ready,
            "state": state,
            "reason": reason,
            "endpoint_status": endpoint_status,
            "webhook_available": ready,
            "signals_accepted": self._counters["accepted"],
            "signals_rejected": self._counters["rejected"],
            "signals_duplicate": self._counters["duplicate"],
            "last_received_signal": (_iso(self._last_accepted_at)),
            "last_rejected_signal": (_iso(self._last_rejected_at)),
            "last_rejection_reason": self._last_rejection_code,
            "stale_after_seconds": float(self.settings.stale_after_seconds),
            "max_signal_age_seconds": float(self.settings.max_signal_age_seconds),
        }

    def self_check(self) -> tuple[bool, str]:
        """Local verification of the authentication path; makes no request."""
        if not self.ready:
            return False, self.unavailable_reason or "the TradingView webhook is not configured"
        probe = json.dumps({"probe": SOURCE}, sort_keys=True).encode("utf-8")
        try:
            self.authenticator.verify_header(
                probe, {"x-webhook-signature": f"sha256={self.authenticator.signature(probe)}"})
            self.authenticator.verify_payload({"secret": self.settings.webhook_secret})
            try:
                self.authenticator.verify_header(probe, {"x-webhook-signature": "sha256=" + "0" * 64})
            except WebhookAuthenticationError:
                pass
            else:
                return False, "webhook authentication self-check failed: an invalid signature was accepted"
        except WebhookAuthenticationError as error:
            return False, f"webhook authentication self-check failed: {error.message}"
        return True, ("the endpoint is installed and authenticates with the configured "
                      "shared secret; no external request was made")

    # -- hydration ---------------------------------------------------------

    def _hydrate(self) -> None:
        if self.journal is None:
            return
        try:
            events = self.journal.events(limit=None)
        except Exception:
            return
        accepted: list[datetime] = []
        rejected: list[datetime] = []
        for event in events:
            event_type = event.get("event_type")
            timestamp = event.get("timestamp")
            when = _parse(timestamp)
            if event_type == ACCEPTED_EVENT:
                self._counters["accepted"] += 1
                if when:
                    accepted.append(when)
            elif event_type == REJECTED_EVENT:
                self._counters["rejected"] += 1
                if when:
                    rejected.append(when)
                context = event.get("context_json") or {}
                if isinstance(context, Mapping) and self._last_rejection_code is None:
                    self._last_rejection_code = context.get("reason") or context.get("code")
            elif event_type == DUPLICATE_EVENT:
                self._counters["duplicate"] += 1
        if accepted:
            self._last_accepted_at = max(accepted)
        if rejected:
            self._last_rejected_at = max(rejected)


def _context(response: WebhookResponse) -> dict[str, Any]:
    """Whitelisted, secret-free context recorded for an accepted signal."""
    body = response.body
    keep = ("signal_id", "symbol", "action", "strategy", "signal_timestamp", "order_id",
            "direction", "quantity", "entry_price", "stop_price", "target_price",
            "exit_price", "realized_pnl", "closed", "position_price_state")
    context = {key: body[key] for key in keep if key in body}
    context["status"] = body.get("status")
    context["code"] = body.get("code")
    return context


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None
