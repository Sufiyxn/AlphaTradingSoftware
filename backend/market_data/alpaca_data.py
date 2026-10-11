"""Alpaca market-data adapter: read-only trades, bars, and ticker reference.

Alpaca is the one US-equities data source this build can use **for free**: the
free plan streams real-time IEX trades over WebSocket and serves 7+ years of
historical bars over REST (``feed=iex``). The paid plan adds the consolidated
SIP feed, selected through ``MARKET_DATA_STREAM_FEED``.

Requests authenticate with headers (``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY``)
so the key never appears in a URL, and every response is parsed against the
documented schema; anything else raises instead of guessing. The *latest* value
is always an observed trade price - never a mid, never a derived mark.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .base import VendorMarketDataProvider, rfc3339_to_utc, require_number
from .models import Bar, Quote
from .provider import ProviderNoDataError

#: REST timeframe names this adapter maps to the platform's timeframes.
TIMEFRAMES: Mapping[str, str] = {
    "1min": "1Min",
    "5min": "5Min",
    "15min": "15Min",
    "1h": "1Hour",
    "60min": "1Hour",
    "1day": "1Day",
    "daily": "1Day",
}

FEEDS = ("iex", "sip", "delayed_sip")
_MAX_PAGES = 10
_PAGE_LIMIT = 10_000


class AlpacaMarketDataProvider(VendorMarketDataProvider):
    provider = "alpaca"
    label = "Alpaca"
    default_base_url = "https://data.alpaca.markets"
    capabilities = ("historical_ohlcv", "latest_quotes", "symbol_lookup",
                    "read_only", "realtime_stream")

    def __init__(self, *, api_secret: str | None = None, feed: str = "",
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._api_secret = (api_secret or "").strip() or None
        selected = (feed or "iex").strip().lower()
        if selected not in FEEDS:
            raise ValueError(f"Alpaca feed must be one of {', '.join(FEEDS)}, "
                             f"received {selected!r}")
        self.feed = selected
        if self.timeframe not in TIMEFRAMES:
            supported = ", ".join(sorted(TIMEFRAMES))
            raise ValueError(
                f"unsupported Alpaca timeframe {self.timeframe!r}; use one of {supported}")

    # -- request helpers --------------------------------------------------
    def _extra_headers(self) -> Mapping[str, str]:
        headers = {"APCA-API-KEY-ID": self._api_key}
        if self._api_secret:
            headers["APCA-API-SECRET-KEY"] = self._api_secret
        return headers

    def _feed_params(self) -> dict[str, str]:
        return {"feed": self.feed}

    # -- public API -------------------------------------------------------
    def get_latest(self, symbols: Sequence[str]) -> Sequence[Quote]:
        quotes: list[Quote] = []
        for symbol in dict.fromkeys(str(item).strip().upper() for item in symbols):
            if not symbol:
                continue
            try:
                payload = self._request_json(
                    f"{self.base_url}/v2/stocks/{symbol}/trades/latest",
                    params=self._feed_params(),
                    purpose="latest quote",
                    missing_status_is_empty=True,
                )
            except ProviderNoDataError:
                continue
            quotes.append(self._quote_from(symbol, payload))
        return tuple(quotes)

    def get_historical(self, symbol: str, start: datetime, end: datetime) -> Sequence[Bar]:
        symbol = symbol.strip().upper()
        window_start = start.astimezone(timezone.utc)
        window_end = end.astimezone(timezone.utc)
        params: dict[str, Any] = {
            **self._feed_params(),
            "timeframe": TIMEFRAMES[self.timeframe],
            "start": window_start.isoformat().replace("+00:00", "Z"),
            "end": window_end.isoformat().replace("+00:00", "Z"),
            "limit": _PAGE_LIMIT,
            "adjustment": "split",
            "sort": "asc",
        }
        bars: dict[datetime, Bar] = {}
        page = 0
        while True:
            try:
                payload = self._request_json(
                    f"{self.base_url}/v2/stocks/{symbol}/bars",
                    params=params, purpose="historical bars",
                    missing_status_is_empty=True)
            except ProviderNoDataError:
                return ()
            results = payload.get("bars")
            if results is None:
                break
            if not isinstance(results, list):
                raise self._malformed("historical bar results were not a list")
            for entry in results:
                bar = self._bar_from(symbol, entry, window_start, window_end)
                if bar is not None:
                    bars.setdefault(bar.timestamp, bar)
            token = payload.get("next_page_token")
            if not isinstance(token, str) or not token:
                break
            page += 1
            if page >= _MAX_PAGES:
                from .provider import ProviderResponseError
                raise ProviderResponseError(
                    "the historical response exceeded the 10-page limit; request a "
                    "narrower range or a coarser timeframe")
            params = {**params, "page_token": token}
        return tuple(bars[key] for key in sorted(bars))

    def lookup_symbol(self, symbol: str) -> dict | None:
        symbol = symbol.strip().upper()
        try:
            payload = self._request_json(
                f"{self.base_url}/v3/reference/tickers/{symbol}",
                params={"symbol": symbol}, purpose="symbol lookup",
                missing_status_is_empty=True)
        except ProviderNoDataError:
            return None
        results = payload.get("results")
        if not isinstance(results, dict):
            raise self._malformed("ticker lookup response was missing results")
        ticker = str(results.get("symbol") or results.get("ticker") or "").upper()
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
        payload = self._request_json(f"{self.base_url}/v2/clock",
                                     purpose="connectivity check")
        opened = payload.get("is_open")
        state = "open" if opened is True else "closed" if opened is False else "unknown"
        return (f"the {self.label} data API accepted the configured key "
                f"(market clock answered; session {state})")

    # -- parsing ----------------------------------------------------------
    def _body_outcome(self, payload: Any, purpose: str) -> tuple[str, str]:
        if not isinstance(payload, dict):
            return "error", "the response body was not a JSON object"
        message = payload.get("message")
        detail = message.strip() if isinstance(message, str) and message.strip() else ""
        code = payload.get("code")
        if code is not None and not any(key in payload for key in
                                        ("trade", "bars", "results", "clock", "next_page_token")):
            lowered = detail.lower()
            if str(code).startswith("401") or "not authorized" in lowered or "unauthorized" in lowered:
                return "auth", detail or "the configured credentials were rejected"
            if str(code).startswith("429") or "rate" in lowered:
                return "rate", detail or "the request quota was exhausted"
            if str(code).startswith("404") or "not found" in lowered:
                return "no_data", detail
            return "error", detail or f"the API reported error code {code}"
        if not payload:
            return "error", "the response body was an empty object"
        if any(key in payload for key in ("trade", "bars", "results", "clock",
                                          "next_page_token", "symbol", "is_open")):
            return "ok", ""
        return "error", "the response did not match the documented Alpaca schema"

    def _quote_from(self, symbol: str, payload: Mapping[str, Any]) -> Quote:
        trade = payload.get("trade")
        if not isinstance(trade, dict):
            raise self._malformed("latest trade response was missing the trade object")
        if "p" not in trade or "t" not in trade:
            raise self._malformed("latest trade was missing its price or timestamp")
        try:
            price = require_number(trade["p"], "price")
            volume = require_number(trade.get("s", 0), "volume")
            timestamp = rfc3339_to_utc(trade["t"])
        except ValueError as error:
            raise self._malformed(str(error)) from None
        if price <= 0:
            raise self._malformed("latest trade price was not positive")
        return Quote(symbol, timestamp, price, volume)

    def _bar_from(self, symbol: str, entry: Any, window_start: datetime,
                  window_end: datetime) -> Bar | None:
        if not isinstance(entry, dict):
            raise self._malformed("a historical bar entry was not an object")
        required = ("t", "o", "h", "l", "c")
        if any(field not in entry for field in required):
            raise self._malformed("a historical bar entry was missing OHLC fields")
        try:
            timestamp = rfc3339_to_utc(entry["t"])
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
