"""Polygon.io adapter: read-only trades, aggregates, and ticker reference.

Requests carry the configured key as a query parameter; responses are parsed
against the documented schema and anything else raises instead of guessing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .base import VendorMarketDataProvider, epoch_to_utc, require_number
from .models import Bar, Quote
from .provider import ProviderNoDataError, ProviderResponseError

TIMEFRAME_RANGES: Mapping[str, tuple[str, int]] = {
    "1min": ("minute", 1),
    "5min": ("minute", 5),
    "15min": ("minute", 15),
    "30min": ("minute", 30),
    "1h": ("hour", 1),
    "60min": ("hour", 1),
    "1day": ("day", 1),
    "daily": ("day", 1),
}

_SUCCESS = {"OK", "SUCCESS", "SUCCESSFUL"}
_AUTH = {"NOT_AUTHORIZED", "UNAUTHORIZED", "INVALID_API_KEY"}
_RATE = {"TOO_MANY_REQUESTS", "RATE_LIMITED", "RATE_LIMIT"}
_NO_DATA = {"NOT_FOUND", "UNKNOWN_TICKER", "NO_RESULTS", "EMPTY_RESULTS"}
_BAD_REQUEST = {"BAD_REQUEST", "INVALID_ARGUMENTS", "INVALID", "ERROR"}
_MAX_PAGES = 10
_PAGE_LIMIT = 50000


class PolygonMarketDataProvider(VendorMarketDataProvider):
    provider = "polygon"
    label = "Polygon"
    default_base_url = "https://api.polygon.io"
    capabilities = ("historical_ohlcv", "latest_quotes", "symbol_lookup", "read_only")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if self.timeframe not in TIMEFRAME_RANGES:
            supported = ", ".join(sorted(TIMEFRAME_RANGES))
            raise ValueError(
                f"unsupported Polygon timeframe {self.timeframe!r}; use one of {supported}")

    def get_latest(self, symbols: Sequence[str]) -> Sequence[Quote]:
        quotes: list[Quote] = []
        for symbol in dict.fromkeys(str(item).strip().upper() for item in symbols):
            if not symbol:
                continue
            try:
                payload = self._request_json(
                    f"{self.base_url}/v2/last/trade/{symbol}",
                    params={"apiKey": self._api_key},
                    purpose="latest quote",
                    missing_status_is_empty=True,
                )
            except ProviderNoDataError:
                continue
            quotes.append(self._quote_from(symbol, payload))
        return tuple(quotes)

    def get_historical(self, symbol: str, start: datetime, end: datetime) -> Sequence[Bar]:
        symbol = symbol.strip().upper()
        timespan, multiplier = TIMEFRAME_RANGES[self.timeframe]
        window_start = start.astimezone(timezone.utc)
        window_end = end.astimezone(timezone.utc)
        from_date = window_start.date().isoformat()
        to_date = (window_end - timedelta(microseconds=1)).date().isoformat()
        url = (f"{self.base_url}/v2/aggs/ticker/{symbol}/range/"
               f"{multiplier}/{timespan}/{from_date}/{to_date}")
        params: Mapping[str, object] | None = {
            "apiKey": self._api_key,
            "adjusted": "true",
            "sort": "asc",
            "limit": _PAGE_LIMIT,
        }
        bars: dict[datetime, Bar] = {}
        page = 0
        while True:
            try:
                payload = self._request_json(url, params=params, purpose="historical bars",
                                             missing_status_is_empty=True)
            except ProviderNoDataError:
                return ()
            params = None
            results = payload.get("results")
            if results is None:
                break
            if not isinstance(results, list):
                raise self._malformed("historical bar results were not a list")
            for entry in results:
                bar = self._bar_from(symbol, entry, window_start, window_end)
                if bar is not None:
                    bars.setdefault(bar.timestamp, bar)
            next_url = payload.get("next_url")
            if not isinstance(next_url, str) or not next_url:
                break
            page += 1
            if page >= _MAX_PAGES:
                raise ProviderResponseError(
                    "the historical response exceeded the 10-page limit; request a "
                    "narrower range or a coarser timeframe")
            url = self._with_next_key(next_url)
        return tuple(bars[key] for key in sorted(bars))

    def lookup_symbol(self, symbol: str) -> dict | None:
        symbol = symbol.strip().upper()
        try:
            payload = self._request_json(
                f"{self.base_url}/v3/reference/tickers/{symbol}",
                params={"apiKey": self._api_key},
                purpose="symbol lookup",
                missing_status_is_empty=True,
            )
        except ProviderNoDataError:
            return None
        results = payload.get("results")
        if not isinstance(results, dict):
            raise self._malformed("ticker lookup response was missing results")
        ticker = str(results.get("ticker") or "").upper()
        if not ticker:
            raise self._malformed("ticker lookup response was missing the ticker symbol")
        return {
            "symbol": ticker,
            "name": results.get("name"),
            "market": results.get("market"),
            "type": results.get("type"),
            "exchange": results.get("exchange"),
        }

    def _probe(self) -> str:
        self._request_json(
            f"{self.base_url}/v3/reference/tickers/AAPL",
            params={"apiKey": self._api_key},
            purpose="connectivity check",
        )
        return (f"the {self.label} API accepted the configured key "
                "(ticker lookup succeeded)")

    def _body_outcome(self, payload: Any, purpose: str) -> tuple[str, str]:
        if not isinstance(payload, dict):
            return "error", "the response body was not a JSON object"
        status = payload.get("status")
        detail = ""
        for field in ("error", "message"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
        if isinstance(status, str) and status:
            state = status.upper()
            message = f"{status}: {detail}" if detail else f"the API reported status {status}"
            if state in _SUCCESS:
                return "ok", ""
            if state in _AUTH:
                return "auth", message
            if state in _RATE:
                return "rate", message
            if state in _NO_DATA:
                return "no_data", message
            if state in _BAD_REQUEST:
                return "error", message
            return "error", f"unexpected status {status}"
        if not payload:
            return "error", "the response body was an empty object"
        if "results" in payload or "next_url" in payload:
            return "ok", ""
        return "error", "the response did not match the documented Polygon schema"

    def _quote_from(self, symbol: str, payload: Mapping[str, Any]) -> Quote:
        results = payload.get("results")
        if not isinstance(results, dict):
            raise self._malformed("latest quote response was missing trade results")
        if "price" not in results:
            raise self._malformed("latest quote trade was missing its price")
        moment = None
        for field in ("sip_timestamp", "participant_timestamp", "after_hours", "at"):
            if results.get(field) is not None:
                moment = results[field]
                break
        if moment is None:
            raise self._malformed("latest quote trade was missing its timestamp")
        try:
            timestamp = epoch_to_utc(moment)
            price = require_number(results["price"], "price")
            volume = require_number(results.get("volume", 0), "volume")
        except ValueError as error:
            raise self._malformed(str(error)) from None
        if price <= 0:
            raise self._malformed("latest quote trade price was not positive")
        return Quote(symbol, timestamp, price, volume)

    def _bar_from(self, symbol: str, entry: Any, window_start: datetime,
                  window_end: datetime) -> Bar | None:
        if not isinstance(entry, dict):
            raise self._malformed("a historical bar entry was not an object")
        required = ("t", "o", "h", "l", "c")
        if any(field not in entry for field in required):
            raise self._malformed("a historical bar entry was missing OHLC fields")
        try:
            timestamp = epoch_to_utc(entry["t"])
            bar = Bar(
                symbol=symbol,
                timestamp=timestamp,
                open=require_number(entry["o"], "open"),
                high=require_number(entry["h"], "high"),
                low=require_number(entry["l"], "low"),
                close=require_number(entry["c"], "close"),
                volume=require_number(entry.get("v", 0), "volume"),
            )
        except ValueError as error:
            raise self._malformed(str(error)) from None
        if not window_start <= timestamp < window_end:
            return None
        return bar

    def _with_next_key(self, next_url: str) -> str:
        lowered = next_url.lower()
        if "apikey=" in lowered:
            return next_url
        separator = "&" if "?" in next_url else "?"
        return f"{next_url}{separator}apiKey={self._api_key}"
