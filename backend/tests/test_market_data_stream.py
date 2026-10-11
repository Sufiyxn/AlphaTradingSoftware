"""Streaming market-data tests: protocol, socket, reconnection, and honesty.

These tests run against a loopback RFC 6455 server (``backend.testing.ws_server``)
so a real socket, a real handshake, real frames, and real reconnects are
exercised without any network access or vendor credential. Nothing here is
wired into the application: ``ApplicationRuntime`` only ever starts a stream
the operator's own configuration asked for.

Covers: provider message normalization (Polygon + Alpaca), authentication and
subscription refusals, reconnection after an abrupt drop, malformed events,
missing-symbol honesty (never a fabricated price), the REST/stream merge in
``MarketDataService``, configuration reporting, and secret redaction.
"""
from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timezone

from backend.config import load_config
from backend.config.providers import MarketDataSettings
from backend.market_data import MarketDataService
from backend.market_data.stream import (
    STATE_AUTH_FAILED,
    STATE_CONNECTED,
    STATE_NOT_CONFIGURED,
    STATE_OFF,
    STATE_READY,
    STATE_SUBSCRIPTION_FAILED,
    AlpacaStreamProtocol,
    MarketDataStream,
    PolygonStreamProtocol,
    build_market_stream,
    stream_configuration,
)
from backend.testing.market_data import MockMarketDataProvider
from backend.testing.ws_server import WebSocketTestServer

API_KEY = "stream-key-do-not-leak-123456"


