"""Provider-independent historical candle series for the trading terminal.

One request shape, one validation path: every series is fetched through the
same ``MarketDataService`` the trading pipeline uses, so OHLC relationships,
chronology and symbol identity are validated before anything reaches the UI.
When no provider is configured the series is empty with an honest
``NOT_CONFIGURED`` state - no demo candles, no synthetic history, and no
retry loop.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from backend.config.providers import MarketDataSettings

from .models import Bar
from .provider import MarketDataProviderError, ProviderNoDataError
from .registry import build_market_data_provider
from .service import MarketDataService
from .validation import DataValidationError

#: Expected spacing of one candle, used to age data honestly per timeframe.
#: A daily candle cannot be judged stale against a five-minute threshold.
TIMEFRAME_INTERVALS_SECONDS: dict[str, float] = {
    "1min": 60.0,
    "5min": 300.0,
    "15min": 900.0,
    "30min": 1800.0,
    "1h": 3600.0,
    "60min": 3600.0,
    "1day": 86400.0,
    "daily": 86400.0,
}

#: Frames offered when the provider catalog does not advertise its own list.
DEFAULT_TIMEFRAMES = ("1min", "5min", "15min", "1h", "1day")

#: Hard ceiling on returned points; older bars are dropped, never invented.
MAX_POINTS = 2000

#: A request never asks for more history than this, whatever the frame.
MAX_DAYS = 366

#: Series states. OK means real, validated candles are present.
OK = "OK"
NO_DATA = "NO_DATA"
NOT_CONFIGURED = "NOT_CONFIGURED"
ERROR = "ERROR"

FRESH = "FRESH"
STALE = "STALE"

_MINUTE_FRAMES = frozenset({"1min", "5min", "15min", "30min", "1h", "60min"})

_SYMBOL_CHARSET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-^=/:")


@dataclass(frozen=True, slots=True)
class CandleSeries:
    """One validated candle series plus the evidence behind its state."""

    symbol: str
    timeframe: str
    state: str
    reason: str | None
    provider: str | None
    provider_state: str | None
    requested_start: datetime
    requested_end: datetime
    as_of: datetime | None
    age_seconds: float | None
    freshness: str | None
    max_age_seconds: float | None
    truncated: bool
    bar_count: int
    timeframes: tuple[str, ...]
    bars: tuple[Bar, ...] = ()

    @property
    def ok(self) -> bool:
        return self.state is OK

    def payload(self) -> dict:
        """JSON-ready view; candle timestamps are unix seconds for the chart."""
        candles = [
            {
                "time": int(bar.timestamp.timestamp()),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in self.bars
        ]
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "state": self.state,
            "reason": self.reason,
            "provider": self.provider,
            "provider_state": self.provider_state,
            "requested": {
                "start": self.requested_start.isoformat(),
                "end": self.requested_end.isoformat(),
            },
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "age_seconds": self.age_seconds,
            "freshness": self.freshness,
            "max_age_seconds": self.max_age_seconds,
            "truncated": self.truncated,
            "bar_count": self.bar_count,
            "timeframes": list(self.timeframes),
            "candles": candles,
        }


class CandleSeriesService:
    """Serve validated OHLCV history for one timeframe at a time.

    Each requested timeframe gets its own provider instance built from the
    same settings, so a timeframe the connected adapter does not advertise is
    refused up front instead of being silently rewritten to one it does.
    """

    def __init__(self, settings: MarketDataSettings, *, clock=None,
                 stale_after_seconds: float | None = None,
                 provider_factory=None):
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stale_after = float(
            stale_after_seconds if stale_after_seconds is not None
            else settings.stale_after_seconds)
        #: A test or an embedded host may supply its own provider; production
        #: always builds from settings so credentials are never fabricated.
        self._provider_factory = provider_factory
        self._services: dict[str, MarketDataService] = {}
        self._lock = threading.RLock()

    @property
    def settings(self) -> MarketDataSettings:
        return self._settings

    @property
    def configured(self) -> bool:
        """True when a real provider can be asked for candles at all."""
        if self._provider_factory is not None:
            return True
        return bool(self._settings.available)

    def supported_timeframes(self) -> tuple[str, ...]:
        """Frames the connected provider actually advertises."""
        found = self._settings.entry
        if found is None:
            return DEFAULT_TIMEFRAMES
        return tuple(found.timeframes)

    def get(self, symbol: str, *, timeframe: str | None = None,
            days: float | None = None, start: datetime | None = None,
            end: datetime | None = None) -> CandleSeries:
        requested = normalize_symbol(symbol)
        frame = normalize_timeframe(timeframe or self._settings.timeframe,
                                    self.supported_timeframes())
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        window_end = self._bounded_end(end, now)
        window_start = self._bounded_start(start, window_end, frame, days)

        try:
            service = self._service(frame)
        except MarketDataProviderError as error:
            return self._series(requested, frame, window_start, window_end, now,
                                ERROR, str(error))
        except Exception as error:  # noqa: BLE001 - provider build must not 500
            return self._series(requested, frame, window_start, window_end, now,
                                ERROR, f"market-data provider is unavailable: {error}")

        status = service.status
        provider = status.get("provider")
        provider_state = status.get("state")
        if not service.configured:
            return self._series(requested, frame, window_start, window_end, now,
                                NOT_CONFIGURED,
                                str(status.get("reason") or "no market data provider is connected"),
                                provider=provider, provider_state=provider_state)

        try:
            bars = service.get_historical(requested, window_start, window_end)
        except ProviderNoDataError as error:
            return self._series(requested, frame, window_start, window_end, now,
                                NO_DATA, str(error),
                                provider=provider, provider_state=provider_state)
        except MarketDataProviderError as error:
            return self._series(requested, frame, window_start, window_end, now,
                                ERROR, str(error),
                                provider=provider, provider_state=provider_state)
        except DataValidationError as error:
            return self._series(requested, frame, window_start, window_end, now,
                                ERROR, f"provider returned unusable candles: {error}",
                                provider=provider, provider_state=provider_state)

        bars, truncated = self._trim(bars)
        return self._series(requested, frame, window_start, window_end, now,
                            OK if bars else NO_DATA,
                            None if bars else
                            "the provider returned no candles for the requested window",
                            provider=provider, provider_state=provider_state,
                            bars=bars, truncated=truncated)

    def _service(self, timeframe: str) -> MarketDataService:
        with self._lock:
            service = self._services.get(timeframe)
            if service is not None:
                return service
            settings = (self._settings if self._settings.timeframe == timeframe
                        else replace(self._settings, timeframe=timeframe))
            if self._provider_factory is not None:
                provider = self._provider_factory(settings)
            else:
                provider = build_market_data_provider(settings, clock=self._clock)
            service = MarketDataService(provider)
            self._services[timeframe] = service
            return service

    def _bounded_end(self, end: datetime | None, now: datetime) -> datetime:
        if end is None:
            return now
        if end.tzinfo is None or end.utcoffset() is None:
            raise ValueError("end must include a timezone")
        return min(end, now)

    def _bounded_start(self, start: datetime | None, end: datetime,
                       timeframe: str, days: float | None) -> datetime:
        limit = self._max_days(timeframe)
        if start is None:
            span = limit if days is None else float(days)
            if not math.isfinite(span) or span <= 0:
                raise ValueError("days must be a positive number")
            return end - timedelta(days=min(span, limit))
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("start must include a timezone")
        if start >= end:
            raise ValueError("start must be earlier than end")
        if end - start > timedelta(days=limit):
            return end - timedelta(days=limit)
        return start

    @staticmethod
    def _max_days(timeframe: str) -> float:
        interval = TIMEFRAME_INTERVALS_SECONDS[timeframe]
        if timeframe in _MINUTE_FRAMES:
            return max(1.0, math.ceil(MAX_POINTS * interval / 86400))
        return float(MAX_DAYS)

    @staticmethod
    def _trim(bars: tuple[Bar, ...]) -> tuple[tuple[Bar, ...], bool]:
        if len(bars) <= MAX_POINTS:
            return bars, False
        return bars[-MAX_POINTS:], True

    def _series(self, symbol: str, timeframe: str, start: datetime,
                end: datetime, now: datetime, state: str, reason: str | None,
                *, provider: str | None = None, provider_state: str | None = None,
                bars: tuple[Bar, ...] = (), truncated: bool = False) -> CandleSeries:
        interval = TIMEFRAME_INTERVALS_SECONDS[timeframe]
        max_age = max(self._stale_after, 2.0 * interval)
        as_of = bars[-1].timestamp if bars else None
        age = (now - as_of).total_seconds() if as_of is not None else None
        freshness = None if age is None else (FRESH if age <= max_age else STALE)
        return CandleSeries(
            symbol=symbol, timeframe=timeframe, state=state, reason=reason,
            provider=provider, provider_state=provider_state,
            requested_start=start, requested_end=end,
            as_of=as_of, age_seconds=age, freshness=freshness,
            max_age_seconds=max_age, truncated=truncated,
            bar_count=len(bars), timeframes=self.supported_timeframes(),
            bars=bars,
        )


def normalize_symbol(symbol: str) -> str:
    """Uppercase, trim, and reject anything outside the accepted character set."""
    if not isinstance(symbol, str):
        raise ValueError("symbol must be a non-empty string")
    cleaned = symbol.strip().upper()
    if not cleaned or len(cleaned) > 16 or any(ch not in _SYMBOL_CHARSET for ch in cleaned):
        raise ValueError("symbol must be 1-16 characters from letters, digits, .-^=/:")
    return cleaned


def normalize_timeframe(timeframe: str, supported: tuple[str, ...]) -> str:
    if not isinstance(timeframe, str) or not timeframe.strip():
        raise ValueError("timeframe must be a non-empty string")
    frame = timeframe.strip().lower()
    if frame not in TIMEFRAME_INTERVALS_SECONDS:
        raise ValueError(
            f"unknown timeframe {timeframe!r}; use one of "
            f"{', '.join(sorted(TIMEFRAME_INTERVALS_SECONDS))}")
    if frame not in supported:
        raise ValueError(
            f"timeframe {frame!r} is not supported by the connected provider; "
            f"use one of {', '.join(supported)}")
    return frame
