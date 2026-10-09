"""Phase 10 checkpoint 4 proof: an owner can close an open paper position.

CP4 exists because the only close paths were a TradingView ``EXIT`` alert and
an automatic stop/target exit - and with no market-data provider attached
(the configuration the committed smoke runs in) neither can fire. These tests
hold the guarantees the checkpoint promises:

- the exit price is always supplied by the caller; it is never defaulted,
  marked or derived from the entry, because a fabricated exit price would be
  a fabricated P&L;
- an impossible price (absent, zero, negative, non-numeric, NaN) and an
  unknown or already-closed symbol are refused without touching state;
- a successful close runs the same slippage, fee, journal and trade-close
  accounting as an automatic or signal exit, exactly once;
- the HTTP surface keeps the owner-token guard: 401 without it, 503 when
  owner controls are disabled, 400/404 for the refusals above, 200 only for a
  close that actually happened;
- the response is labelled paper execution and carries no credential.
"""
import json
import threading
import unittest
from datetime import datetime, timezone
from urllib import request as urlrequest
from urllib.error import HTTPError

from backend.config import load_config
from backend.core.runtime import ApplicationRuntime
from backend.core.server import create_server
from backend.tradingview.auth import TradingViewAuthenticator

CLOCK = datetime(2024, 1, 2, 15, 30, tzinfo=timezone.utc)
SECRET = "cp4-webhook-secret-never-echo-this"
OWNER_TOKEN = "cp4-owner-token"


def build_runtime(*, owner_token=OWNER_TOKEN, database_path=":memory:"):
    config = load_config(env={
        "TRADINGVIEW_ENABLED": "true",
        "TRADINGVIEW_WEBHOOK_SECRET": SECRET,
        **({"OWNER_CONTROL_TOKEN": owner_token} if owner_token else {}),
    })
    return ApplicationRuntime(database_path=database_path,
                              owner_token=owner_token,
                              clock=lambda: CLOCK, config=config)


def alert(**overrides) -> dict:
    body = {
        "action": "buy",
        "symbol": "AAPL",
        "quantity": 5,
        "price": 50.0,
        "stop_loss": 48.0,
        "take_profit": 54.0,
        "timestamp": "2024-01-02T15:29:00Z",
        "strategy": "tv_ema_cross",
        "alert_id": "cp4-alert-1",
    }
    body.update(overrides)
    return {key: value for key, value in body.items() if value is not None}


def open_position(runtime, **overrides) -> dict:
    """Open one paper position through the real signed webhook path."""
    raw = json.dumps(alert(**overrides)).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": "sha256="
        + TradingViewAuthenticator(SECRET).signature(raw),
    }
    response = runtime.tradingview.handle(raw, headers)
    if response.status != 202:
        raise AssertionError(f"setup failed: {response.status} {response.body}")
    return response.body


class OwnerCloseTests(unittest.TestCase):
    def setUp(self):
        self.runtime = build_runtime()
        open_position(self.runtime)
        self.position = next(item for item in self.runtime.portfolio.positions
                             if item.symbol == "AAPL")

    def tearDown(self):
        self.runtime.close()

    def expected_pnl(self, requested: float) -> float:
        config = self.runtime.paper.config
        fill = requested - config.slippage_per_share
        fees = self.position.quantity * config.commission_per_share
        return ((fill - self.position.average_entry_price)
                * self.position.quantity - fees)

    def test_owner_close_closes_the_position_and_books_the_real_pnl(self):
        expected = self.expected_pnl(51.0)
        outcome = self.runtime.close_position("AAPL", 51.0)
        self.assertTrue(outcome["closed"])
        self.assertEqual(outcome["symbol"], "AAPL")
        self.assertEqual(outcome["quantity"], self.position.quantity)
        self.assertEqual(outcome["direction"], "LONG")
        self.assertEqual(outcome["requested_price"], 51.0)
        self.assertEqual(outcome["execution"], "PAPER")
        self.assertEqual(outcome["reason"], "MANUAL")
        self.assertAlmostEqual(outcome["realized_pnl"], expected, places=9)
        self.assertEqual(list(self.runtime.portfolio.positions), [])
        self.assertAlmostEqual(self.runtime.portfolio.realized_pnl,
                               expected, places=9)

    def test_the_close_is_journaled_exactly_once_and_the_order_stays_filled(self):
        self.runtime.close_position("AAPL", 51.0)
        events = [row for row in self.runtime.repository.rows("position_events")
                  if row["event_type"] == "CLOSED"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["context_json"]["reason"], "MANUAL")
        trades = self.runtime.repository.rows("trades")
        self.assertEqual(len(trades), 1)
        self.assertIsNotNone(trades[0]["exit_timestamp"])
        self.assertAlmostEqual(trades[0]["pnl"],
                               self.expected_pnl(51.0), places=9)
        orders = self.runtime.repository.rows("orders")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "FILLED",
                         "a close never rewrites the entry order")

    def test_close_never_accepts_an_impossible_price(self):
        for price in (None, 0, -1, "51", True, float("nan"), float("inf")):
            with self.subTest(price=price):
                outcome = self.runtime.close_position("AAPL", price)
                self.assertFalse(outcome["closed"])
                self.assertEqual(outcome["code"], "INVALID_PRICE")
                self.assertEqual(
                    [item.symbol for item in self.runtime.portfolio.positions],
                    ["AAPL"], "a refused close changes nothing")

    def test_close_requires_a_symbol(self):
        outcome = self.runtime.close_position("", 51.0)
        self.assertFalse(outcome["closed"])
        self.assertEqual(outcome["code"], "INVALID_SYMBOL")

    def test_unknown_and_already_closed_positions_are_not_found(self):
        missing = self.runtime.close_position("MSFT", 51.0)
        self.assertFalse(missing["closed"])
        self.assertEqual(missing["code"], "POSITION_NOT_FOUND")
        self.runtime.close_position("AAPL", 51.0)
        again = self.runtime.close_position("AAPL", 51.0)
        self.assertFalse(again["closed"])
        self.assertEqual(again["code"], "POSITION_NOT_FOUND",
                         "a second close is never reported as success")

    def test_the_close_response_carries_no_credential(self):
        outcome = self.runtime.close_position("AAPL", 51.0)
        serialized = json.dumps(outcome)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn(OWNER_TOKEN, serialized)


