from datetime import datetime
from typing import Sequence

from .models import Bar, Quote
from .provider import MarketDataProvider, MarketDataProviderError
from .validation import DataValidationError, normalize_bar, normalize_quote


class MarketDataService:
    """Validate provider output before exposing normalized market data to consumers."""

    def __init__(self, provider: MarketDataProvider, stream=None):
        self._provider = provider
        self._stream = stream

    @property
    def configured(self) -> bool:
        return bool(getattr(self._provider, "is_configured", True))

    @property
    def stream(self):
        """The live-quote stream attached to this service, if one is running."""
        return self._stream

    def attach_stream(self, stream) -> None:
        """Attach (or replace) the streaming source used by ``get_latest``."""
        self._stream = stream

    @property
    def provider_id(self) -> str:
        return str(getattr(self._provider, "provider", "unknown")).strip().lower()

    @property
    def status(self) -> dict:
        """Honest provider state; NOT_CONFIGURED when nothing real is connected.

        For a real adapter the state comes from recorded evidence: credentials
        alone report UNAVAILABLE, a successful request reports CONNECTED, a
        quiet window downgrades to STALE, and throttles report RATE_LIMITED.
        """
        if not self.configured:
            return {
                "state": "NOT_CONFIGURED",
                "provider": self.provider_id,
                "reason": str(getattr(self._provider, "reason", None)
                              or "no market data provider is connected"),
            }
        snapshot = getattr(self._provider, "health", None)
        if isinstance(snapshot, dict) and snapshot.get("state"):
            return dict(snapshot)
        return {"state": "CONFIGURED", "provider": self.provider_id, "reason": None}

    def check(self) -> dict:
        """Backend connectivity probe; the browser never performs this request."""
        if not self.configured:
            return {
                "status": "NOT_CONFIGURED",
                "success": False,
                "state": "NOT_CONFIGURED",
                "detail": str(getattr(self._provider, "reason", None)
                              or "no market data provider is connected"),
            }
        probe = getattr(self._provider, "health_check", None)
        state = str(self.status.get("state") or "CONFIGURED")
        if not callable(probe):
            return {
                "status": "UNSUPPORTED",
                "success": False,
                "state": state,
                "detail": (f"the {self.provider_id} provider exposes no connectivity "
                           "probe; no network request was made"),
            }
        outcome = probe()
        if not isinstance(outcome, dict):
            return {
                "status": "UNSUPPORTED",
                "success": False,
                "state": state,
                "detail": (f"the {self.provider_id} provider returned an unusable probe "
                           "result; no network request was made"),
            }
        success = bool(outcome.get("success"))
        return {
            "status": str(outcome.get("status") or ("PASSED" if success else "FAILED")),
            "success": success,
            "state": str(self.status.get("state") or state),
            "detail": str(outcome.get("detail") or ""),
        }

    def lookup(self, symbol: str) -> dict | None:
        """Instrument lookup for a connected provider, or None when unsupported."""
        lookup_symbol = getattr(self._provider, "lookup_symbol", None)
        if not callable(lookup_symbol):
            raise MarketDataProviderError(
                f"symbol lookup is not supported by the {self.provider_id} provider")
        return lookup_symbol(symbol)

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
        except MarketDataProviderError:
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
        """Latest quotes, preferring live stream events over REST snapshots.

        The stream and the REST adapter are two views of the same provider:
        whatever arrived on the live socket wins for that symbol (it is newer),
        and REST fills the symbols the stream has not seen yet. Every returned
        quote - from either path - has already been validated by
        ``normalize_quote``, so a consumer can never observe an unvalidated
        price. Symbols nobody answered stay absent; they are never invented.
        """
        requested = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols if isinstance(symbol, str) and symbol.strip()))
        if not requested:
            raise DataValidationError("at least one non-empty symbol is required")
        streamed: dict[str, Quote] = self._stream.latest(requested) if self._stream else {}
        rest: tuple[Quote, ...] = ()
        missing = [symbol for symbol in requested if symbol not in streamed]
        if missing:
            try:
                rest = tuple(normalize_quote(quote) for quote in self._provider.get_latest(missing))
            except DataValidationError:
                raise
            except MarketDataProviderError:
                raise
            except Exception as error:
                raise MarketDataProviderError("latest data request failed") from error
            returned: set[str] = set()
            for quote in rest:
                if quote.symbol not in requested:
                    raise DataValidationError(f"provider returned unexpected symbol {quote.symbol}")
                if quote.symbol in returned:
                    raise DataValidationError(f"provider returned duplicate quote for {quote.symbol}")
                returned.add(quote.symbol)
        merged = dict(streamed)
        for quote in rest:
            merged.setdefault(quote.symbol, quote)
        return tuple(merged[symbol] for symbol in requested if symbol in merged)
