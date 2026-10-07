"""Validation of a canonical signal candidate into a ``NormalizedSignal``.

The candidate mapping is already provider-agnostic (produced by the provider
adapter). This module owns the shared rules: required fields, symbol shape and
membership, action, quantity, price levels, and timestamp freshness.
"""
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .models import NormalizedSignal, SignalAction, SignalValidationError

_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")
_MAX_QUANTITY = 1_000_000
_MAX_TEXT_LENGTH = 64

_KNOWN_ACTIONS = {action.value for action in SignalAction}


def parse_timestamp(value: Any, *, now: datetime, max_age_seconds: float,
                    future_tolerance_seconds: float = 60.0) -> datetime:
    """Parse a signal timestamp and reject invalid, stale, or future values.

    ISO-8601 strings (including a trailing ``Z``) and UNIX epoch seconds are
    accepted. A timestamp without an explicit offset is interpreted as UTC and
    documented as such; an unparseable value is rejected instead of guessed.
    """
    if isinstance(value, bool):
        raise SignalValidationError("timestamp must be a date/time value",
                                    reason="INVALID_TIMESTAMP")
    parsed: datetime
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise SignalValidationError("timestamp must be a finite epoch value",
                                        reason="INVALID_TIMESTAMP")
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise SignalValidationError("timestamp must not be empty",
                                        reason="INVALID_TIMESTAMP")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise SignalValidationError(
                "timestamp must be ISO-8601 (for example 2024-01-02T15:30:00Z) "
                "or a UNIX epoch value", reason="INVALID_TIMESTAMP") from None
    else:
        raise SignalValidationError("timestamp must be a date/time value",
                                    reason="INVALID_TIMESTAMP")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    if parsed > now_utc + timedelta(seconds=future_tolerance_seconds):
        raise SignalValidationError("timestamp is in the future",
                                    reason="INVALID_TIMESTAMP")
    if parsed < now_utc - timedelta(seconds=max_age_seconds):
        raise SignalValidationError(
            f"timestamp is older than the configured maximum signal age of "
            f"{max_age_seconds:g} seconds", reason="STALE_TIMESTAMP")
    return parsed


def _require_text(candidate: Mapping[str, Any], field: str) -> str:
    value = candidate.get(field)
    if value is None:
        raise SignalValidationError(f"missing required field: {field}",
                                    reason="MISSING_FIELD")
    if not isinstance(value, str):
        raise SignalValidationError(f"{field} must be a string",
                                    reason="INVALID_PAYLOAD")
    text = value.strip()
    if not text:
        raise SignalValidationError(f"{field} must not be empty",
                                    reason="INVALID_PAYLOAD")
    if len(text) > _MAX_TEXT_LENGTH:
        raise SignalValidationError(f"{field} is too long",
                                    reason="INVALID_PAYLOAD")
    return text


def _optional_number(candidate: Mapping[str, Any], field: str, *, positive: bool = True) -> float | None:
    value = candidate.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SignalValidationError(f"{field} must be a number",
                                    reason="INVALID_PAYLOAD")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = "a positive finite number" if positive else "a finite number"
        raise SignalValidationError(f"{field} must be {qualifier}",
                                    reason="INVALID_PAYLOAD")
    return number


def _optional_quantity(candidate: Mapping[str, Any]) -> int | None:
    value = candidate.get("quantity")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SignalValidationError("quantity must be a whole number",
                                    reason="INVALID_QUANTITY")
    if float(value) != int(value):
        raise SignalValidationError("quantity must be a whole number of shares",
                                    reason="INVALID_QUANTITY")
    quantity = int(value)
    if quantity <= 0 or quantity > _MAX_QUANTITY:
        raise SignalValidationError(
            f"quantity must be between 1 and {_MAX_QUANTITY}",
            reason="INVALID_QUANTITY")
    return quantity


