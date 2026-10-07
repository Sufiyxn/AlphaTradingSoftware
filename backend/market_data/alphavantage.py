"""Alpha Vantage adapter: read-only intraday/daily series and symbol search.

Series timestamps carry the exchange-local wall clock (America/New_York) and
are converted to UTC before validation. Throttle notices in the body become an
explicit rate-limit state instead of silently missing data.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .base import VendorMarketDataProvider, require_number
from .models import Bar, Quote
from .provider import ProviderNoDataError
from .session import NEW_YORK

_DAILY_TIMEFRAMES = {"1day", "daily", "day"}
_INTERVALS: Mapping[str, str] = {
    "1min": "1min",
    "5min": "5min",
    "15min": "15min",
    "30min": "30min",
    "1h": "60min",
    "60min": "60min",
}
_RATE_MARKERS = ("frequency", "per minute", "per hour", "per day", "rate limit",
                 "call limit", "quota", "5 calls")
_AUTH_MARKERS = ("api key", "apikey", "api-key")
_NO_DATA_PURPOSES = {"latest quote", "historical bars", "symbol lookup"}


class AlphaVantageMarketDataProvider(VendorMarketDataProvider):
    provider = "alphavantage"
    label = "Alpha Vantage"
    default_base_url = "https://www.alphavantage.co"
    capabilities = ("historical_ohlcv", "latest_quotes", "symbol_lookup", "read_only")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if self._is_daily:
            return
        if self.timeframe not in _INTERVALS:
            supported = ", ".join(sorted(_INTERVALS) + sorted(_DAILY_TIMEFRAMES))
            raise ValueError(
                f"unsupported Alpha Vantage timeframe {self.timeframe!r}; "
                f"use one of {supported}")

    @property
    def _is_daily(self) -> bool:
        return self.timeframe in _DAILY_TIMEFRAMES

    @property
    def _interval(self) -> str:
        return "60min" if self.timeframe in ("1h", "60min") else _INTERVALS.get(
            self.timeframe, "1min")

    def get_latest(self, symbols: Sequence[str]) -> Sequence[Quote]:
        quotes: list[Quote] = []
        for symbol in dict.fromkeys(str(item).strip().upper() for item in symbols):
            if not symbol:
                continue
            quote = self._latest_quote(symbol)
            if quote is not None:
                quotes.append(quote)
        return tuple(quotes)

    def get_historical(self, symbol: str, start: datetime, end: datetime) -> Sequence[Bar]:
        symbol = symbol.strip().upper()
        params, series_key, stamp_format = self._series_request(symbol, outputsize="full")
        try:
            payload = self._request_json(f"{self.base_url}/query", params=params,
                                         purpose="historical bars")
        except ProviderNoDataError:
            return ()
        series = self._series(payload, series_key)
        if series is None:
            return ()
        window_start = start.astimezone(timezone.utc)
        window_end = end.astimezone(timezone.utc)
        bars: list[Bar] = []
        for stamp, entry in series.items():
            if not isinstance(entry, dict):
                raise self._malformed("a time-series entry was not an object")
            timestamp = self._stamp(stamp, stamp_format)
            try:
                bar = Bar(
                    symbol=symbol,
                    timestamp=timestamp,
                    open=require_number(entry.get("1. open"), "open"),
                    high=require_number(entry.get("2. high"), "high"),
                    low=require_number(entry.get("3. low"), "low"),
                    close=require_number(entry.get("4. close"), "close"),
                    volume=require_number(entry.get("5. volume", 0), "volume"),
                )
            except ValueError as error:
                raise self._malformed(str(error)) from None
            if window_start <= timestamp < window_end:
                bars.append(bar)
        bars.sort(key=lambda bar: bar.timestamp)
        return tuple(bars)

    def lookup_symbol(self, symbol: str) -> dict | None:
        symbol = symbol.strip().upper()
        try:
            payload = self._request_json(
                f"{self.base_url}/query",
                params={"function": "SYNTAX_SEARCH", "keywords": symbol,
                        "apikey": self._api_key},
                purpose="symbol lookup",
            )
        except ProviderNoDataError:
            return None
        matches = payload.get("bestMatches")
        if matches is None:
            if isinstance(payload, dict) and payload.get("Meta Data"):
                return None
            raise self._malformed("symbol search response was missing bestMatches")
        if not isinstance(matches, list):
            raise self._malformed("symbol search results were not a list")
        for entry in matches:
            if not isinstance(entry, dict):
                raise self._malformed("a symbol search entry was not an object")
            if str(entry.get("1. symbol") or "").upper() != symbol:
                continue
            return {
                "symbol": str(entry.get("1. symbol") or "").upper(),
                "name": entry.get("2. name"),
                "type": entry.get("3. type"),
                "region": entry.get("4. region"),
                "currency": entry.get("8. currency"),
            }
        return None

    def _probe(self) -> str:
        self._request_json(
            f"{self.base_url}/query",
            params={"function": "SYNTAX_SEARCH", "keywords": "AAPL",
                    "apikey": self._api_key},
            purpose="connectivity check",
        )
        return (f"the {self.label} API accepted the configured key "
                "(symbol search succeeded)")

    def _latest_quote(self, symbol: str) -> Quote | None:
        params, series_key, stamp_format = self._series_request(symbol,
                                                                 outputsize="compact")
        try:
            payload = self._request_json(f"{self.base_url}/query", params=params,
                                         purpose="latest quote")
        except ProviderNoDataError:
            return None
        series = self._series(payload, series_key)
        if not series:
            return None
        stamp = max(series)
        entry = series[stamp]
        if not isinstance(entry, dict):
            raise self._malformed("a time-series entry was not an object")
        if "4. close" not in entry:
            raise self._malformed("the latest time-series entry was missing its close")
        try:
            price = require_number(entry["4. close"], "price")
            volume = require_number(entry.get("5. volume", 0), "volume")
        except ValueError as error:
            raise self._malformed(str(error)) from None
        if price <= 0:
            raise self._malformed("the latest time-series close was not positive")
        return Quote(symbol, self._stamp(stamp, stamp_format), price, volume)

    def _series_request(self, symbol: str, *, outputsize: str) -> tuple[dict, str, str]:
        if self._is_daily:
            return (
                {"function": "TIME_SERIES_DAILY", "symbol": symbol,
                 "outputsize": outputsize, "apikey": self._api_key},
                "Time Series (Daily)",
                "%Y-%m-%d",
            )
        interval = self._interval
        return (
            {"function": "TIME_SERIES_INTRADAY", "symbol": symbol,
             "interval": interval, "outputsize": outputsize,
             "apikey": self._api_key},
            f"Time Series ({interval})",
            "%Y-%m-%d %H:%M:%S",
        )

    def _series(self, payload: Any, series_key: str) -> Mapping[str, Any] | None:
        if not isinstance(payload, dict):
            raise self._malformed("the response body was not a JSON object")
        series = payload.get(series_key)
        if series is None:
            if payload.get("Meta Data"):
                return None
            raise self._malformed(f"the response was missing {series_key!r}")
        if not isinstance(series, dict):
            raise self._malformed(f"{series_key!r} was not an object")
        if not series:
            return None
        return series

    @staticmethod
    def _stamp(value: str, stamp_format: str) -> datetime:
        try:
            naive = datetime.strptime(str(value), stamp_format)
        except ValueError:
            raise ValueError(f"timestamp {value!r} does not match {stamp_format!r}") from None
        moment = naive.replace(tzinfo=NEW_YORK).astimezone(timezone.utc)
        if not 1990 <= moment.year <= 2100:
            raise ValueError("timestamp is outside a plausible market-data range")
        return moment

    def _body_outcome(self, payload: Any, purpose: str) -> tuple[str, str]:
        if not isinstance(payload, dict):
            return "error", "the response body was not a JSON object"
        if not payload:
            return "error", "the response body was an empty object"
        error_message = payload.get("Error Message")
        if isinstance(error_message, str) and error_message.strip():
            text = error_message.strip()
            lowered = text.lower()
            if any(marker in lowered for marker in _AUTH_MARKERS):
                return "auth", text
            if "invalid api call" in lowered and purpose in _NO_DATA_PURPOSES:
                return "no_data", text
            return "error", text
        for field in ("Note", "Information"):
            text = payload.get(field)
            if isinstance(text, str) and text.strip():
                lowered = text.strip()
                if any(marker in lowered.lower() for marker in _RATE_MARKERS):
                    return "rate", lowered
                return "error", lowered
        for key in payload:
            if isinstance(key, str) and key.startswith("Time Series"):
                return "ok", ""
        if "Global Quote" in payload or "bestMatches" in payload or "Meta Data" in payload:
            return "ok", ""
        return "error", "the response did not match the documented Alpha Vantage schema"
