"""Alpaca market-data adapter tests: schema, auth headers, and failure mapping.

Every response here is scripted through ``FakeTransport`` (tests-only), so the
adapter's parsing, entitlement parameters, header authentication, and error
classification are proven without any network access or credential.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from backend.config import load_config
from backend.market_data import MarketDataService
from backend.market_data.alpaca_data import AlpacaMarketDataProvider
from backend.market_data.provider import (
    ProviderAuthenticationError,
    ProviderNoDataError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from backend.testing.http import FakeResponse, FakeTransport

KEY = "alpaca-key-do-not-leak"
SECRET = "alpaca-secret-do-not-leak"
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)


def build(*queue, routes=None, feed="", timeframe="1min",
          responses=()) -> tuple[AlpacaMarketDataProvider, FakeTransport]:
    transport = FakeTransport(*responses, *queue, routes=routes or {})
    provider = AlpacaMarketDataProvider(api_key=KEY, api_secret=SECRET, feed=feed,
                                        timeframe=timeframe, transport=transport,
                                        clock=lambda: NOW)
    return provider, transport


class AlpacaAdapterTests(unittest.TestCase):
    def test_the_latest_quote_is_an_observed_trade_with_a_utc_timestamp(self):
        provider, transport = build(routes={
            "/v2/stocks/AAPL/trades/latest": FakeResponse(200, {
                "symbol": "AAPL",
                "trade": {"t": "2026-10-12T14:30:00.123456789Z", "x": "V",
                          "p": 171.29, "s": 172, "c": ["@"], "i": 1, "z": "C"},
            }),
        })
        quotes = provider.get_latest(["aapl"])
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].symbol, "AAPL")
        self.assertEqual(quotes[0].price, 171.29)
        self.assertEqual(quotes[0].volume, 172)
        self.assertEqual(quotes[0].timestamp.microsecond, 123456)
        self.assertEqual(quotes[0].timestamp.tzinfo, timezone.utc)
        # Header authentication: the key must never ride in the URL.
        url = transport.requested_urls[0]
        self.assertNotIn(KEY, url)
        self.assertNotIn(SECRET, url)
        headers = transport.requested_headers[0]
        self.assertEqual(headers["apca-api-key-id"], KEY)
        self.assertEqual(headers["apca-api-secret-key"], SECRET)

    def test_the_selected_feed_is_sent_so_recency_matches_the_entitlement(self):
        provider, transport = build(routes={
            "/v2/stocks/AAPL/trades/latest": FakeResponse(200, {
                "symbol": "AAPL",
                "trade": {"t": "2026-10-12T14:30:00Z", "p": 10.0, "s": 1},
            }),
        }, feed="delayed_sip")
        provider.get_latest(["AAPL"])
        self.assertIn("feed=delayed_sip", transport.requested_urls[0])

    def test_historical_bars_parse_page_and_respect_the_requested_window(self):
        first = FakeResponse(200, {
            "bars": [
                {"t": "2026-10-11T13:30:00Z", "o": 10, "h": 11, "l": 9.5, "c": 10.5, "v": 100},
                {"t": "2026-10-11T13:31:00Z", "o": 10.5, "h": 11.2, "l": 10.1, "c": 11.0, "v": 120},
            ],
            "next_page_token": "page-2",
        })
        second = FakeResponse(200, {
            "bars": [
                {"t": "2026-10-11T13:32:00Z", "o": 11, "h": 11.5, "l": 10.9, "c": 11.2, "v": 90},
            ],
            "next_page_token": None,
        })
        provider, transport = build(first, second)
        bars = provider.get_historical(
            "AAPL", datetime(2026, 10, 11, 13, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc))
        self.assertEqual([bar.close for bar in bars], [10.5, 11.0, 11.2])
        self.assertTrue(all(bar.symbol == "AAPL" for bar in bars))
        self.assertTrue(all(bar.high >= bar.close >= bar.low for bar in bars))
        self.assertIn("timeframe=1Min", transport.requested_urls[0])
        self.assertIn("page_token=page-2", transport.requested_urls[1])

    def test_a_bar_outside_the_requested_window_is_dropped_not_relabelled(self):
        route = FakeResponse(200, {
            "bars": [
                {"t": "2026-10-01T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10},
                {"t": "2026-10-11T13:30:00Z", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100},
            ],
        })
        provider, _ = build(routes={"/v2/stocks/AAPL/bars": route})
        bars = provider.get_historical(
            "AAPL", datetime(2026, 10, 11, tzinfo=timezone.utc),
            datetime(2026, 10, 12, tzinfo=timezone.utc))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].timestamp.day, 11)

    def test_credentials_are_reported_never_echoed(self):
        provider, _ = build(FakeResponse(401, {
            "code": 40110000, "message": f"not authorized for {KEY}"}))
        with self.assertRaises(ProviderAuthenticationError) as caught:
            provider.get_latest(["AAPL"])
        self.assertNotIn(KEY, str(caught.exception))
        self.assertIn("***", str(caught.exception))

    def test_a_rejected_secret_never_appears_in_an_error(self):
        provider, _ = build(FakeResponse(401, {
            "code": 40110000, "message": f"bad secret {SECRET}"}))
        with self.assertRaises(ProviderAuthenticationError) as caught:
            provider.get_latest(["AAPL"])
        self.assertNotIn(SECRET, str(caught.exception))

    def test_rate_limits_and_missing_symbols_map_to_distinct_honest_states(self):
        limited, _ = build(FakeResponse(429, {
            "code": 42900000, "message": "rate limit exceeded"}))
        with self.assertRaises(ProviderRateLimitError):
            limited.get_latest(["AAPL"])

        empty, _ = build(routes={
            "/v2/stocks/AAPL/trades/latest": FakeResponse(404, {"message": "not found"}),
        })
        self.assertEqual(empty.get_latest(["AAPL"]), ())

        no_bars, _ = build(FakeResponse(404, {"message": "not found"}))
        service = MarketDataService(no_bars)
        # A 404 means "no bars here" - reported as an empty series, never an error.
        self.assertEqual(
            service.get_historical("AAPL",
                                   datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc),
                                   NOW), ())

    def test_a_body_that_does_not_match_the_documented_schema_is_refused(self):
        provider, _ = build(routes={
            "/v2/stocks/AAPL/trades/latest": FakeResponse(200, {"unexpected": "shape"}),
        })
        with self.assertRaises(ProviderResponseError):
            provider.get_latest(["AAPL"])

    def test_a_malformed_trade_is_refused_instead_of_guessing(self):
        provider, _ = build(routes={
            "/v2/stocks/AAPL/trades/latest": FakeResponse(200, {
                "symbol": "AAPL", "trade": {"t": "not-a-time", "p": 1.0, "s": 1}}),
        })
        with self.assertRaises(ProviderResponseError):
            provider.get_latest(["AAPL"])

    def test_the_connectivity_probe_reports_a_real_clock_answer(self):
        provider, transport = build(routes={
            "/v2/clock": FakeResponse(200, {"is_open": False,
                                            "next_open": "2026-10-12T13:30:00Z",
                                            "next_close": "2026-10-12T20:00:00Z"}),
        })
        outcome = provider.health_check()
        self.assertTrue(outcome["success"], outcome)
        self.assertEqual(outcome["status"], "PASSED")
        self.assertIn("session closed", outcome["detail"])
        self.assertEqual(transport.requested_urls[0],
                         "https://data.alpaca.markets/v2/clock")

    def test_symbol_lookup_returns_reference_data(self):
        provider, _ = build(routes={
            "/v3/reference/tickers/AAPL": FakeResponse(200, {
                "results": {"symbol": "AAPL", "name": "Apple Inc.",
                            "market": "us_equity", "type": "CS", "exchange": "XNAS"},
            }),
        })
        found = provider.lookup_symbol("aapl")
        self.assertEqual(found["symbol"], "AAPL")
        self.assertEqual(found["name"], "Apple Inc.")

    def test_the_registry_builds_alpaca_when_it_is_the_configured_provider(self):
        from backend.market_data import build_market_data_provider
        settings = load_config(env={
            "MARKET_DATA_PROVIDER": "alpaca",
            "ALPACA_API_KEY": KEY,
            "ALPACA_API_SECRET": SECRET,
        }).market_data
        provider = build_market_data_provider(settings)
        self.assertIsInstance(provider, AlpacaMarketDataProvider)
        self.assertEqual(provider.feed, "iex")
        self.assertIn("realtime_stream", provider.capabilities)

    def test_an_unsupported_timeframe_or_feed_is_refused_at_construction(self):
        with self.assertRaises(ValueError):
            AlpacaMarketDataProvider(api_key=KEY, api_secret=SECRET, timeframe="2h")
        with self.assertRaises(ValueError):
            AlpacaMarketDataProvider(api_key=KEY, api_secret=SECRET, feed="dark-pool")

    def test_a_broken_payload_never_yields_a_price(self):
        provider, _ = build(routes={
            "/v2/stocks/AAPL/trades/latest": FakeResponse(200, {
                "symbol": "AAPL", "trade": {"t": "2026-10-12T14:30:00Z", "p": 0.0, "s": 1}}),
        })
        with self.assertRaises(ProviderResponseError):
            provider.get_latest(["AAPL"])


if __name__ == "__main__":
    unittest.main()