class HttpPositionCloseTests(unittest.TestCase):
    def setUp(self):
        self.runtime = build_runtime()
        open_position(self.runtime)
        self.server = create_server(self.runtime, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.runtime.close()

    def post(self, path, body, token=OWNER_TOKEN):
        request = urlrequest.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **({"X-Owner-Token": token} if token else {})},
            method="POST")
        try:
            with urlrequest.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def get_json(self, path):
        with urlrequest.urlopen(f"http://127.0.0.1:{self.port}{path}",
                                timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_close_requires_the_owner_token(self):
        status, body = self.post("/api/positions/close",
                                 {"symbol": "AAPL", "price": 51.0}, token=None)
        self.assertEqual(status, 401)
        self.assertEqual(len(self.runtime.portfolio.positions), 1)

    def test_close_without_a_real_price_is_refused(self):
        for body in ({"symbol": "AAPL"}, {"symbol": "AAPL", "price": 0},
                     {"symbol": "AAPL", "price": -5},
                     {"symbol": "AAPL", "price": "51"}, {"price": 51.0}):
            with self.subTest(body=body):
                status, outcome = self.post("/api/positions/close", body)
                self.assertEqual(status, 400)
                self.assertFalse(outcome["closed"])
        self.assertEqual(len(self.runtime.portfolio.positions), 1)

    def test_unknown_symbol_is_not_found(self):
        status, outcome = self.post("/api/positions/close",
                                    {"symbol": "MSFT", "price": 51.0})
        self.assertEqual(status, 404)
        self.assertEqual(outcome["code"], "POSITION_NOT_FOUND")

    def test_successful_close_empties_the_position_list_once(self):
        before = self.get_json("/api/positions")["positions"]
        self.assertEqual(len(before), 1)
        status, outcome = self.post("/api/positions/close",
                                    {"symbol": "AAPL", "price": 51.0})
        self.assertEqual(status, 200)
        self.assertTrue(outcome["closed"])
        self.assertEqual(outcome["execution"], "PAPER")
        self.assertEqual(self.get_json("/api/positions")["positions"], [])
        second = self.post("/api/positions/close",
                           {"symbol": "AAPL", "price": 51.0})
        self.assertEqual(second[0], 404)
        self.assertFalse(second[1]["closed"])
        serialized = json.dumps(outcome)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn(OWNER_TOKEN, serialized)


class OwnerControlsDisabledTests(unittest.TestCase):
    def test_close_is_unavailable_when_owner_controls_are_disabled(self):
        runtime = build_runtime(owner_token=None)
        server = create_server(runtime, "127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            open_position(runtime)
            request = urlrequest.Request(
                f"http://127.0.0.1:{port}/api/positions/close",
                data=json.dumps({"symbol": "AAPL", "price": 51.0}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urlrequest.urlopen(request, timeout=10) as response:
                    status, body = response.status, response.read()
            except HTTPError as error:
                status, body = error.code, error.read()
            self.assertEqual(status, 503)
            self.assertIn(b"owner controls are disabled", body)
            self.assertEqual(len(runtime.portfolio.positions), 1,
                             "an unavailable close never changes state")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            runtime.close()


if __name__ == "__main__":
    unittest.main()
