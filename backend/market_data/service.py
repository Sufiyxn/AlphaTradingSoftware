from datetime import datetime
from typing import Sequence

from .models import Bar, Quote
from .provider import MarketDataProvider, MarketDataProviderError
from .validation import DataValidationError, normalize_bar, normalize_quote


class MarketDataService:
    """Validate provider output before exposing normalized market data to consumers."""

    def __init__(self, provider: MarketDataProvider):
        self._provider = provider

    @property
    def configured(self) -> bool:
        return bool(getattr(self._provider, "is_configured", True))

    @property
    def provider_id(self) -> str:
        return str(getattr(self._provider, "provider", "unknown")).strip().lower()

    @property
    def status(self) -> dict:
        """Honest provider state; NOT_CONFIGURED when nothing real is connected."""
        if self.configured:
            return {"state": "CONFIGURED", "provider": self.provider_id, "reason": None}
        return {
            "state": "NOT_CONFIGURED",
            "provider": self.provider_id,
            "reason": str(getattr(self._provider, "reason", None) or "no market data provider is connected"),
        }

    def get_historical(self, symbol: str, start: datetime, end: datetime) -> tuple[Bar, ...]:
        if start.tzinfo is None or start.utcoffset() is None or end.tzinfo is None or end.utcoffset() is None:
            raise DataValidationError("start and end must include a timezone")
        if start >= end:
            raise DataValidationError("start must be earlier than end")
        requested_symbol = symbol.strip().upper() if isinstance(symbol, str) else ""
        if not requested_symbol:
            raise DataValidationError("symbol must be a non-empty string")
        try:
            bars = tuple(normalize_bar(bar) for bar in self._provider.get_historical(requested_symbol, start, end))
        except DataValidationError:
            raise
        except Exception as error:
            raise MarketDataProviderError(f"historical data request failed for {requested_symbol}") from error
        previous: datetime | None = None
        for bar in bars:
            if bar.symbol != requested_symbol:
                raise DataValidationError(f"provider returned unexpected symbol {bar.symbol}")
            if not start.astimezone(bar.timestamp.tzinfo) <= bar.timestamp < end.astimezone(bar.timestamp.tzinfo):
                raise DataValidationError("provider returned a bar outside the requested time range")
            if previous is not None and bar.timestamp <= previous:
                raise DataValidationError("historical bars must be strictly chronological")
            previous = bar.timestamp
        return bars

    def get_latest(self, symbols: Sequence[str]) -> tuple[Quote, ...]:
        requested = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols if isinstance(symbol, str) and symbol.strip()))
        if not requested:
            raise DataValidationError("at least one non-empty symbol is required")
        try:
            quotes = tuple(normalize_quote(quote) for quote in self._provider.get_latest(requested))
        except DataValidationError:
            raise
        except Exception as error:
            raise MarketDataProviderError("latest data request failed") from error
        returned: set[str] = set()
        for quote in quotes:
            if quote.symbol not in requested:
                raise DataValidationError(f"provider returned unexpected symbol {quote.symbol}")
            if quote.symbol in returned:
                raise DataValidationError(f"provider returned duplicate quote for {quote.symbol}")
            returned.add(quote.symbol)
        return quotes