def wait_for(predicate, timeout: float = 6.0, interval: float = 0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def polygon_settings(url: str, **overrides) -> MarketDataSettings:
    values = {"provider": "polygon", "api_key": API_KEY, "stream_url": url}
    values.update(overrides)
    return MarketDataSettings(**values)


def alpaca_settings(url: str, **overrides) -> MarketDataSettings:
    values = {"provider": "alpaca", "api_key": API_KEY,
              "api_secret": "stream-secret-do-not-leak", "stream_url": url}
    values.update(overrides)
    return MarketDataSettings(**values)


# -- protocol unit tests ----------------------------------------------------
class PolygonProtocolTests(unittest.TestCase):
    def setUp(self):
        self.protocol = PolygonStreamProtocol()

    def test_auth_and_subscribe_frames_match_the_documented_wire_format(self):
        settings = polygon_settings("wss://example.invalid/stocks")
        self.assertEqual(json.loads(self.protocol.auth_message(settings)),
                         {"action": "auth", "params": API_KEY})
        self.assertEqual(json.loads(self.protocol.subscribe_message(["AAPL", "MSFT"])),
                         {"action": "subscribe", "params": "T.AAPL,T.MSFT"})

    def test_realtime_and_delayed_endpoints_are_distinct_and_documented(self):
        settings = MarketDataSettings(provider="polygon", api_key=API_KEY)
        self.assertEqual(PolygonStreamProtocol("realtime").endpoint(settings),
                         "wss://socket.massive.com/stocks")
        self.assertEqual(PolygonStreamProtocol("delayed").endpoint(settings),
                         "wss://delayed.massive.com/stocks")
        self.assertIn("delayed", PolygonStreamProtocol("delayed").entitlement)
        override = polygon_settings("ws://127.0.0.1:9/stocks")
        self.assertEqual(PolygonStreamProtocol().endpoint(override),
                         "ws://127.0.0.1:9/stocks")

    def test_an_unknown_feed_is_refused_rather_than_silently_downgraded(self):
        with self.assertRaises(ValueError):
            PolygonStreamProtocol("instant")

    def test_a_trade_event_normalizes_to_a_quote_with_a_utc_provider_timestamp(self):
        events = self.protocol.interpret(json.dumps([{
            "ev": "T", "sym": "msft", "p": 114.125, "s": 100,
            "t": 1536036818784,
        }]))
        self.assertEqual(len(events), 1)
        quote = events[0].quote
        self.assertEqual(quote.symbol, "MSFT")
        self.assertEqual(quote.price, 114.125)
        self.assertEqual(quote.volume, 100)
        self.assertEqual(quote.timestamp, datetime(2018, 9, 4, 4, 53, 38, 784000,
                                                   tzinfo=timezone.utc))

    def test_status_and_refusal_messages_are_classified(self):
        ok = self.protocol.interpret('[{"ev":"status","status":"auth_success",'
                                     '"message":"authenticated"}]')[0]
        self.assertEqual((ok.kind, ok.status), ("status", "auth_success"))
        refused = self.protocol.interpret(
            '[{"ev":"status","status":"auth_failed","message":"invalid api key"}]')[0]
        self.assertEqual((refused.kind, refused.status), ("error", "AUTH_FAILED"))
        self.assertIn("invalid api key", refused.message)

    def test_junk_and_malformed_events_are_dropped_not_invented(self):
        self.assertEqual(self.protocol.interpret("not json")[0].kind, "ignored")
        missing_price = self.protocol.interpret('[{"ev":"T","sym":"AAPL","t":1536036818784}]')[0]
        self.assertEqual(missing_price.kind, "ignored")
        self.assertIsNone(missing_price.quote)
        other = self.protocol.interpret('[{"ev":"Q","sym":"AAPL","bp":1}]')[0]
        self.assertEqual(other.kind, "ignored")


class AlpacaProtocolTests(unittest.TestCase):
    def setUp(self):
        self.protocol = AlpacaStreamProtocol()

    def test_handshake_headers_authenticate_without_a_secret_frame(self):
        settings = alpaca_settings("wss://example.invalid/v2/iex")
        headers = self.protocol.headers(settings)
        self.assertEqual(headers["APCA-API-KEY-ID"], API_KEY)
        self.assertEqual(headers["APCA-API-SECRET-KEY"], "stream-secret-do-not-leak")
        self.assertIsNone(self.protocol.auth_message(settings))
        self.assertEqual(json.loads(self.protocol.subscribe_message(["AAPL"])),
                         {"action": "subscribe", "trades": ["AAPL"]})

    def test_the_selected_feed_is_named_so_recency_is_never_assumed(self):
        self.assertIn("IEX", AlpacaStreamProtocol("iex").entitlement)
        self.assertIn("delayed", AlpacaStreamProtocol("delayed_sip").entitlement)
        with self.assertRaises(ValueError):
            AlpacaStreamProtocol("black-box")

    def test_welcome_and_subscription_events_are_recognized(self):
        events = self.protocol.interpret('[{"T":"success","msg":"connected"}]')
        self.assertEqual((events[0].kind, events[0].status), ("status", "connected"))
        ack = self.protocol.interpret('[{"T":"subscription","trades":["AAPL"]}]')[0]
        self.assertEqual((ack.kind, ack.status), ("status", "subscribed"))

    def test_a_trade_uses_the_rfc3339_nanosecond_timestamp_verbatim(self):
        events = self.protocol.interpret(
            '[{"T":"t","sym":"aapl","p":171.29,"s":172,'
            '"t":"2023-09-29T19:59:59.246196362Z"}]')
        quote = events[0].quote
        self.assertEqual(quote.symbol, "AAPL")
        self.assertEqual(quote.price, 171.29)
        self.assertEqual(quote.timestamp.microsecond, 246196)
        self.assertEqual(quote.timestamp.tzinfo, timezone.utc)

    def test_provider_errors_map_to_the_honest_failure_state(self):
        auth = self.protocol.interpret('[{"T":"error","code":40110000,'
                                       '"msg":"request is not authorized"}]')[0]
        self.assertEqual(auth.status, "AUTH_FAILED")
        subscription = self.protocol.interpret(
            '[{"T":"error","code":4006,"msg":"subscription limit exceeded"}]')[0]
        self.assertEqual(subscription.status, "SUBSCRIPTION_FAILED")


# -- stream against a real loopback socket ----------------------------------
class StreamSessionTests(unittest.TestCase):
    def _polygon_server(self, *, drop_after=None, refuse_auth=False,
                        refuse_subscription=False, malformed=False):
        def handler(conn):
            auth = conn.recv_json()
            if auth.get("action") != "auth":
                raise AssertionError(f"expected an auth frame, received {auth}")
            if refuse_auth:
                conn.send(json.dumps([{"ev": "status", "status": "auth_failed",
                                       "message": "invalid api key"}]))
                return
            conn.send(json.dumps([{"ev": "status", "status": "auth_success",
                                   "message": "authenticated"}]))
            subscribe = conn.recv_json()
            if subscribe.get("action") != "subscribe":
                raise AssertionError(f"expected a subscribe frame, received {subscribe}")
            if refuse_subscription:
                conn.send(json.dumps([{"ev": "status", "status": "failed",
                                       "message": "subscription rejected: not entitled"}]))
                return
            conn.send(json.dumps([{"ev": "status", "status": "subscribed"}]))
            if malformed:
                conn.send("this is not json")
                conn.send(json.dumps([{"ev": "T", "sym": "AAPL", "t": 1536036818784}]))
            sent = 0
            while True:
                sent += 1
                conn.send(json.dumps([{"ev": "T", "sym": "AAPL",
                                       "p": 100.0 + sent, "s": 10,
                                       "t": 1700000000000 + sent * 1000}]))
                if drop_after is not None and sent >= drop_after:
                    conn.drop()
                    return
                time.sleep(0.02)
        return WebSocketTestServer(handler)

    def _stream(self, settings, symbols=("AAPL", "MSFT")):
        return MarketDataStream(protocol=PolygonStreamProtocol(),
                                settings=settings, symbols=symbols,
                                stale_after_seconds=300.0, backoff_max_seconds=0.05)

    def test_a_successful_handshake_reaches_connected_and_caches_live_quotes(self):
        server = self._polygon_server()
        server.start()
        stream = self._stream(polygon_settings(server.url))
        try:
            stream.start()
            self.assertTrue(wait_for(lambda: stream.snapshot()["state"] == STATE_CONNECTED),
                            f"stream never connected: {stream.snapshot()}")
            snapshot = stream.snapshot()
            self.assertTrue(snapshot["streaming"])
            self.assertTrue(snapshot["authenticated"])
            self.assertIn("AAPL", snapshot["symbols"])
            self.assertTrue(wait_for(lambda: stream.latest(["AAPL"]) != {}))
            quote = stream.latest(["AAPL"])["AAPL"]
            # The provider's own event time drives freshness, not socket arrival.
            self.assertEqual(quote.timestamp.tzinfo, timezone.utc)
            self.assertEqual(quote.timestamp,
                             datetime(2023, 11, 14, 22, 13, 21, tzinfo=timezone.utc))
            self.assertGreaterEqual(snapshot["attempts"], 1)
            self.assertGreaterEqual(stream.snapshot()["quotes_received"], 1)
            self.assertIsNone(stream.snapshot()["reason"])
        finally:
            stream.stop()
            server.stop()

    def test_a_symbol_with_no_event_is_absent_never_invented(self):
        server = self._polygon_server()
        server.start()
        stream = self._stream(polygon_settings(server.url))
        try:
            stream.start()
            self.assertTrue(wait_for(lambda: bool(stream.latest(["AAPL"]))))
            self.assertEqual(stream.latest(["MSFT"]), {})
        finally:
            stream.stop()
            server.stop()

    def test_malformed_events_are_counted_and_do_not_end_the_session(self):
        server = self._polygon_server(malformed=True)
        server.start()
        stream = self._stream(polygon_settings(server.url))
        try:
            stream.start()
            self.assertTrue(wait_for(lambda: stream.snapshot()["ignored_messages"] >= 1),
                            stream.snapshot())
            self.assertTrue(wait_for(lambda: stream.snapshot()["state"] == STATE_CONNECTED))
        finally:
            stream.stop()
            server.stop()

    def test_an_abrupt_drop_is_reconnected_with_backoff(self):
        server = self._polygon_server(drop_after=3)
        server.start()
        stream = self._stream(polygon_settings(server.url))
        try:
            stream.start()
            self.assertTrue(wait_for(lambda: stream.snapshot()["reconnects"] >= 2),
                            f"expected reconnects: {stream.snapshot()}")
            snapshot = stream.snapshot()
            self.assertGreaterEqual(snapshot["attempts"], 3)
            self.assertIn(snapshot["state"], (STATE_CONNECTED, "RECONNECTING"))
            self.assertTrue(wait_for(lambda: stream.snapshot()["state"] == STATE_CONNECTED),
                            stream.snapshot())
        finally:
            stream.stop()
            server.stop()

    def test_a_refused_authentication_is_reported_as_auth_failed_with_the_reason(self):
        server = self._polygon_server(refuse_auth=True)
        server.start()
        stream = self._stream(polygon_settings(server.url))
        try:
            stream.start()
            self.assertTrue(wait_for(lambda: stream.snapshot()["state"] == STATE_AUTH_FAILED),
                            stream.snapshot())
            snapshot = stream.snapshot()
            self.assertIn("invalid api key", snapshot["reason"])
            self.assertFalse(snapshot["streaming"])
            self.assertEqual(stream.latest(), {})
        finally:
            stream.stop()
            server.stop()

    def test_a_refused_subscription_is_reported_with_the_provider_message(self):
        server = self._polygon_server(refuse_subscription=True)
        server.start()
        stream = self._stream(polygon_settings(server.url))
        try:
            stream.start()
            self.assertTrue(
                wait_for(lambda: stream.snapshot()["state"] == STATE_SUBSCRIPTION_FAILED),
                stream.snapshot())
            self.assertIn("not entitled", stream.snapshot()["reason"])
        finally:
            stream.stop()
            server.stop()

    def test_an_alpaca_stream_authenticates_with_handshake_headers(self):
        def handler(conn):
            conn.send(json.dumps([{"T": "success", "msg": "connected"}]))
            conn.send(json.dumps([{"T": "success", "msg": "authenticated"}]))
            subscribe = conn.recv_json()
            if subscribe != {"action": "subscribe", "trades": ["AAPL"]}:
                raise AssertionError(f"unexpected subscribe frame: {subscribe}")
            conn.send(json.dumps([{"T": "subscription", "trades": ["AAPL"]}]))
            sent = 0
            while True:
                sent += 1
                conn.send(json.dumps([{"T": "t", "sym": "AAPL", "p": 50.5 + sent,
                                       "s": 5, "t": "2026-10-12T14:30:00.123456789Z"}]))
                time.sleep(0.02)

        with WebSocketTestServer(handler) as server:
            stream = MarketDataStream(protocol=AlpacaStreamProtocol(),
                                      settings=alpaca_settings(server.url),
                                      symbols=("AAPL",), backoff_max_seconds=0.05)
            try:
                stream.start()
                self.assertTrue(wait_for(lambda: stream.snapshot()["state"] == STATE_CONNECTED),
                                stream.snapshot())
                headers = server.handshake_headers[0] if server.handshake_headers else {}
                self.assertEqual(headers.get("apca-api-key-id"), API_KEY)
                self.assertTrue(wait_for(lambda: bool(stream.latest(["AAPL"]))))
                self.assertIn("IEX", stream.snapshot()["entitlement"])
            finally:
                stream.stop()

    def test_the_snapshot_never_contains_a_credential(self):
        server = self._polygon_server()
        server.start()
        stream = self._stream(polygon_settings(server.url))
        try:
            stream.start()
            self.assertTrue(wait_for(lambda: stream.snapshot()["state"] == STATE_CONNECTED))
            body = json.dumps(stream.snapshot())
            self.assertNotIn(API_KEY, body)
        finally:
            stream.stop()
            server.stop()

    def test_stop_parks_the_stream_in_a_stopped_state(self):
        server = self._polygon_server()
        server.start()
        stream = self._stream(polygon_settings(server.url))
        stream.start()
        self.assertTrue(wait_for(lambda: stream.snapshot()["state"] == STATE_CONNECTED))
        stream.stop()
        snapshot = stream.snapshot()
        self.assertEqual(snapshot["state"], "STOPPED")
        self.assertFalse(snapshot["streaming"])
        server.stop()


# -- service integration ----------------------------------------------------
class StreamFeedsTheDataServiceTests(unittest.TestCase):
    def test_get_latest_prefers_a_live_stream_quote_and_fills_the_rest_from_rest(self):
        from backend.market_data.models import Quote
        from backend.market_data.stream import StreamEvent

        settings = polygon_settings("ws://127.0.0.1:1/stocks")
        provider = MockMarketDataProvider()
        stream = MarketDataStream(protocol=PolygonStreamProtocol(), settings=settings,
                                  symbols=("AAPL", "MSFT"), backoff_max_seconds=0.05)
        service = MarketDataService(provider, stream=stream)
        # One cached live quote, injected without opening a socket.
        stream._apply(StreamEvent("quote", quote=Quote(
            "AAPL", datetime(2026, 10, 12, 14, 30, tzinfo=timezone.utc), 102.5, 10)))
        quotes = {quote.symbol: quote for quote in service.get_latest(["AAPL", "MSFT"])}
        self.assertEqual(quotes["AAPL"].price, 102.5)   # the live event wins
        self.assertEqual(quotes["MSFT"].price, 203.0)   # REST fills the gap
        self.assertEqual(stream.latest(["MSFT"]), {})    # and nothing was invented

    def test_without_a_stream_the_service_behaves_exactly_like_the_rest_path(self):
        provider = MockMarketDataProvider()
        service = MarketDataService(provider)
        quotes = service.get_latest(["AAPL"])
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0].symbol, "AAPL")
        self.assertIsNone(service.stream)

    def test_configured_stream_is_built_but_never_started_by_the_runtime(self):
        config = load_config(env={"MARKET_DATA_PROVIDER": "polygon",
                                  "POLYGON_API_KEY": API_KEY})
        from backend.core.runtime import ApplicationRuntime
        runtime = ApplicationRuntime(database_path=":memory:", owner_token="owner-test",
                                     config=config, market_data_provider=None)
        try:
            self.assertIsNotNone(runtime.market_stream)
            self.assertFalse(runtime.market_stream.running)
            status = runtime.market_stream_status()
            self.assertEqual(status["state"], STATE_READY)
            self.assertFalse(status["streaming"])
            self.assertIn("not been started", status["reason"])
            body = json.dumps(runtime.status(), default=str)
            self.assertNotIn(API_KEY, body)
        finally:
            runtime.close()


