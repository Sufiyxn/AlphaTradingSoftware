"""Contract tests for the real Polygon and Alpha Vantage adapters.

Every adapter in this file runs against scripted HTTP responses from
``backend.testing.http``; no test opens a socket or contacts a vendor API.
The shared contract covers each required failure class, the health-state
transitions, and the no-fabrication rule: on any failure the adapter raises an
explicit error or returns an empty result - never invented data.
"""
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from urllib import request as urlrequest

from backend.config import load_config
from backend.config.providers import MarketDataSettings
from backend.core.runtime import ApplicationRuntime
from backend.core.server import _json_default, create_server
from backend.market_data import (
    MarketDataService,
    MarketDataProviderError,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    UnconfiguredMarketDataProvider,
    build_market_data_provider,
)
from backend.market_data.alphavantage import AlphaVantageMarketDataProvider
from backend.market_data.polygon import PolygonMarketDataProvider
from backend.portfolio.models import PriceState
from backend.testing import FakeResponse, FakeTransport, MockMarketDataProvider
from backend.tests.test_position_valuation import harness, open_paper

API_KEY = "test-key-do-not-leak"
NOW = datetime(2024, 1, 5, 15, 0, tzinfo=timezone.utc)

ADAPTERS = {
    "polygon": PolygonMarketDataProvider,
    "alphavantage": AlphaVantageMarketDataProvider,
}
PROBE_ROUTES = {
    "polygon": ("/v3/reference/tickers/", FakeResponse(200, {
        "status": "OK",
        "results": {"ticker": "AAPL", "name": "Apple Inc.", "market": "stocks",
                    "type": "CS", "exchange": "XNAS"},
    })),
    "alphavantage": ("SYNTAX_SEARCH", FakeResponse(200, {
        "bestMatches": [{"1. symbol": "AAPL", "2. name": "Apple Inc.",
                         "3. type": "Equity", "4. region": "United States",
                         "8. currency": "USD"}],
    })),
}


class FakeClock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def build_adapter(provider_id: str, *responses, routes=None, clock=None,
                  **kwargs):
    transport = FakeTransport(*responses, routes=routes)
    provider = ADAPTERS[provider_id](
        api_key=API_KEY,
        transport=transport,
        clock=clock or (lambda: NOW),
        timeout_seconds=kwargs.pop("timeout_seconds", 10.0),
        stale_after_seconds=kwargs.pop("stale_after_seconds", 300.0),
        timeframe=kwargs.pop("timeframe", "1min"),
        **kwargs,
    )
    return provider, transport


def trade_payload(price=189.5, volume=321, at: datetime = NOW) -> dict:
    moment = int(at.timestamp() * 1e9)
    return {"results": {"T": "AAPL", "price": price, "volume": volume,
                        "sip_timestamp": moment, "participant_timestamp": moment}}


def agg(at: datetime, open_, high, low, close, volume=1000) -> dict:
    return {"t": int(at.timestamp() * 1000), "o": open_, "h": high,
            "l": low, "c": close, "v": volume}


def aggs_payload(results, next_url=None) -> dict:
    payload = {"status": "OK", "results": results}
    if next_url:
        payload["next_url"] = next_url
    return payload


def av_entry(open_, high, low, close, volume=1000) -> dict:
    return {"1. open": str(open_), "2. high": str(high), "3. low": str(low),
            "4. close": str(close), "5. volume": str(volume)}


def av_intraday(series: dict, interval="1min") -> dict:
    return {"Meta Data": {"2. Symbol": "AAPL"},
            f"Time Series ({interval})": series}


