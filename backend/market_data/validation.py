import math
from datetime import datetime, timezone
from typing import Any, Mapping

from .models import Bar, Quote


class DataValidationError(ValueError):
    """Incoming provider data is missing or violates the normalized data rules."""


def _symbol(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataValidationError("symbol must be a non-empty string")
    return value.strip().upper()


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise DataValidationError("timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DataValidationError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataValidationError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0) or (not positive and result < 0):
        qualifier = "positive and finite" if positive else "non-negative and finite"
        raise DataValidationError(f"{field} must be {qualifier}")
    return result


def normalize_bar(value: Bar | Mapping[str, Any]) -> Bar:
    """Validate and normalize a bar from a provider into the common model."""
    try:
        fields = (
            {name: getattr(value, name) for name in ("symbol", "timestamp", "open", "high", "low", "close", "volume")}
            if isinstance(value, Bar)
            else value
        )
        if not isinstance(fields, Mapping):
            raise DataValidationError("bar must be a Bar or mapping")
        bar = Bar(
            symbol=_symbol(fields["symbol"]),
            timestamp=_timestamp(fields["timestamp"]),
            open=_number(fields["open"], "open", positive=True),
            high=_number(fields["high"], "high", positive=True),
            low=_number(fields["low"], "low", positive=True),
            close=_number(fields["close"], "close", positive=True),
            volume=_number(fields["volume"], "volume"),
        )
    except KeyError as error:
        raise DataValidationError(f"missing bar field: {error.args[0]}") from error
    if bar.high < max(bar.open, bar.close, bar.low):
        raise DataValidationError("high must be at least open, close, and low")
    if bar.low > min(bar.open, bar.close, bar.high):
        raise DataValidationError("low must be at most open, close, and high")
    return bar


def normalize_quote(value: Quote | Mapping[str, Any]) -> Quote:
    """Validate and normalize a latest quote from a provider."""
    try:
        fields = (
            {name: getattr(value, name) for name in ("symbol", "timestamp", "price", "volume")}
            if isinstance(value, Quote)
            else value
        )
        if not isinstance(fields, Mapping):
            raise DataValidationError("quote must be a Quote or mapping")
        return Quote(
            symbol=_symbol(fields["symbol"]),
            timestamp=_timestamp(fields["timestamp"]),
            price=_number(fields["price"], "price", positive=True),
            volume=_number(fields["volume"], "volume"),
        )
    except KeyError as error:
        raise DataValidationError(f"missing quote field: {error.args[0]}") from error
