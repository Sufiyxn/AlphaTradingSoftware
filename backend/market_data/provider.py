from datetime import datetime
from typing import Protocol, Sequence

from .models import Bar, Quote


class MarketDataProviderError(Exception):
    """Raised when a provider cannot retrieve requested market data."""


class ProviderAuthenticationError(MarketDataProviderError):
    """The provider rejected the configured credentials."""


class ProviderRateLimitError(MarketDataProviderError):
    """The provider refused the request because a rate limit was reached."""


class ProviderTimeoutError(MarketDataProviderError):
    """The provider did not answer within the configured timeout."""


class ProviderConnectionError(MarketDataProviderError):
    """The provider could not be reached at all."""


class ProviderResponseError(MarketDataProviderError):
    """The provider answered with a body this adapter cannot trust."""


class ProviderUnavailableError(MarketDataProviderError):
    """The provider reported a server-side failure."""


class ProviderNoDataError(MarketDataProviderError):
    """The provider answered but holds no data for this symbol or range.

    Control-flow signal inside adapters: absence of data is never replaced
    with a fabricated value, so callers translate it into an empty result.
    """


class MarketDataProvider(Protocol):
    """Read-only interface implemented by interchangeable data providers.

    Optional capabilities are detected by attribute: an adapter may expose
    ``health()`` (connectivity snapshot), ``health_check()`` (backend probe),
    and ``lookup_symbol()`` (instrument lookup). Consumers must treat their
    absence as "not supported" rather than inventing a result.
    """

    def get_historical(self, symbol: str, start: datetime, end: datetime) -> Sequence[Bar]:
        """Return bars in the half-open time range [start, end)."""
        ...

    def get_latest(self, symbols: Sequence[str]) -> Sequence[Quote]:
        """Return latest available quotes for requested symbols."""
        ...