class SharedProviderContractTests(unittest.TestCase):
    """Required failure classes and health states, for both vendors."""

    def test_missing_or_blank_credentials_are_rejected_at_construction(self):
        for provider_id, adapter in ADAPTERS.items():
            with self.subTest(provider_id=provider_id):
                with self.assertRaises(ValueError):
                    adapter(api_key="", transport=FakeTransport())
                with self.assertRaises(ValueError):
                    adapter(api_key="   ", transport=FakeTransport())
                with self.assertRaises(ValueError):
                    adapter(transport=FakeTransport())

    def test_rejected_credentials_raise_auth_error_and_never_leak_the_key(self):
        for provider_id in ADAPTERS:
            for status in (401, 403):
                with self.subTest(provider_id=provider_id, status=status):
                    body = {"error": f"key {API_KEY} was rejected"}
                    provider, _ = build_adapter(
                        provider_id, FakeResponse(status, body))
                    with self.assertRaises(ProviderAuthenticationError) as caught:
                        provider.get_latest(("AAPL",))
                    text = str(caught.exception)
                    self.assertIn("authentication failed", text)
                    self.assertNotIn(API_KEY, text)
                    self.assertEqual(provider.health["state"], "ERROR")
                    self.assertNotIn(API_KEY, json.dumps(provider.health, default=str))

    def test_rate_limit_records_window_and_state(self):
        for provider_id in ADAPTERS:
            with self.subTest(provider_id=provider_id):
                provider, _ = build_adapter(
                    provider_id,
                    FakeResponse(429, {"error": "slow down"},
                                 headers={"Retry-After": "120"}))
                with self.assertRaises(ProviderRateLimitError):
                    provider.get_latest(("AAPL",))
                health = provider.health
                self.assertEqual(health["state"], "RATE_LIMITED")
                self.assertEqual(health["rate_limited_until"],
                                 NOW + timedelta(seconds=120))
                self.assertNotIn(API_KEY, json.dumps(health, default=str))

    def test_expired_rate_limit_window_reports_unavailable_not_connected(self):
        clock = FakeClock()
        provider, _ = build_adapter(
            "polygon", FakeResponse(429, {"error": "slow"},
                                    headers={"Retry-After": "60"}), clock=clock)
        with self.assertRaises(ProviderRateLimitError):
            provider.get_latest(("AAPL",))
        clock.advance(61)
        health = provider.health
        self.assertEqual(health["state"], "UNAVAILABLE")
        self.assertIn("expired", health["reason"])

    def test_server_side_failure_is_unavailable(self):
        for provider_id in ADAPTERS:
            for status in (500, 503):
                with self.subTest(provider_id=provider_id, status=status):
                    provider, _ = build_adapter(provider_id,
                                                FakeResponse(status, {"error": "boom"}))
                    with self.assertRaises(ProviderUnavailableError):
                        provider.get_latest(("AAPL",))
                    self.assertEqual(provider.health["state"], "UNAVAILABLE")

    def test_timeout_is_explicit_and_the_message_hides_the_request(self):
        from backend.testing import timeout_error
        for provider_id in ADAPTERS:
            with self.subTest(provider_id=provider_id):
                provider, _ = build_adapter(provider_id, timeout_error())
                with self.assertRaises(ProviderTimeoutError) as caught:
                    provider.get_latest(("AAPL",))
                text = str(caught.exception)
                self.assertIn("timed out", text)
                self.assertNotIn("http", text.lower())
                self.assertNotIn(API_KEY, text)
                self.assertEqual(provider.health["state"], "UNAVAILABLE")

    def test_connection_failure_is_unavailable(self):
        from backend.testing import connection_error
        for provider_id in ADAPTERS:
            with self.subTest(provider_id=provider_id):
                provider, _ = build_adapter(provider_id, connection_error())
                with self.assertRaises(ProviderConnectionError):
                    provider.get_latest(("AAPL",))
                health = provider.health
                self.assertEqual(health["state"], "UNAVAILABLE")
                self.assertIn("could not be reached", health["reason"])

    def test_malformed_non_json_body_is_an_error(self):
        for provider_id in ADAPTERS:
            with self.subTest(provider_id=provider_id):
                provider, _ = build_adapter(
                    provider_id, FakeResponse(200, raw_body=b"<html>oops</html>"))
                with self.assertRaises(ProviderResponseError):
                    provider.get_latest(("AAPL",))
                self.assertEqual(provider.health["state"], "ERROR")

    def test_empty_body_is_an_error(self):
        for provider_id in ADAPTERS:
            with self.subTest(provider_id=provider_id):
                provider, _ = build_adapter(provider_id,
                                            FakeResponse(200, raw_body=b""))
                with self.assertRaises(ProviderResponseError):
                    provider.get_latest(("AAPL",))
                self.assertEqual(provider.health["state"], "ERROR")

    def test_empty_object_response_is_an_error(self):
        for provider_id in ADAPTERS:
            with self.subTest(provider_id=provider_id):
                provider, _ = build_adapter(provider_id, FakeResponse(200, {}))
                with self.assertRaises(ProviderResponseError) as caught:
                    provider.get_latest(("AAPL",))
                self.assertIn("unusable response", str(caught.exception))
                self.assertEqual(provider.health["state"], "ERROR")

    def test_unexpected_schema_is_an_error(self):
        for provider_id in ADAPTERS:
            with self.subTest(provider_id=provider_id):
                provider, _ = build_adapter(
                    provider_id, FakeResponse(200, {"something_else": [1, 2, 3]}))
                with self.assertRaises(ProviderResponseError):
                    provider.get_latest(("AAPL",))
                self.assertEqual(provider.health["state"], "ERROR")

    def test_connecting_state_is_visible_while_a_request_is_in_flight(self):
        transport = FakeTransport()
        provider = PolygonMarketDataProvider(api_key=API_KEY, transport=transport,
                                             clock=lambda: NOW)
        observed = []

        def observe(url):
            observed.append(provider.health["state"])
            return FakeResponse(200, trade_payload())

        transport.enqueue(observe)
        provider.get_latest(("AAPL",))
        self.assertEqual(observed, ["CONNECTING"])
        self.assertEqual(provider.health["state"], "CONNECTED")

    def test_health_check_passes_only_after_a_real_successful_request(self):
        for provider_id in ADAPTERS:
            with self.subTest(provider_id=provider_id):
                fragment, response = PROBE_ROUTES[provider_id]
                provider, _ = build_adapter(provider_id, routes={fragment: response})
                self.assertEqual(provider.health["state"], "UNAVAILABLE")
                self.assertIn("not completed a connectivity check",
                              provider.health["reason"])
                outcome = provider.health_check()
                self.assertTrue(outcome["success"])
                self.assertEqual(outcome["status"], "PASSED")
                self.assertEqual(provider.health["state"], "CONNECTED")
                self.assertEqual(provider.health["last_success_at"], NOW)

    def test_connected_success_decays_to_stale_after_the_window(self):
        clock = FakeClock()
        fragment, response = PROBE_ROUTES["polygon"]
        provider, _ = build_adapter("polygon", routes={fragment: response},
                                    clock=clock, stale_after_seconds=300.0)
        provider.health_check()
        self.assertEqual(provider.health["state"], "CONNECTED")
        clock.advance(301)
        health = provider.health
        self.assertEqual(health["state"], "STALE")
        self.assertIn("older than 300 seconds", health["reason"])

    def test_probe_failures_are_classified_for_the_connection_center(self):
        provider, _ = build_adapter("polygon", FakeResponse(429,
                                                            {"error": "quota"}))
        outcome = provider.health_check()
        self.assertFalse(outcome["success"])
        self.assertEqual(outcome["status"], "RATE_LIMITED")
        self.assertEqual(provider.health["state"], "RATE_LIMITED")


