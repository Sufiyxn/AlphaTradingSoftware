"""Provider-agnostic bridge between a configured market-data provider and consumers.

The bridge knows nothing about portfolios, brokers, order flow, or signals.
It fetches real quotes, ages them against a *configured* staleness window, and
reports one of five honest availability states. It never invents, interpolates,
back-fills, or reuses a signal price as a market price: a symbol without a real
quote is reported UNPRICED with the reason it cannot be valued.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .models import Quote
from .service import MarketDataService

CONNECTED = "CONNECTED"
NOT_CONFIGURED = "NOT_CONFIGURED"
UNAVAILABLE = "UNAVAILABLE"
STALE = "STALE"
ERROR = "ERROR"

PRICED = "PRICED"
QUOTE_UNPRICED = "UNPRICED"

REASON_NOT_CONFIGURED = "no continuous market-data provider is connected"
REASON_NO_QUOTE = "the market-data provider returned no quote for the requested symbols"
REASON_NO_REFRESH = "no market-data refresh has run yet"
REASON_STALE = "every available quote is older than the configured staleness window"
REASON_STALE_SYMBOL = "the newest real quote is older than the configured staleness window"

DEFAULT_STALE_AFTER_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class SymbolValuation:
    """One symbol's valuation outcome. ``price`` is ``None`` when UNPRICED."""

    symbol: str
    state: str
    price: float | None = None
    as_of: datetime | None = None
    age_seconds: float | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class BridgeReport:
    availability: str
    reason: str | None
    refreshed_at: datetime
    last_quote_at: datetime | None
    stale_after_seconds: float
    valuations: tuple[SymbolValuation, ...]

    @property
    def by_symbol(self) -> dict[str, SymbolValuation]:
        return {valuation.symbol: valuation for valuation in self.valuations}


class MarketDataBridge:
    """Fetches real quotes and classifies their freshness for any consumer."""

    def __init__(self, service: MarketDataService, *, clock,
                 stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS):
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self._service = service
        self._clock = clock
        self._stale_after = timedelta(seconds=stale_after_seconds)
        self._last_report: BridgeReport | None = None

    @property
    def stale_after(self) -> timedelta:
        return self._stale_after

    @property
    def last_report(self) -> BridgeReport | None:
        return self._last_report

    def availability(self) -> dict:
        """Latest known availability; honest before and after the first refresh."""
        if self._last_report is not None:
            report = self._last_report
            return {"state": report.availability, "reason": report.reason,
                    "last_quote_at": report.last_quote_at,
                    "stale_after_seconds": report.stale_after_seconds,
                    "refreshed_at": report.refreshed_at}
        if self._service.configured:
            state, reason = UNAVAILABLE, REASON_NO_REFRESH
        else:
            state, reason = NOT_CONFIGURED, REASON_NOT_CONFIGURED
        return {"state": state, "reason": reason, "last_quote_at": None,
                "stale_after_seconds": self._stale_after.total_seconds(),
                "refreshed_at": self._clock()}

    def refresh(self, symbols: Sequence[str]) -> BridgeReport:
        requested = _requested(symbols)
        now = self._clock()

        if not self._service.configured:
            return self._store(self._degraded(NOT_CONFIGURED, REASON_NOT_CONFIGURED, requested, now))

        if not requested:
            return self._store(self._degraded(UNAVAILABLE, "no symbols were requested", requested, now))

        try:
            quotes = self._service.get_latest(requested)
        except Exception as error:
            reason = f"the market-data request failed: {error}"
            return self._store(self._degraded(ERROR, reason, requested, now))

        quotes_by_symbol = {quote.symbol: quote for quote in quotes}
        valuations = tuple(
            self._classify(symbol, quotes_by_symbol.get(symbol), now) for symbol in requested)
        availability, reason = _availability(valuations)
        report = BridgeReport(availability, reason, now, _newest(valuations),
                              self._stale_after.total_seconds(), valuations)
        return self._store(report)

    def _classify(self, symbol: str, quote: Quote | None, now: datetime) -> SymbolValuation:
        if quote is None:
            return SymbolValuation(
                symbol, QUOTE_UNPRICED,
                reason=f"the market-data provider returned no quote for {symbol}")
        age = _age(now, quote.timestamp)
        if age is None:
            return SymbolValuation(
                symbol, QUOTE_UNPRICED,
                reason=f"the quote for {symbol} carries no usable observation time")
        if age > self._stale_after:
            return SymbolValuation(symbol, STALE, quote.price, quote.timestamp,
                                   age.total_seconds(), REASON_STALE_SYMBOL)
        return SymbolValuation(symbol, PRICED, quote.price, quote.timestamp, age.total_seconds(), None)

    def _degraded(self, availability: str, reason: str, requested: Sequence[str],
                  now: datetime) -> BridgeReport:
        valuations = tuple(SymbolValuation(symbol, QUOTE_UNPRICED, reason=reason)
                           for symbol in requested)
        return BridgeReport(availability, reason, now, None,
                            self._stale_after.total_seconds(), valuations)

    def _store(self, report: BridgeReport) -> BridgeReport:
        self._last_report = report
        return report


def _requested(symbols: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        symbol.strip().upper() for symbol in symbols
        if isinstance(symbol, str) and symbol.strip()))


def _availability(valuations: Sequence[SymbolValuation]) -> tuple[str, str | None]:
    if any(valuation.state == PRICED for valuation in valuations):
        return CONNECTED, None
    if any(valuation.state == STALE for valuation in valuations):
        return STALE, REASON_STALE
    return UNAVAILABLE, REASON_NO_QUOTE


def _newest(valuations: Sequence[SymbolValuation]) -> datetime | None:
    timestamps = [valuation.as_of for valuation in valuations if valuation.as_of is not None]
    return max(timestamps) if timestamps else None


def _age(now: datetime, timestamp: datetime | None) -> timedelta | None:
    if not isinstance(timestamp, datetime):
        return None
    observed = timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return current - observed
