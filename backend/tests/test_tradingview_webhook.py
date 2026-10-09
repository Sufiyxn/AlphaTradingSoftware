"""Phase 4 - TradingView webhook ingestion tests.

Covers authentication, validation, normalization, idempotency/replay,
risk and paper execution integration, the real-price rule, connection state,
secret redaction, and HTTP behaviour of POST /api/webhooks/tradingview.
"""
import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from urllib import request as urlrequest
from urllib.error import HTTPError

from backend.config import load_config
from backend.core.runtime import ApplicationRuntime
from backend.core.server import _json_default, create_server
from backend.execution.models import Order, OrderStatus
from backend.strategies.models import Direction

CLOCK = datetime(2024, 1, 2, 15, 29, 0, 1, tzinfo=timezone.utc)
SECRET = "tv-webhook-secret-never-echo-this"
OWNER_TOKEN = "owner-test-token"
ENDPOINT = "/api/webhooks/tradingview"


def base_env(**overrides) -> dict:
    env = {
        "TRADINGVIEW_ENABLED": "true",
        "TRADINGVIEW_WEBHOOK_SECRET": SECRET,
        "OWNER_CONTROL_TOKEN": OWNER_TOKEN,
    }
    env.update(overrides)
    return env


def build_runtime(*, env=None, owner_token=OWNER_TOKEN, database_path=":memory:",
                  clock=None, config=None):
    if config is None:
        merged = base_env(**(env or {}))
        if owner_token is None:
            merged.pop("OWNER_CONTROL_TOKEN", None)
        config = load_config(env=merged)
    return ApplicationRuntime(database_path=database_path, owner_token=owner_token,
                              clock=clock or (lambda: CLOCK), config=config)


def payload(**overrides) -> dict:
    alert = {
        "action": "buy",
        "symbol": "AAPL",
        "quantity": 5,
        "price": 50.0,
        "stop_loss": 48.0,
        "take_profit": 54.0,
        "timestamp": "2024-01-02T15:28:00Z",
        "strategy": "tv_ema_cross",
    }
    alert.update(overrides)
    return {key: value for key, value in alert.items() if value is not None}


def encode(alert: dict) -> bytes:
    return json.dumps(alert).encode("utf-8")