class PolygonAdapterTests(unittest.TestCase):
    def test_latest_quote_parses_documented_trade_fields(self):
        provider, transport = build_adapter(
            "polygon", routes={"/v2/last/trade/": FakeResponse(200, trade_payload(189.5, 321))})
        quotes = provider.get_latest(("aapl", "AAPL"))
        self.assertEqual(len(quotes), 1)
        quote = quotes[0]
        self.assertEqual(quote.symbol, "AAPL")
        self.assertEqual(quote.timestamp, NOW)
        self.assertEqual(quote.price, 189.5)
        self.assertEqual(quote.volume, 321.0)
        self.assertIsNone(quote.bid)
        self.assertIsNone(quote.ask)
        self.assertIn("/v2/last/trade/AAPL", transport.requested_urls[0])
        self.assertIn(f"apiKey={API_KEY}", transport.requested_urls[0])

    def test_latest_quote_without_a_price_is_malformed(self):
        payload = trade_payload()
        del payload["results"]["price"]
        provider, _ = build_adapter("polygon",
                                    routes={"/v2/last/trade/": FakeResponse(200, payload)})
        with self.assertRaises(ProviderResponseError) as caught:
            provider.get_latest(("AAPL",))
        self.assertIn("missing its price", str(caught.exception))
        self.assertEqual(provider.health["state"], "ERROR")

    def test_latest_quote_without_a_timestamp_is_malformed(self):
        payload = trade_payload()
        del payload["results"]["sip_timestamp"]
        del payload["results"]["participant_timestamp"]
        provider, _ = build_adapter("polygon",
                                    routes={"/v2/last/trade/": FakeResponse(200, payload)})
        with self.assertRaises(ProviderResponseError) as caught:
            provider.get_latest(("AAPL",))
        self.assertIn("missing its timestamp", str(caught.exception))

    def test_unknown_symbol_returns_no_quote_without_inventing_one(self):
        provider, _ = build_adapter(
            "polygon", routes={"/v2/last/trade/": FakeResponse(404, {"status": "NOT_FOUND"})})
        self.assertEqual(provider.get_latest(("ZZZZ",)), ())

    def test_no_results_status_returns_no_quote(self):
        provider, _ = build_adapter(
            "polygon", routes={"/v2/last/trade/": FakeResponse(200, {"status": "NO_RESULTS"})})
        self.assertEqual(provider.get_latest(("ZZZZ",)), ())

    def test_unauthorized_status_body_is_an_auth_error(self):
        provider, _ = build_adapter(
            "polygon",
            routes={"/v2/last/trade/": FakeResponse(200, {"status": "NOT_AUTHORIZED",
                                                          "error": "Not Authorized"})})
        with self.assertRaises(ProviderAuthenticationError):
            provider.get_latest(("AAPL",))
        self.assertEqual(provider.health["state"], "ERROR")

    def test_too_many_requests_status_body_is_rate_limited(self):
        provider, _ = build_adapter(
            "polygon",
            routes={"/v2/last/trade/": FakeResponse(200,
                                                    {"status": "TOO_MANY_REQUESTS"})})
        with self.assertRaises(ProviderRateLimitError):
            provider.get_latest(("AAPL",))
        self.assertEqual(provider.health["state"], "RATE_LIMITED")

    def test_historical_url_covers_the_half_open_window(self):
        provider, transport = build_adapter(
            "polygon", routes={"/v2/aggs/": FakeResponse(200, aggs_payload([]))})
        service = MarketDataService(provider)
        service.get_historical("aapl", datetime(2024, 1, 5, tzinfo=timezone.utc),
                               datetime(2024, 1, 6, tzinfo=timezone.utc))
        url = transport.requested_urls[0]
        self.assertIn("/v2/aggs/ticker/AAPL/range/1/minute/2024-01-05/2024-01-05", url)
        self.assertIn("adjusted=true", url)
        self.assertIn("sort=asc", url)
        self.assertIn(f"apiKey={API_KEY}", url)

    def test_historical_filters_sorts_and_deduplicates(self):
        start = datetime(2024, 1, 5, 14, 30, tzinfo=timezone.utc)
        end = datetime(2024, 1, 5, 15, 0, tzinfo=timezone.utc)
        results = [
            agg(datetime(2024, 1, 5, 14, 31, tzinfo=timezone.utc), 101, 102, 100, 102),
            agg(datetime(2024, 1, 5, 14, 30, tzinfo=timezone.utc), 100, 101, 99, 101),
            agg(datetime(2024, 1, 5, 14, 31, tzinfo=timezone.utc), 101, 102, 100, 102),
            agg(datetime(2024, 1, 5, 13, 0, tzinfo=timezone.utc), 90, 91, 89, 91),
            agg(end, 110, 111, 109, 111),
        ]
        provider, _ = build_adapter(
            "polygon", routes={"/v2/aggs/": FakeResponse(200, aggs_payload(results))})
        bars = MarketDataService(provider).get_historical("AAPL", start, end)
        self.assertEqual([bar.timestamp for bar in bars],
                         [datetime(2024, 1, 5, 14, 30, tzinfo=timezone.utc),
                          datetime(2024, 1, 5, 14, 31, tzinfo=timezone.utc)])
        self.assertEqual([bar.close for bar in bars], [101.0, 102.0])

    def test_pagination_follows_next_url_and_adds_the_missing_key(self):
        base = "https://api.polygon.io/v2/aggs/ticker/AAPL/rect/1?cursor=abc"
        provider, transport = build_adapter(
            "polygon",
            FakeResponse(200, aggs_payload(
                [agg(datetime(2024, 1, 5, 14, 31, tzinfo=timezone.utc), 101, 102, 100, 102)],
                next_url=base)),
            FakeResponse(200, aggs_payload(
                [agg(datetime(2024, 1, 5, 14, 32, tzinfo=timezone.utc), 102, 103, 101, 103)])),
        )
        bars = MarketDataService(provider).get_historical(
            "AAPL", datetime(2024, 1, 5, 14, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 5, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(len(bars), 2)
        self.assertEqual(len(transport.requested_urls), 2)
        second = transport.requested_urls[1]
        self.assertIn("cursor=abc", second)
        self.assertEqual(second.count("apiKey="), 1)
        self.assertIn(f"apiKey={API_KEY}", second)

    def test_pagination_does_not_duplicate_an_existing_key(self):
        base = f"https://api.polygon.io/v2/aggs/ticker/AAPL/rect/1?cursor=abc&apiKey={API_KEY}"
        provider, transport = build_adapter(
            "polygon",
            FakeResponse(200, aggs_payload([], next_url=base)),
            FakeResponse(200, aggs_payload([])),
        )
        MarketDataService(provider).get_historical(
            "AAPL", datetime(2024, 1, 5, 14, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 5, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(transport.requested_urls[1].count("apiKey="), 1)

    def test_page_limit_fails_closed_instead_of_silently_truncating(self):
        next_url = "https://api.polygon.io/v2/aggs/ticker/AAPL/rect/1?cursor=x"
        pages = [FakeResponse(200, aggs_payload([], next_url=next_url))
                 for _ in range(10)]
        provider, _ = build_adapter("polygon", *pages)
        with self.assertRaises(ProviderResponseError) as caught:
            MarketDataService(provider).get_historical(
                "AAPL", datetime(2024, 1, 5, 14, 0, tzinfo=timezone.utc),
                datetime(2024, 1, 5, 15, 0, tzinfo=timezone.utc))
        self.assertIn("10-page limit", str(caught.exception))

    def test_empty_results_return_no_bars(self):
        for payload in (aggs_payload([]), {"status": "OK"}):
            with self.subTest(payload=payload):
                provider, _ = build_adapter(
                    "polygon", routes={"/v2/aggs/": FakeResponse(200, payload)})
                bars = MarketDataService(provider).get_historical(
                    "AAPL", datetime(2024, 1, 5, 14, 0, tzinfo=timezone.utc),
                    datetime(2024, 1, 5, 15, 0, tzinfo=timezone.utc))
                self.assertEqual(bars, ())

    def test_malformed_bar_entry_is_an_error(self):
        provider, _ = build_adapter(
            "polygon",
            routes={"/v2/aggs/": FakeResponse(200, aggs_payload([{"t": "junk"}]))})
        with self.assertRaises(ProviderResponseError) as caught:
            MarketDataService(provider).get_historical(
                "AAPL", datetime(2024, 1, 5, 14, 0, tzinfo=timezone.utc),
                datetime(2024, 1, 5, 15, 0, tzinfo=timezone.utc))
        self.assertIn("missing OHLC fields", str(caught.exception))

    def test_lookup_symbol_returns_details_and_none_for_unknown(self):
        provider, _ = build_adapter(
            "polygon",
            routes={
                "/v3/reference/tickers/GOOG": FakeResponse(200, {
                    "status": "OK",
                    "results": {"ticker": "GOOG", "name": "Alphabet Inc.",
                                "market": "stocks", "type": "CS",
                                "exchange": "XNAS"},
                }),
                "/v3/reference/tickers/NOPE": FakeResponse(404, {"status": "NOT_FOUND"}),
            })
        found = provider.lookup_symbol("goog")
        self.assertEqual(found["symbol"], "GOOG")
        self.assertEqual(found["name"], "Alphabet Inc.")
        self.assertIsNone(provider.lookup_symbol("NOPE"))

    def test_unsupported_timeframe_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            PolygonMarketDataProvider(api_key=API_KEY, timeframe="2h",
                                      transport=FakeTransport())


class AlphaVantageAdapterTests(unittest.TestCase):
    def intraday_provider(self, responses=(), series=None, interval="1min",
                          outputsize="compact", **kwargs):
        routes = {}
        if series is not None:
            payload = av_intraday(series, interval=interval)
            routes[f"outputsize={outputsize}"] = FakeResponse(200, payload)
        return build_adapter("alphavantage", *responses, routes=routes, **kwargs)

    def test_latest_quote_converts_new_york_wall_clock_to_utc(self):
        provider, transport = self.intraday_provider(
            series={"2024-01-05 10:00:00": av_entry(189, 190, 188, "189.5", 1234)})
        quotes = provider.get_latest(("aapl",))
        self.assertEqual(len(quotes), 1)
        quote = quotes[0]
        self.assertEqual(quote.symbol, "AAPL")
        self.assertEqual(quote.timestamp, NOW)
        self.assertEqual(quote.price, 189.5)
        self.assertEqual(quote.volume, 1234.0)
        url = transport.requested_urls[0]
        self.assertIn("TIME_SERIES_INTRADAY", url)
        self.assertIn("interval=1min", url)
        self.assertIn(f"apikey={API_KEY}", url)

    def test_latest_quote_skips_an_unknown_symbol(self):
        provider, _ = self.intraday_provider(responses=(
            FakeResponse(200, {"Error Message":
                               "Invalid API call. Please review the API documentation."}),))
        self.assertEqual(provider.get_latest(("ZZZZ",)), ())

    def test_latest_quote_skips_when_the_series_is_absent(self):
        provider, _ = build_adapter(
            "alphavantage", routes={"outputsize=compact": FakeResponse(
                200, {"Meta Data": {"2. Symbol": "AAPL"}})})
        self.assertEqual(provider.get_latest(("AAPL",)), ())

    def test_historical_intraday_is_sorted_and_range_filtered(self):
        series = {
            "2024-01-05 09:31:00": av_entry(101, 102, 100, 102),
            "2024-01-05 09:30:00": av_entry(100, 101, 99, 101),
            "2024-01-05 10:00:00": av_entry(110, 111, 109, 111),
            "2024-01-04 09:30:00": av_entry(90, 91, 89, 91),
        }
        provider, transport = build_adapter(
            "alphavantage",
            routes={"outputsize=full": FakeResponse(200, av_intraday(series))})
        bars = MarketDataService(provider).get_historical(
            "AAPL", datetime(2024, 1, 5, 14, 30, tzinfo=timezone.utc),
            datetime(2024, 1, 5, 15, 0, tzinfo=timezone.utc))
        self.assertEqual([bar.timestamp for bar in bars],
                         [datetime(2024, 1, 5, 14, 30, tzinfo=timezone.utc),
                          datetime(2024, 1, 5, 14, 31, tzinfo=timezone.utc)])
        self.assertIn("outputsize=full", transport.requested_urls[0])

    def test_daily_series_converts_session_dates_to_utc(self):
        provider, transport = build_adapter(
            "alphavantage", timeframe="daily",
            routes={"TIME_SERIES_DAILY": FakeResponse(200, {
                "Meta Data": {"2. Symbol": "AAPL"},
                "Time Series (Daily)": {"2024-01-05": av_entry(100, 110, 95, 105)},
            })})
        bars = MarketDataService(provider).get_historical(
            "AAPL", datetime(2024, 1, 5, tzinfo=timezone.utc),
            datetime(2024, 1, 6, tzinfo=timezone.utc))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].timestamp,
                         datetime(2024, 1, 5, 5, 0, tzinfo=timezone.utc))
        self.assertIn("TIME_SERIES_DAILY", transport.requested_urls[0])

    def test_interval_timeframes_map_to_documented_values(self):
        provider, transport = build_adapter(
            "alphavantage", timeframe="1h",
            routes={"outputsize=full": FakeResponse(200, av_intraday({}, interval="60min"))})
        MarketDataService(provider).get_historical(
            "AAPL", datetime(2024, 1, 5, 14, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 5, 15, 0, tzinfo=timezone.utc))
        self.assertIn("interval=60min", transport.requested_urls[0])

    def test_unsupported_timeframe_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            AlphaVantageMarketDataProvider(api_key=API_KEY, timeframe="2h",
                                           transport=FakeTransport())

    def test_rate_limit_note_marks_rate_limited_with_a_window(self):
        provider, _ = build_adapter(
            "alphavantage",
            routes={"outputsize=compact": FakeResponse(200, {
                "Note": ("Thank you for using Alpha Vantage! Our standard API call "
                         "frequency is 5 calls per minute and 500 calls per day.")})})
        with self.assertRaises(ProviderRateLimitError):
            provider.get_latest(("AAPL",))
        health = provider.health
        self.assertEqual(health["state"], "RATE_LIMITED")
        self.assertEqual(health["rate_limited_until"], NOW + timedelta(seconds=60))

    def test_error_message_mentioning_a_key_is_an_authentication_failure(self):
        provider, _ = build_adapter(
            "alphavantage",
            routes={"outputsize=compact": FakeResponse(200, {
                "Error Message": f"Invalid API KEY {API_KEY}. Please claim your key."})})
        with self.assertRaises(ProviderAuthenticationError) as caught:
            provider.get_latest(("AAPL",))
        self.assertNotIn(API_KEY, str(caught.exception))
        self.assertEqual(provider.health["state"], "ERROR")

    def test_premium_information_is_an_explicit_error_not_missing_data(self):
        provider, _ = build_adapter(
            "alphavantage",
            routes={"outputsize=compact": FakeResponse(200, {
                "Information": "This endpoint is available on the premium plan."})})
        with self.assertRaises(ProviderResponseError):
            provider.get_latest(("AAPL",))
        self.assertEqual(provider.health["state"], "ERROR")

    def test_lookup_symbol_matches_exactly_and_returns_none_for_a_miss(self):
        provider, _ = build_adapter(
            "alphavantage",
            routes={"SYNTAX_SEARCH": FakeResponse(200, {"bestMatches": [
                {"1. symbol": "IBM", "2. name": "IBM Corp",
                 "3. type": "Equity", "4. region": "United States",
                 "8. currency": "USD"},
                {"1. symbol": "AAPL", "2. name": "Apple Inc.",
                 "3. type": "Equity", "4. region": "United States",
                 "8. currency": "USD"},
            ]})})
        found = provider.lookup_symbol("AAPL")
        self.assertEqual(found["symbol"], "AAPL")
        self.assertEqual(found["name"], "Apple Inc.")
        self.assertIsNone(provider.lookup_symbol("ZZZZ"))


class ServiceRegistryAndConfigTests(unittest.TestCase):
    def test_status_moves_unavailable_connected_stale_with_real_evidence(self):
        clock = FakeClock()
        fragment, response = PROBE_ROUTES["polygon"]
        provider, _ = build_adapter("polygon", routes={fragment: response}, clock=clock)
        service = MarketDataService(provider)
        self.assertEqual(service.status["state"], "UNAVAILABLE")
        self.assertIn("not completed a connectivity check", service.status["reason"])
        outcome = service.check()
        self.assertTrue(outcome["success"])
        self.assertEqual(outcome["status"], "PASSED")
        self.assertEqual(service.status["state"], "CONNECTED")
        self.assertIn("historical_ohlcv", service.status["capabilities"])
        clock.advance(301)
        self.assertEqual(service.status["state"], "STALE")

    def test_rate_limit_state_is_visible_in_service_status(self):
        provider, _ = build_adapter("polygon",
                                    FakeResponse(429, {"error": "quota"}))
        service = MarketDataService(provider)
        with self.assertRaises(ProviderRateLimitError):
            service.get_latest(("AAPL",))
        self.assertEqual(service.status["state"], "RATE_LIMITED")
        self.assertIn("rate limited", service.status["reason"])

    def test_check_without_a_provider_is_not_configured_and_never_requests(self):
        service = MarketDataService(UnconfiguredMarketDataProvider())
        outcome = service.check()
        self.assertEqual(outcome["status"], "NOT_CONFIGURED")
        self.assertFalse(outcome["success"])
        self.assertEqual(service.status["state"], "NOT_CONFIGURED")

    def test_check_for_a_fixture_provider_reports_unsupported(self):
        service = MarketDataService(MockMarketDataProvider())
        outcome = service.check()
        self.assertEqual(outcome["status"], "UNSUPPORTED")
        self.assertIn("no network request was made", outcome["detail"])
        self.assertEqual(service.status["state"], "CONFIGURED")

    def test_rate_limit_detail_surfaces_through_the_service(self):
        provider, _ = build_adapter("polygon",
                                    FakeResponse(429, {"error": "quota"}))
        service = MarketDataService(provider)
        with self.assertRaises(ProviderRateLimitError) as caught:
            service.get_latest(("AAPL",))
        self.assertIn("rate limited", str(caught.exception))

    def test_auth_failure_raises_instead_of_returning_quotes(self):
        provider, _ = build_adapter("polygon", FakeResponse(401, {"error": "no"}))
        service = MarketDataService(provider)
        with self.assertRaises(ProviderAuthenticationError):
            service.get_latest(("AAPL",))

    def test_symbol_lookup_passes_through_and_reports_unsupported(self):
        fragment, response = PROBE_ROUTES["polygon"]
        provider, _ = build_adapter("polygon", routes={fragment: response})
        service = MarketDataService(provider)
        self.assertEqual(service.lookup("aapl")["symbol"], "AAPL")
        fixture = MarketDataService(MockMarketDataProvider())
        with self.assertRaises(MarketDataProviderError):
            fixture.lookup("AAPL")

    def test_registry_builds_real_adapters_with_an_injected_transport(self):
        settings = load_config(env={
            "MARKET_DATA_PROVIDER": "polygon",
            "POLYGON_API_KEY": API_KEY,
        }).market_data
        fragment, response = PROBE_ROUTES["polygon"]
        transport = FakeTransport(routes={fragment: response})
        provider = build_market_data_provider(settings, transport=transport,
                                              clock=lambda: NOW)
        self.assertIsInstance(provider, PolygonMarketDataProvider)
        self.assertTrue(provider.health_check()["success"])
        self.assertTrue(transport.requested_urls)

    def test_registry_never_selects_fixture_or_mock_providers(self):
        for provider_id in ("mock", "test_fixture", "fixture", "test"):
            with self.subTest(provider_id=provider_id):
                settings = MarketDataSettings(provider=provider_id, api_key="present")
                built = build_market_data_provider(settings)
                self.assertIsInstance(built, UnconfiguredMarketDataProvider)

    def test_settings_expose_timeout_and_validate_timeframe(self):
        settings = load_config(env={"MARKET_DATA_TIMEOUT_SECONDS": "7.5"}).market_data
        self.assertEqual(settings.timeout_seconds, 7.5)
        with self.assertRaises(ValueError):
            load_config(env={"MARKET_DATA_TIMEOUT_SECONDS": "0"})
        with self.assertRaises(ValueError):
            load_config(env={"MARKET_DATA_PROVIDER": "polygon",
                             "POLYGON_API_KEY": API_KEY,
                             "MARKET_DATA_TIMEFRAME": "2h"})
        fragment, response = PROBE_ROUTES["polygon"]
        settings = load_config(env={
            "MARKET_DATA_PROVIDER": "polygon",
            "POLYGON_API_KEY": API_KEY,
            "MARKET_DATA_TIMEOUT_SECONDS": "7.5",
        }).market_data
        transport = FakeTransport(routes={fragment: response})
        provider = build_market_data_provider(settings, transport=transport,
                                              clock=lambda: NOW)
        provider.health_check()
        self.assertEqual(transport.requested_timeouts, [7.5])

    def test_each_vendor_only_accepts_its_own_credential_variable(self):
        polygon_only = load_config(env={"MARKET_DATA_PROVIDER": "alphavantage",
                                        "POLYGON_API_KEY": API_KEY}).market_data
        self.assertFalse(polygon_only.configured)
        self.assertEqual(polygon_only.missing_credentials, ("ALPHAVANTAGE_API_KEY",))
        av_configured = load_config(env={
            "MARKET_DATA_PROVIDER": "alphavantage",
            "ALPHAVANTAGE_API_KEY": API_KEY,
        }).market_data
        self.assertTrue(av_configured.configured)
        self.assertTrue(av_configured.available)


class RealProviderValuationTests(unittest.TestCase):
    def test_paper_position_marks_from_a_real_provider_shaped_quote(self):
        provider, _ = build_adapter(
            "polygon", routes={"/v2/last/trade/": FakeResponse(
                200, trade_payload(105.0, 500, at=NOW))})
        portfolio, engine, valuation = harness(provider)
        open_paper(portfolio, engine, symbol="AAA", entry=100.0)
        report = valuation.refresh(["AAA"])
        self.assertEqual(report.availability, "CONNECTED")
        position = portfolio.positions[0]
        self.assertEqual(position.price_state, PriceState.FRESH)
        self.assertEqual(position.market_price, 105.0)
        self.assertEqual(position.unrealized_pnl, 50.0)
        self.assertTrue(position.auto_exit_enabled)


class ConnectionCenterAndEndpointTests(unittest.TestCase):
    def build_runtime(self, env, provider=None):
        return ApplicationRuntime(database_path=":memory:", owner_token="owner-test",
                                  clock=lambda: NOW, config=load_config(env=env),
                                  market_data_provider=provider)

    def test_connection_test_runs_a_backend_probe_and_reports_connectivity(self):
        routes = {
            "/v3/reference/tickers/": FakeResponse(200, {
                "status": "OK",
                "results": {"ticker": "AAPL", "name": "Apple Inc.",
                            "market": "stocks", "type": "CS",
                            "exchange": "XNAS"},
            }),
            "/v2/last/trade/": FakeResponse(200, trade_payload(105.0)),
            "/v2/aggs/": FakeResponse(200, aggs_payload([])),
        }
        provider, _ = build_adapter("polygon", routes=routes)
        runtime = self.build_runtime(
            {"MARKET_DATA_PROVIDER": "polygon", "POLYGON_API_KEY": API_KEY}, provider)
        try:
            result = runtime.connections.test("market_data")
            self.assertEqual(result["status"], "PASSED")
            self.assertTrue(result["success"])
            self.assertNotIn("no network request was made", result["message"])
            status = runtime.connections.status()
            self.assertEqual(status["market_data"]["implemented"], True)
            self.assertEqual(status["market_data"]["connectivity"]["state"], "CONNECTED")
            self.assertNotIn(API_KEY, json.dumps(status, default=_json_default))
            readiness = runtime.connections.readiness()
            self.assertTrue(any(item["key"] == "market_data_test_passed"
                                and item["passed"] for item in readiness["checks"]))
            cycle = runtime.orchestrator.run_once()
            self.assertEqual(cycle.approved_trades, ())
        finally:
            runtime.close()

    def test_endpoints_report_honest_no_data_instead_of_claimed_results(self):
        runtime = self.build_runtime({})
        server = create_server(runtime, "127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            backtest = self.get(port, "/api/backtest")
            self.assertEqual(backtest["status"], "NO_DATA")
            self.assertIsNone(backtest["metrics"])
            self.assertIn("no historical bars were retrieved", backtest["reason"])
            quotes = self.get(port, "/api/quotes")
            self.assertEqual(quotes["provider"], "none")
            self.assertEqual(quotes["state"], "NOT_CONFIGURED")
            self.assertEqual(quotes["quotes"], [])
            connections = self.get(port, "/api/connections")
            self.assertEqual(connections["market_data"]["connectivity"]["state"],
                             "NOT_CONFIGURED")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            runtime.close()

    def test_quotes_endpoint_serves_provider_quotes_when_configured(self):
        routes = {
            "/v2/last/trade/": FakeResponse(200, trade_payload(105.0)),
            "/v3/reference/tickers/": FakeResponse(200, {
                "status": "OK", "results": {"ticker": "AAPL", "name": "Apple Inc."}}),
        }
        provider, _ = build_adapter("polygon", routes=routes)
        runtime = self.build_runtime(
            {"MARKET_DATA_PROVIDER": "polygon", "POLYGON_API_KEY": API_KEY}, provider)
        server = create_server(runtime, "127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            quotes = self.get(port, "/api/quotes")
            self.assertEqual(quotes["provider"], "polygon")
            self.assertEqual(quotes["state"], "CONNECTED")
            self.assertTrue(quotes["quotes"])
            self.assertEqual(quotes["quotes"][0]["price"], 105.0)
            self.assertNotIn(API_KEY, json.dumps(quotes, default=_json_default))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            runtime.close()

    @staticmethod
    def get(port, path):
        with urlrequest.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