class StreamConfigurationTests(unittest.TestCase):
    def test_states_are_honest_about_what_is_and_is_not_possible(self):
        off = stream_configuration(MarketDataSettings(provider="polygon", api_key="k",
                                                      stream=False))
        self.assertEqual(off["state"], STATE_OFF)
        self.assertIn("disabled", off["reason"])

        unconfigured = stream_configuration(MarketDataSettings())
        self.assertEqual(unconfigured["state"], STATE_NOT_CONFIGURED)

        unsupported = stream_configuration(
            MarketDataSettings(provider="alphavantage", api_key="k"))
        self.assertEqual(unsupported["state"], STATE_NOT_CONFIGURED)
        self.assertIn("no streaming adapter", unsupported["reason"])

        missing_secret = MarketDataSettings(provider="alpaca", api_key="k")
        self.assertEqual(stream_configuration(missing_secret)["state"], STATE_NOT_CONFIGURED)

        ready = stream_configuration(MarketDataSettings(provider="alpaca", api_key="k",
                                                        api_secret="s"))
        self.assertEqual(ready["state"], STATE_READY)
        self.assertTrue(ready["entitlement"])

    def test_a_bad_feed_value_is_reported_instead_of_starting_a_socket(self):
        settings = MarketDataSettings(provider="alpaca", api_key="k", api_secret="s",
                                      stream_feed="wat")
        report = stream_configuration(settings)
        self.assertNotEqual(report["state"], STATE_READY)
        self.assertIn("MARKET_DATA_STREAM_FEED", report["reason"])

    def test_build_market_stream_returns_none_unless_every_precondition_holds(self):
        self.assertIsNone(build_market_stream(MarketDataSettings(), symbols=("AAPL",)))
        self.assertIsNone(build_market_stream(
            MarketDataSettings(provider="polygon", stream=False), symbols=("AAPL",)))
        built = build_market_stream(
            MarketDataSettings(provider="polygon", api_key="k"), symbols=("AAPL",))
        try:
            self.assertIsNotNone(built)
            self.assertFalse(built.running)
        finally:
            built.stop()


if __name__ == "__main__":
    unittest.main()