def _metadata(value: Any) -> tuple[tuple[str, str], ...]:
    """Keep provider metadata as immutable, stringly-typed pairs."""
    if value is None:
        return ()
    if isinstance(value, Mapping):
        items = value.items()
    else:
        try:
            items = list(value)
        except TypeError:
            raise SignalValidationError("metadata must be an object",
                                        reason="INVALID_PAYLOAD") from None
    pairs: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise SignalValidationError("metadata must be an object",
                                        reason="INVALID_PAYLOAD")
        key, val = item
        if not isinstance(key, str) or not key or len(key) > _MAX_TEXT_LENGTH:
            raise SignalValidationError("metadata keys must be short non-empty strings",
                                        reason="INVALID_PAYLOAD")
        if val is None:
            continue
        if not isinstance(val, (str, int, float, bool)):
            raise SignalValidationError("metadata values must be scalars",
                                        reason="INVALID_PAYLOAD")
        text = str(val)
        if len(text) > 256:
            raise SignalValidationError("metadata values are too long",
                                        reason="INVALID_PAYLOAD")
        pairs.append((key, text))
    return tuple(pairs[:16])


def normalize_signal(candidate: Mapping[str, Any], *, source: str, signal_id: str,
                     now: datetime, max_age_seconds: float,
                     allowed_symbols: Sequence[str] | None = None,
                     allowed_strategies: Sequence[str] = (),
                     future_tolerance_seconds: float = 60.0) -> NormalizedSignal:
    """Validate a canonical candidate and return the normalized signal."""
    if not isinstance(candidate, Mapping):
        raise SignalValidationError("signal must be an object", reason="INVALID_PAYLOAD")

    symbol = _require_text(candidate, "symbol").upper()
    if not _SYMBOL_PATTERN.match(symbol):
        raise SignalValidationError(
            "symbol must be 1-12 characters of A-Z, 0-9, '.', or '-'",
            reason="INVALID_SYMBOL")
    if allowed_symbols is not None:
        permitted = {item.strip().upper() for item in allowed_symbols}
        if symbol not in permitted:
            raise SignalValidationError(
                f"{symbol} is not in the configured trading universe",
                reason="UNSUPPORTED_SYMBOL")

    action_text = _require_text(candidate, "action").upper()
    if action_text not in _KNOWN_ACTIONS:
        raise SignalValidationError(
            "action must be ENTER_LONG, ENTER_SHORT, or EXIT",
            reason="UNSUPPORTED_ACTION")
    action = SignalAction(action_text)

    timestamp = parse_timestamp(candidate.get("timestamp"), now=now,
                                max_age_seconds=max_age_seconds,
                                future_tolerance_seconds=future_tolerance_seconds)

    strategy: str | None = None
    raw_strategy = candidate.get("strategy")
    if raw_strategy is not None:
        strategy = _require_text(candidate, "strategy").lower()
        if strategy != strategy.strip():
            raise SignalValidationError("strategy must not contain padding",
                                        reason="INVALID_PAYLOAD")
    permitted_strategies = tuple(item.strip().lower() for item in allowed_strategies if item.strip())
    if permitted_strategies:
        if strategy is None:
            raise SignalValidationError(
                "strategy metadata is required by TRADINGVIEW_ALLOWED_STRATEGIES",
                reason="UNSUPPORTED_STRATEGY")
        if strategy not in permitted_strategies:
            raise SignalValidationError(
                f"strategy '{strategy}' is not in the allowed strategy list",
                reason="UNSUPPORTED_STRATEGY")

    quantity = _optional_quantity(candidate)
    entry_price = _optional_number(candidate, "entry_price")
    stop_price = _optional_number(candidate, "stop_price")
    target_price = _optional_number(candidate, "target_price")
    reported_price = _optional_number(candidate, "reported_price")

    if action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
        for field, value in (("entry_price", entry_price), ("stop_price", stop_price),
                             ("target_price", target_price)):
            if value is None:
                raise SignalValidationError(f"missing required field: {field}",
                                            reason="MISSING_FIELD")
        if reported_price is None:
            reported_price = entry_price
    else:
        if reported_price is None:
            raise SignalValidationError(
                "an exit signal requires an explicit reported price",
                reason="MISSING_FIELD")

    metadata_items = _metadata(candidate.get("metadata"))

    return NormalizedSignal(
        source=source,
        signal_id=signal_id,
        symbol=symbol,
        action=action,
        timestamp=timestamp,
        quantity=quantity,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        reported_price=reported_price,
        strategy=strategy,
        metadata=metadata_items,
    )