def signature(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def signed_headers(body: bytes, secret: str = SECRET) -> dict:
    return {"Content-Type": "application/json", "X-Webhook-Signature": signature(body, secret)}


def send(runtime, alert, *, secret: str = SECRET, headers: dict | None = None,
         raw: bytes | None = None):
    body = raw if raw is not None else encode(alert)
    request_headers = dict(headers) if headers is not None else signed_headers(body, secret)
    return runtime.tradingview.handle(body, request_headers)


def positions(runtime) -> dict:
    return {position.symbol: position for position in runtime.portfolio.positions}


def order_rows(runtime) -> tuple:
    return runtime.repository.rows("orders")


def event_types(runtime) -> list:
    return [event["event_type"] for event in runtime.repository.events()]


class WebhookAuthenticationTests(unittest.TestCase):
    def test_valid_authenticated_signal_is_accepted(self):
        runtime = build_runtime()
        try:
            response = send(runtime, payload())
            self.assertEqual(response.status, 202)
            self.assertEqual(response.body["status"], "accepted")
            self.assertEqual(response.body["code"], "ACCEPTED")
            self.assertEqual(response.body["symbol"], "AAPL")
            self.assertEqual(response.body["action"], "ENTER_LONG")
            self.assertEqual(response.body["quantity"], 5)
            self.assertTrue(response.body["order_id"])
        finally:
            runtime.close()

    def test_payload_shared_secret_is_accepted_without_a_signature_header(self):
        runtime = build_runtime()
        try:
            alert = payload(secret=SECRET)
            response = send(runtime, alert, headers={"Content-Type": "application/json"})
            self.assertEqual(response.status, 202)
            self.assertEqual(response.body["code"], "ACCEPTED")
        finally:
            runtime.close()

    def test_missing_authentication_is_rejected(self):
        runtime = build_runtime()
        try:
            response = send(runtime, payload(), headers={"Content-Type": "application/json"})
            self.assertEqual(response.status, 401)
            self.assertEqual(response.body["code"], "AUTH_FAILED")
            self.assertEqual(response.body["status"], "rejected")
            self.assertEqual(runtime.portfolio.positions, ())
            self.assertEqual(order_rows(runtime), ())
        finally:
            runtime.close()

    def test_invalid_authentication_is_rejected(self):
        runtime = build_runtime()
        try:
            body = encode(payload())
            wrong_signature = {"Content-Type": "application/json",
                               "X-Webhook-Signature": "sha256=" + "0" * 64}
            response = runtime.tradingview.handle(body, wrong_signature)
            self.assertEqual(response.status, 401)
            self.assertEqual(response.body["code"], "AUTH_FAILED")

            wrong_secret = send(runtime, payload(secret="not-the-secret"),
                                headers={"Content-Type": "application/json"})
            self.assertEqual(wrong_secret.status, 401)
            self.assertEqual(wrong_secret.body["code"], "AUTH_FAILED")

            signed_wrong_key = runtime.tradingview.handle(
                body, signed_headers(body, secret="attacker-key"))
            self.assertEqual(signed_wrong_key.status, 401)
            self.assertEqual(runtime.portfolio.positions, ())
        finally:
            runtime.close()

    def test_webhook_uses_its_own_secret_and_not_the_owner_token(self):
        runtime = build_runtime()
        try:
            # Owner credentials must not authenticate the webhook.
            owner_only = {"Content-Type": "application/json",
                          "X-Owner-Token": OWNER_TOKEN}
            response = send(runtime, payload(), headers=owner_only)
            self.assertEqual(response.status, 401)
            # ...and the webhook must not need the owner token at all.
            accepted = send(runtime, payload(symbol="BAC"),
                            headers={"Content-Type": "application/json",
                                     "X-Webhook-Signature": signature(encode(payload(symbol="BAC")))})
            self.assertEqual(accepted.status, 202)
        finally:
            runtime.close()

    def test_disabled_or_unconfigured_webhook_fails_closed(self):
        for overrides in ({"TRADINGVIEW_ENABLED": "false"},
                          {"TRADINGVIEW_WEBHOOK_SECRET": ""}):
            with self.subTest(overrides=overrides):
                runtime = build_runtime(env=overrides)
                try:
                    response = send(runtime, payload())
                    self.assertEqual(response.status, 503)
                    self.assertEqual(response.body["code"], "NOT_CONFIGURED")
                    self.assertEqual(runtime.portfolio.positions, ())
                finally:
                    runtime.close()


class WebhookValidationTests(unittest.TestCase):
    def _rejects(self, runtime, *, raw=None, alert=None, secret=SECRET, headers=None):
        response = send(runtime, alert if alert is not None else payload(),
                        secret=secret, headers=headers, raw=raw)
        self.assertEqual(response.status, 400, response.body)
        self.assertEqual(response.body["status"], "rejected")
        self.assertEqual(response.body["code"], "INVALID_PAYLOAD")
        self.assertEqual(runtime.portfolio.positions, ())
        self.assertEqual(order_rows(runtime), ())
        return response

    def test_malformed_json_is_rejected(self):
        runtime = build_runtime()
        try:
            response = send(runtime, payload(), raw=b"{not-json")
            self.assertEqual(response.status, 400)
            self.assertEqual(response.body["code"], "INVALID_PAYLOAD")
            self.assertEqual(response.body["reason"], "MALFORMED_JSON")
        finally:
            runtime.close()

    def test_non_object_and_empty_bodies_are_rejected(self):
        runtime = build_runtime()
        try:
            array = send(runtime, payload(), raw=b"[1, 2, 3]")
            self.assertEqual(array.status, 400)
            self.assertEqual(array.body["reason"], "INVALID_PAYLOAD")
            empty = send(runtime, payload(), raw=b"")
            self.assertEqual(empty.status, 400)
            self.assertEqual(empty.body["reason"], "EMPTY_BODY")
        finally:
            runtime.close()

    def test_missing_required_fields_are_rejected(self):
        runtime = build_runtime()
        try:
            for broken in (payload(symbol=None), payload(timestamp=None),
                           payload(stop_loss=None), payload(take_profit=None),
                           payload(action=None)):
                with self.subTest(broken={key: value for key, value in broken.items()
                                          if value is None}):
                    response = self._rejects(runtime, alert=broken)
                    self.assertEqual(response.body["reason"], "MISSING_FIELD")
        finally:
            runtime.close()

    def test_invalid_action_is_rejected(self):
        runtime = build_runtime()
        try:
            response = self._rejects(runtime, alert=payload(action="hold"))
            self.assertEqual(response.body["reason"], "UNSUPPORTED_ACTION")
        finally:
            runtime.close()

    def test_invalid_and_unsupported_symbols_are_rejected(self):
        runtime = build_runtime()
        try:
            bad_shape = self._rejects(runtime, alert=payload(symbol="NOT A SYMBOL!"))
            self.assertEqual(bad_shape.body["reason"], "INVALID_SYMBOL")
            outside_universe = self._rejects(runtime, alert=payload(symbol="MSFT"))
            self.assertEqual(outside_universe.body["reason"], "UNSUPPORTED_SYMBOL")
        finally:
            runtime.close()

    def test_stale_invalid_and_future_timestamps_are_rejected(self):
        runtime = build_runtime()
        try:
            stale = self._rejects(runtime, alert=payload(timestamp="2024-01-02T15:20:00Z"))
            self.assertEqual(stale.body["reason"], "STALE_TIMESTAMP")
            invalid = self._rejects(runtime, alert=payload(timestamp="not-a-timestamp"))
            self.assertEqual(invalid.body["reason"], "INVALID_TIMESTAMP")
            future = self._rejects(runtime, alert=payload(timestamp="2024-01-02T15:40:00Z"))
            self.assertEqual(future.body["reason"], "INVALID_TIMESTAMP")
            missing = self._rejects(runtime, alert=payload(timestamp=None))
            self.assertEqual(missing.body["reason"], "MISSING_FIELD")
        finally:
            runtime.close()

    def test_invalid_quantities_and_prices_are_rejected(self):
        runtime = build_runtime()
        try:
            for broken in (payload(quantity=0), payload(quantity=-3),
                           payload(quantity="ten"), payload(quantity=2.5),
                           payload(price=0), payload(stop_loss=-1),
                           payload(take_profit="free")):
                with self.subTest(broken=broken):
                    response = self._rejects(runtime, alert=broken)
                    self.assertIn(response.body["reason"], ("INVALID_PAYLOAD",
                                                            "INVALID_QUANTITY"))
        finally:
            runtime.close()

    def test_exit_signal_without_a_reported_price_is_rejected(self):
        runtime = build_runtime()
        try:
            response = self._rejects(runtime, alert=payload(action="close", price=None))
            self.assertEqual(response.body["reason"], "MISSING_FIELD")
        finally:
            runtime.close()

    def test_oversized_payload_is_rejected(self):
        runtime = build_runtime()
        try:
            oversized = encode(payload(note="x" * 20_000))
            response = send(runtime, payload(), raw=oversized)
            self.assertEqual(response.status, 413)
            self.assertEqual(response.body["code"], "PAYLOAD_TOO_LARGE")
        finally:
            runtime.close()

    def test_disallowed_strategy_is_rejected(self):
        runtime = build_runtime(env={"TRADINGVIEW_ALLOWED_STRATEGIES": "tv_ema_cross"})
        try:
            blocked = self._rejects(runtime, alert=payload(strategy="something_else"))
            self.assertEqual(blocked.body["reason"], "UNSUPPORTED_STRATEGY")
            missing = self._rejects(runtime, alert=payload(strategy=None))
            self.assertEqual(missing.body["reason"], "UNSUPPORTED_STRATEGY")
            allowed = send(runtime, payload(strategy="tv_ema_cross"))
            self.assertEqual(allowed.status, 202)
        finally:
            runtime.close()

    def test_entry_above_the_price_ceiling_is_rejected(self):
        runtime = build_runtime()
        try:
            response = self._rejects(runtime, alert=payload(
                price=150.0, stop_loss=148.0, take_profit=154.0))
            self.assertEqual(response.body["reason"], "PRICE_LIMIT_EXCEEDED")
        finally:
            runtime.close()


class WebhookTradingTests(unittest.TestCase):
    def test_accepted_signal_reaches_the_paper_workflow(self):
        runtime = build_runtime()
        try:
            response = send(runtime, payload())
            self.assertEqual(response.status, 202)
            self.assertEqual(len(runtime.portfolio.positions), 1)
            position = positions(runtime)["AAPL"]
            self.assertEqual(position.direction, Direction.LONG)
            self.assertEqual(position.quantity, 5)
            self.assertAlmostEqual(position.average_entry_price, 50.0, delta=0.05)
            self.assertEqual(len(runtime.paper.trades), 1)
            self.assertEqual(len(order_rows(runtime)), 1)
            self.assertIn("TV_SIGNAL_RECEIVED", event_types(runtime))
            decisions = runtime.repository.rows("risk_decisions")
            self.assertEqual(len(decisions), 1)
            self.assertTrue(decisions[0]["approved"])
            self.assertIn("SIGNAL_PROCESSED",
                          [event["event_type"] for event in runtime.repository.events()])
        finally:
            runtime.close()

    def test_risk_decision_quantity_is_never_widened_by_the_alert(self):
        runtime = build_runtime()
        try:
            response = send(runtime, payload(quantity=2))
            self.assertEqual(response.status, 202)
            position = positions(runtime)["AAPL"]
            self.assertEqual(position.quantity, 2)
            self.assertGreaterEqual(response.body["risk_approved_quantity"],
                                    response.body["quantity"])
        finally:
            runtime.close()

    def test_signal_without_quantity_falls_back_to_risk_sizing(self):
        runtime = build_runtime()
        try:
            response = send(runtime, payload(quantity=None))
            self.assertEqual(response.status, 202, response.body)
            position = positions(runtime)["AAPL"]
            self.assertEqual(position.quantity, response.body["risk_approved_quantity"])
            self.assertGreater(position.quantity, 0)
        finally:
            runtime.close()

    def test_duplicate_delivery_creates_no_second_order(self):
        runtime = build_runtime()
        try:
            first = send(runtime, payload())
            second = send(runtime, payload())
            self.assertEqual(first.status, 202)
            self.assertEqual(second.status, 409)
            self.assertEqual(second.body["code"], "DUPLICATE_SIGNAL")
            self.assertEqual(second.body["status"], "duplicate")
            self.assertEqual(len(runtime.portfolio.positions), 1)
            self.assertEqual(len(order_rows(runtime)), 1)
            self.assertEqual(len(runtime.paper.trades), 1)
        finally:
            runtime.close()

    def test_replayed_event_id_is_detected_even_when_the_body_differs(self):
        runtime = build_runtime()
        try:
            first = send(runtime, payload(alert_id="alert-42", quantity=5))
            replay = send(runtime, payload(alert_id="alert-42", quantity=10))
            self.assertEqual(first.status, 202)
            self.assertEqual(replay.status, 409)
            self.assertEqual(replay.body["code"], "DUPLICATE_SIGNAL")
            self.assertEqual(len(order_rows(runtime)), 1)
            self.assertEqual(len(runtime.portfolio.positions), 1)
        finally:
            runtime.close()

    def test_replay_is_blocked_after_a_restart_through_the_journal(self):
        database = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3")
        database.close()
        os.unlink(database.name)
        try:
            first_runtime = build_runtime(database_path=database.name)
            try:
                self.assertEqual(send(first_runtime, payload()).status, 202)
            finally:
                first_runtime.close()

            second_runtime = build_runtime(database_path=database.name)
            try:
                replay = send(second_runtime, payload())
                self.assertEqual(replay.status, 409)
                self.assertEqual(replay.body["code"], "DUPLICATE_SIGNAL")
                self.assertEqual(len(second_runtime.portfolio.positions), 1)
                self.assertEqual(len(order_rows(second_runtime)), 1)
            finally:
                second_runtime.close()
        finally:
            if os.path.exists(database.name):
                os.unlink(database.name)

    def test_the_durable_fill_row_names_the_source_that_raised_it(self):
        runtime = build_runtime()
        try:
            response = send(runtime, payload(alert_id="fill-evidence"))
            self.assertEqual(response.status, 202, response.body)

            fills = runtime.repository.rows("fills")
            self.assertEqual(len(fills), 1, "the execution is durable in its own row")
            row = fills[0]
            self.assertEqual(row["symbol"], "AAPL")
            self.assertEqual(row["quantity"], positions(runtime)["AAPL"].quantity,
                             "the position holds exactly what the row records")
            context = row["context_json"]
            self.assertEqual(context["source"], "tradingview")
            self.assertTrue(context["signal_id"],
                            "the signal that raised the fill is recorded with it")
            self.assertEqual(context["order_id"], row["client_order_id"])
        finally:
            runtime.close()

    def test_risk_rejection_does_not_execute(self):
        runtime = build_runtime()
        try:
            response = send(runtime, payload(take_profit=52.0))
            self.assertEqual(response.status, 422)
            self.assertEqual(response.body["code"], "RISK_REJECTED")
            self.assertEqual(response.body["status"], "rejected")
            self.assertTrue(response.body["reasons"])
            self.assertEqual(runtime.portfolio.positions, ())
            self.assertEqual(order_rows(runtime), ())
            self.assertEqual(len(runtime.paper.trades), 0)
            self.assertIn("TV_SIGNAL_REJECTED", event_types(runtime))
        finally:
            runtime.close()

    def test_kill_switch_blocks_execution(self):
        runtime = build_runtime()
        try:
            runtime.risk.set_kill_switch(True)
            response = send(runtime, payload())
            self.assertEqual(response.status, 422)
            self.assertEqual(response.body["code"], "RISK_REJECTED")
            self.assertIn("kill switch enabled", response.body["reasons"])
            self.assertEqual(runtime.portfolio.positions, ())
            self.assertEqual(order_rows(runtime), ())
        finally:
            runtime.close()

    def test_duplicate_symbol_position_is_rejected_by_risk(self):
        runtime = build_runtime()
        try:
            self.assertEqual(send(runtime, payload()).status, 202)
            second = send(runtime, payload(alert_id="second-alert"))
            self.assertEqual(second.status, 422)
            self.assertEqual(second.body["code"], "RISK_REJECTED")
            self.assertEqual(len(runtime.portfolio.positions), 1)
            self.assertEqual(len(order_rows(runtime)), 1)
        finally:
            runtime.close()

    def test_unavailable_execution_reports_error_and_creates_no_position(self):
        runtime = build_runtime()

        class RejectingExecution:
            def place_order(self, request):
                return Order("PAPER-REJECTED-000001", request, OrderStatus.REJECTED,
                             0, None, CLOCK, (), rejection_reason="execution disabled")

        runtime.paper.execution = RejectingExecution()
        try:
            response = send(runtime, payload())
            self.assertEqual(response.status, 503)
            self.assertEqual(response.body["code"], "EXECUTION_UNAVAILABLE")
            self.assertEqual(response.body["status"], "error")
            self.assertEqual(runtime.portfolio.positions, ())
        finally:
            runtime.close()

    def test_internal_failure_returns_a_machine_readable_error(self):
        runtime = build_runtime()

        def explode(*args, **kwargs):
            raise RuntimeError("paper engine unavailable")

        runtime.paper.submit_decision = explode
        try:
            response = send(runtime, payload())
            self.assertEqual(response.status, 500)
            self.assertEqual(response.body["code"], "INTERNAL_ERROR")
            self.assertEqual(runtime.portfolio.positions, ())
            self.assertNotIn(SECRET, json.dumps(response.body))
        finally:
            runtime.close()

    def test_exit_signal_closes_the_position_with_the_reported_price(self):
        runtime = build_runtime()
        try:
            self.assertEqual(send(runtime, payload()).status, 202)
            exit_signal = payload(action="close", symbol="AAPL", price=52.5,
                                  stop_loss=None, take_profit=None, quantity=None)
            response = send(runtime, exit_signal)
            self.assertEqual(response.status, 202, response.body)
            self.assertEqual(response.body["action"], "EXIT")
            self.assertEqual(response.body["exit_price"], 52.5)
            self.assertEqual(runtime.portfolio.positions, ())
            self.assertEqual(len(runtime.repository.closed_trades()), 1)
        finally:
            runtime.close()

    def test_exit_without_an_open_position_is_rejected(self):
        runtime = build_runtime()
        try:
            response = send(runtime, payload(action="close", price=52.5,
                                             stop_loss=None, take_profit=None))
            self.assertEqual(response.status, 422)
            self.assertEqual(response.body["code"], "NO_OPEN_POSITION")
            self.assertEqual(runtime.portfolio.positions, ())
        finally:
            runtime.close()


class RealPriceRuleTests(unittest.TestCase):
    def test_position_stays_unpriced_without_continuous_market_data(self):
        runtime = build_runtime()
        try:
            availability = runtime.valuation.availability()
            self.assertIn(availability["state"], ("NOT_CONFIGURED", "UNAVAILABLE"))
            response = send(runtime, payload())
            self.assertEqual(response.status, 202)
            position = positions(runtime)["AAPL"]
            self.assertIsNone(position.market_price)
            self.assertIsNone(position.price_as_of)
            self.assertEqual(position.price_state.value, "UNPRICED")
            self.assertIsNone(position.unrealized_pnl)
            self.assertEqual(response.body["position_price_state"], "UNPRICED")
            self.assertIsNone(response.body["position_market_price"])
            snapshot = runtime.portfolio.snapshot()
            self.assertEqual(snapshot.valuation_status, "PARTIAL")
            self.assertEqual(snapshot.unpriced_position_count, 1)
        finally:
            runtime.close()

    def test_alert_price_is_entry_metadata_and_never_an_ongoing_price(self):
        runtime = build_runtime()
        try:
            send(runtime, payload(price=50.0))
            position = positions(runtime)["AAPL"]
            self.assertAlmostEqual(position.average_entry_price, 50.0, delta=0.05)
            self.assertIsNone(position.market_price)
            self.assertEqual(position.price_state.value, "UNPRICED")

            runtime.refresh_market()
            refreshed = positions(runtime)["AAPL"]
            self.assertIsNone(refreshed.market_price)
            self.assertEqual(refreshed.price_state.value, "UNPRICED")

            # A later alert carrying a different price cannot move the mark.
            second = send(runtime, payload(alert_id="price-update", price=61.0,
                                           symbol="BAC"))
            self.assertEqual(second.status, 422)
            still = positions(runtime)["AAPL"]
            self.assertIsNone(still.market_price)
            self.assertEqual(still.price_state.value, "UNPRICED")
        finally:
            runtime.close()

    def test_automatic_exits_do_not_run_from_an_alert_price(self):
        runtime = build_runtime()
        try:
            send(runtime, payload())
            trade = runtime.paper.trades[0]
            self.assertEqual(runtime.paper.on_quote("AAPL", CLOCK, trade.stop_price - 1), ())
            self.assertEqual(len(runtime.paper.trades), 1)
            self.assertEqual(len(runtime.portfolio.positions), 1)
        finally:
            runtime.close()


class ConnectionStateTests(unittest.TestCase):
    def test_unconfigured_state_and_connection_test_are_honest(self):
        runtime = build_runtime(env={"TRADINGVIEW_ENABLED": "false"})
        try:
            section = runtime.connections.status()["tradingview"]
            self.assertEqual(section["state"], "NOT_CONFIGURED")
            self.assertFalse(section["ready"])
            self.assertFalse(section["webhook_available"])
            self.assertIn("TRADINGVIEW_ENABLED", section["reason"])
            result = runtime.connections.test("tradingview")
            self.assertEqual(result["status"], "NOT_CONFIGURED")
            self.assertFalse(result["success"])
        finally:
            runtime.close()

    def test_configured_without_traffic_is_not_reported_as_connected(self):
        runtime = build_runtime()
        try:
            section = runtime.connections.status()["tradingview"]
            self.assertTrue(section["secret_configured"])
            self.assertNotEqual(section["state"], "CONNECTED")
            self.assertEqual(section["state"], "UNAVAILABLE")
            self.assertIn("no TradingView signal has been authenticated yet",
                          section["reason"])
            self.assertEqual(section["signals_accepted"], 0)
            self.assertEqual(section["endpoint_status"], "INSTALLED")
            self.assertTrue(section["webhook_available"])
            result = runtime.connections.test("tradingview")
            self.assertTrue(result["success"])
            self.assertIn("no external request was made", result["message"])
        finally:
            runtime.close()

    def test_accepted_signal_reports_connected_and_counts_traffic(self):
        runtime = build_runtime()
        try:
            self.assertEqual(send(runtime, payload()).status, 202)
            send(runtime, payload(action="hold"))
            section = runtime.connections.status()["tradingview"]
            self.assertEqual(section["state"], "CONNECTED")
            self.assertEqual(section["signals_accepted"], 1)
            self.assertGreaterEqual(section["signals_rejected"], 1)
            self.assertEqual(section["last_received_signal"], CLOCK.isoformat())
            self.assertIsNotNone(section["last_rejected_signal"])
        finally:
            runtime.close()

    def test_connection_state_is_hydrated_from_the_journal_after_restart(self):
        database = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3")
        database.close()
        os.unlink(database.name)
        try:
            first = build_runtime(database_path=database.name)
            try:
                send(first, payload())
            finally:
                first.close()
            later = build_runtime(database_path=database.name,
                                  clock=lambda: CLOCK + timedelta(days=2))
            try:
                section = later.connections.status()["tradingview"]
                self.assertEqual(section["signals_accepted"], 1)
                self.assertEqual(section["last_received_signal"], CLOCK.isoformat())
                self.assertEqual(section["state"], "STALE")
                self.assertIn("seconds", section["reason"])
            finally:
                later.close()
        finally:
            if os.path.exists(database.name):
                os.unlink(database.name)

    def test_secrets_never_appear_in_status_connections_or_journal(self):
        runtime = build_runtime()
        try:
            send(runtime, payload())
            send(runtime, payload(), headers={"Content-Type": "application/json"})
            documents = [
                runtime.connections.status(),
                runtime.status(),
                runtime.config.describe(),
                runtime.connections.readiness(),
            ]
            serialized = json.dumps(documents, default=_json_default)
            self.assertNotIn(SECRET, serialized)
            journal = {
                "events": runtime.repository.events(),
                "errors": runtime.repository.errors(),
                "orders": runtime.repository.rows("orders"),
                "trades": runtime.repository.rows("trades"),
                "risk": runtime.repository.rows("risk_decisions"),
            }
            self.assertNotIn(SECRET, json.dumps(journal, default=_json_default))
        finally:
            runtime.close()


class HttpEndpointTests(unittest.TestCase):
    def setUp(self):
        self.runtime = build_runtime()
        self.server = create_server(self.runtime, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.runtime.close()

    def post(self, path: str, body: bytes, headers: dict | None = None):
        request = urlrequest.Request(f"http://127.0.0.1:{self.port}{path}", data=body,
                                     headers=headers or {}, method="POST")
        try:
            with urlrequest.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def get(self, path: str):
        with urlrequest.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_http_status_and_response_behaviour(self):
        body = encode(payload())
        accepted_status, accepted = self.post(ENDPOINT, body, signed_headers(body))
        self.assertEqual(accepted_status, 202)
        self.assertEqual(accepted["code"], "ACCEPTED")

        duplicate_status, duplicate = self.post(ENDPOINT, body, signed_headers(body))
        self.assertEqual(duplicate_status, 409)
        self.assertEqual(duplicate["code"], "DUPLICATE_SIGNAL")

        unauth_status, unauth = self.post(ENDPOINT, encode(payload(symbol="BAC")),
                                          {"Content-Type": "application/json"})
        self.assertEqual(unauth_status, 401)
        self.assertEqual(unauth["code"], "AUTH_FAILED")

        malformed_status, malformed = self.post(ENDPOINT, b"{oops",
                                                signed_headers(b"{oops"))
        self.assertEqual(malformed_status, 400)
        self.assertEqual(malformed["code"], "INVALID_PAYLOAD")

        rejected_body = encode(payload(alert_id="risk-check", take_profit=52.0))
        risk_status, risk = self.post(ENDPOINT, rejected_body,
                                      signed_headers(rejected_body))
        self.assertEqual(risk_status, 422)
        self.assertEqual(risk["code"], "RISK_REJECTED")
        self.assertTrue(risk["reasons"])

        oversized = encode(payload(note="x" * 20_000))
        large_status, large = self.post(ENDPOINT, oversized, signed_headers(oversized))
        self.assertEqual(large_status, 413)
        self.assertEqual(large["code"], "PAYLOAD_TOO_LARGE")

        self.assertEqual(len(self.runtime.portfolio.positions), 1)
        self.assertEqual(len(order_rows(self.runtime)), 1)

    def test_webhook_never_requires_the_owner_token(self):
        control = urlrequest.Request(f"http://127.0.0.1:{self.port}/api/control/start",
                                     data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urlrequest.urlopen(control, timeout=10) as response:
                control_status = response.status
        except HTTPError as error:
            control_status = error.code
        self.assertEqual(control_status, 401)

        body = encode(payload(symbol="BAC"))
        status, result = self.post(ENDPOINT, body, signed_headers(body))
        self.assertEqual(status, 202)
        self.assertEqual(result["code"], "ACCEPTED")

    def test_http_endpoints_serve_json_and_report_tradingview_state(self):
        for path in ("/api/status", "/api/positions", "/api/readiness", "/api/connections"):
            status, document = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIsInstance(document, dict, path)
        status, connections = self.get("/api/connections")
        self.assertEqual(connections["tradingview"]["state"], "UNAVAILABLE")
        self.assertEqual(connections["tradingview"]["endpoint_status"], "INSTALLED")
        self.assertNotIn(SECRET, json.dumps(connections))

    def test_the_activity_feed_carries_executions_and_risk_decisions(self):
        body = encode(payload(alert_id="activity-evidence"))
        accepted, _ = self.post(ENDPOINT, body, signed_headers(body))
        self.assertEqual(accepted, 202)

        status, document = self.get("/api/activity?limit=50")
        self.assertEqual(status, 200)
        rows = document["events"]
        kinds = {row["record_type"] for row in rows}
        self.assertIn("fill", kinds, "the journal feed shows executions")
        self.assertIn("risk", kinds, "the journal feed shows the risk gate")
        fill = next(row for row in rows if row["record_type"] == "fill")
        self.assertIn("executed", fill["message"])
        self.assertEqual(fill["symbol"], "AAPL")
        risk = next(row for row in rows if row["record_type"] == "risk")
        self.assertIn("APPROVED", risk["message"])
        self.assertNotIn(SECRET, json.dumps(document))

    def test_the_webhook_order_and_its_executions_are_served_after_a_restart(self):
        database = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3")
        database.close()
        os.unlink(database.name)
        try:
            first = build_runtime(database_path=database.name)
            try:
                self.assertEqual(send(first, payload(alert_id="restart-evidence")).status, 202)
            finally:
                first.close()

            second = build_runtime(database_path=database.name)
            server = create_server(second, "127.0.0.1", 0)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def get(path: str) -> dict:
                with urlrequest.urlopen(f"http://127.0.0.1:{port}{path}",
                                        timeout=10) as response:
                    return json.loads(response.read().decode("utf-8"))

            try:
                orders = get("/api/orders")
                self.assertEqual(orders["count"], 1)
                order = orders["orders"][0]
                self.assertEqual(order["status"], "FILLED")
                self.assertEqual(order["symbol"], "AAPL")
                self.assertEqual(order["source"], "tradingview")
                self.assertTrue(order["signal_id"])
                self.assertEqual(order["filled_quantity"], 5)
                self.assertEqual(len(order["fills"]), 1)

                fills = get("/api/logs?kind=fills")
                self.assertTrue(any(row.get("record_type") == "fills" for row in fills),
                                "the durable fill journal is exported over HTTP")

                restored = get("/api/positions")
                self.assertEqual(len(restored["positions"]), 1)
                self.assertEqual(restored["positions"][0]["quantity"], 5)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                second.close()
        finally:
            if os.path.exists(database.name):
                os.unlink(database.name)


if __name__ == "__main__":
    unittest.main()
