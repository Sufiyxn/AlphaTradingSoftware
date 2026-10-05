from datetime import datetime
from typing import Protocol, Sequence

from .models import Bar, Quote


class MarketDataProviderError(Exception):
    """Raised when a provider cannot retrieve requested market data."""


class MarketDataProvider(Protocol):
    """Read-only interface implemented by interchangeable data providers."""

    def get_historical(self, symbol: str, start: datetime, end: datetime) -> Sequence[Bar]:
        """Return bars in the half-open time range [start, end)."""
        ...

    def get_latest(self, symbols: Sequence[str]) -> Sequence[Quote]:
        """Return latest available quotes for requested symbols."""
        ...
