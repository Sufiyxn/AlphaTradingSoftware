"""Phase 10 checkpoints 4 and 5: owner exits, including a partial close.

CP4 exists because the only close paths were an automatic stop/target exit
and the owner close endpoint - and with no market-data provider attached
(the configuration the committed smoke runs in) an automatic exit cannot fire.
CP5 adds the scaling half: the same endpoint can reduce an open position
instead of only ending it. These tests hold the guarantees the checkpoints
promise:

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
- the response is labelled paper execution and carries no credential;
- a partial close leaves the remainder at its entry price, moves cash only by
  what it realized, sums (with the final close) to exactly the P&L a full
  close would have booked, keeps the trade row open until the last unit
  leaves, and survives a restart with the right quantity and the right cash.
"""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from urllib import request as urlrequest
from urllib.error import HTTPError

from backend.config import load_config
from backend.core.runtime import ApplicationRuntime
from backend.core.server import create_server
from backend.tests.pipeline_fixture import open_position

CLOCK = datetime(2024, 1, 2, 15, 30, tzinfo=timezone.utc)
OWNER_TOKEN = "cp4-owner-token"


def build_runtime(*, owner_token=OWNER_TOKEN, database_path=":memory:"):
    config = load_config(env={
        **({"OWNER_CONTROL_TOKEN": owner_token} if owner_token else {}),
    })
    return ApplicationRuntime(database_path=database_path,
                              owner_token=owner_token,
                              clock=lambda: CLOCK, config=config)


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


class PartialCloseTests(unittest.TestCase):
    """Checkpoint 5: the same exit path can reduce a position, not just end it."""

    def setUp(self):
        self.runtime = build_runtime()
        open_position(self.runtime)
        self.position = next(item for item in self.runtime.portfolio.positions
                             if item.symbol == "AAPL")
        self.entry = self.position.average_entry_price
        self.config = self.runtime.paper.config

    def tearDown(self):
        self.runtime.close()

    def expected_pnl(self, quantity, requested):
        fill = requested - self.config.slippage_per_share
        return ((fill - self.entry) * quantity
                - quantity * self.config.commission_per_share)

    def test_partial_close_leaves_the_remainder_at_its_entry_price(self):
        outcome = self.runtime.close_position("AAPL", 51.0, 2)
        self.assertTrue(outcome["closed"])
        self.assertTrue(outcome["partial"])
        self.assertEqual(outcome["quantity"], 2)
        self.assertEqual(outcome["remaining"], 3)
        self.assertEqual(outcome["execution"], "PAPER")
        self.assertEqual(len(self.runtime.portfolio.positions), 1)
        remainder = self.runtime.portfolio.positions[0]
        self.assertEqual(remainder.quantity, 3)
        self.assertEqual(remainder.average_entry_price, self.entry,
                         "the remainder keeps the entry it already had")
        self.assertEqual(remainder.direction, self.position.direction)

    def test_partial_closes_sum_to_exactly_the_full_close(self):
        configured_full = self.expected_pnl(self.position.quantity, 51.0)
        total = 0.0
        for chunk in (2, 1, 2):
            outcome = self.runtime.close_position("AAPL", 51.0, chunk)
            self.assertTrue(outcome["closed"])
            total += outcome["realized_pnl"]
        self.assertAlmostEqual(total, configured_full, places=9,
                               msg="stepwise closes must add up to one full close")

        witness = build_runtime()
        try:
            open_position(witness)
            full = witness.close_position("AAPL", 51.0)
            self.assertAlmostEqual(full["realized_pnl"], configured_full, places=9)
        finally:
            witness.close()

    def test_cash_and_realized_pnl_move_only_by_the_realized_amount(self):
        cash_before = self.runtime.portfolio.cash
        realized_before = self.runtime.portfolio.realized_pnl
        outcome = self.runtime.close_position("AAPL", 51.0, 2)
        fill = 51.0 - self.config.slippage_per_share
        proceeds = (2 * fill - 2 * self.config.commission_per_share)
        self.assertAlmostEqual(self.runtime.portfolio.cash - cash_before, proceeds,
                               places=9,
                               msg="a close books its proceeds; the entry cost was "
                                   "already debited when the position opened")
        self.assertAlmostEqual(self.runtime.portfolio.realized_pnl - realized_before,
                               outcome["realized_pnl"], places=9)
        self.assertEqual(self.runtime.portfolio.snapshot().trade_count, 1,
                         "one close operation, even though the trade stays open")

    def test_the_whole_trade_cash_equals_the_pnl_it_realized(self):
        runtime = build_runtime()
        try:
            start = runtime.portfolio.cash
            open_position(runtime)
            total = 0.0
            for chunk in (2, 1, 2):
                step = runtime.close_position("AAPL", 51.0, chunk)
                self.assertTrue(step["closed"])
                total += step["realized_pnl"]
            self.assertEqual(runtime.portfolio.positions, ())
            self.assertAlmostEqual(runtime.portfolio.cash - start, total, places=9,
                                   msg="entry debit plus every step's proceeds must "
                                       "net out to exactly the realized P&L")
            self.assertEqual(runtime.portfolio.snapshot().trade_count, 3,
                             "three close operations: two partials and the final one")
        finally:
            runtime.close()

    def test_over_close_and_impossible_quantities_are_refused(self):
        cash_before = self.runtime.portfolio.cash
        for quantity in (6, 0, -1, 2.5, "2", True, float("nan")):
            with self.subTest(quantity=quantity):
                outcome = self.runtime.close_position("AAPL", 51.0, quantity)
                self.assertFalse(outcome["closed"])
                self.assertEqual(outcome["code"], "INVALID_QUANTITY")
                self.assertEqual(self.runtime.portfolio.positions[0].quantity, 5)
                self.assertEqual(self.runtime.portfolio.cash, cash_before)

    def test_the_trade_row_stays_open_until_the_last_unit_leaves(self):
        self.runtime.close_position("AAPL", 51.0, 2)
        open_rows = self.runtime.repository.open_trades()
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["quantity"], 3,
                         "the row carries what the position still holds")
        self.assertIsNone(open_rows[0]["exit_timestamp"])
        details = open_rows[0]["context_json"]
        self.assertEqual(len(details["partials"]), 1)
        self.assertEqual(details["closed_quantity"], 2)
        self.assertAlmostEqual(details["realized_pnl"],
                               self.expected_pnl(2, 51.0), places=9)
        events = self.runtime.repository.rows("position_events")
        self.assertEqual([row["event_type"] for row in events
                          if row["event_type"] != "OPENED"], ["PARTIALLY_CLOSED"])
        self.assertEqual(self.runtime.repository.closed_trades(), ())

        final = self.runtime.close_position("AAPL", 51.0, 3)
        self.assertTrue(final["closed"])
        self.assertFalse(final["partial"])
        closed_rows = self.runtime.repository.closed_trades()
        self.assertEqual(len(closed_rows), 1)
        self.assertEqual(closed_rows[0]["quantity"], 5,
                         "the closed row reports the whole trade, not the last chunk")
        self.assertAlmostEqual(
            closed_rows[0]["pnl"],
            self.expected_pnl(2, 51.0) + self.expected_pnl(3, 51.0), places=9)
        events = self.runtime.repository.rows("position_events")
        self.assertEqual([row["event_type"] for row in events
                          if row["event_type"] != "OPENED"],
                         ["PARTIALLY_CLOSED", "CLOSED"])
        order = self.runtime.repository.rows("orders")[0]
        self.assertEqual(order["status"], "FILLED",
                         "a close never rewrites the entry order")
        self.assertEqual(self.runtime.repository.open_trades(), ())

    def test_a_restart_restores_the_remainder_and_the_partials_cash(self):
        database = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        database.close()
        os.unlink(database.name)
        try:
            first = build_runtime(database_path=database.name)
            try:
                open_position(first)
                outcome = first.close_position("AAPL", 51.0, 2)
                self.assertTrue(outcome["closed"])
                expected = {
                    "cash": first.portfolio.cash,
                    "realized": first.portfolio.realized_pnl,
                    "daily": first.portfolio.daily_pnl,
                    "trade_count": first.portfolio.snapshot().trade_count,
                    "entry": first.portfolio.positions[0].average_entry_price,
                }
            finally:
                first.close()

            second = build_runtime(database_path=database.name)
            try:
                self.assertEqual(len(second.portfolio.positions), 1)
                remainder = second.portfolio.positions[0]
                self.assertEqual(remainder.quantity, 3)
                self.assertEqual(remainder.average_entry_price, expected["entry"])
                self.assertAlmostEqual(second.portfolio.cash, expected["cash"], places=6,
                                       msg="the partial close's cash must survive a restart")
                self.assertAlmostEqual(second.portfolio.realized_pnl,
                                       expected["realized"], places=6)
                self.assertAlmostEqual(second.portfolio.daily_pnl,
                                       expected["daily"], places=6)
                self.assertEqual(second.portfolio.snapshot().trade_count,
                                 expected["trade_count"])
                self.assertEqual(len(second.repository.open_trades()), 1)
                # The remainder still exits for the full amount it holds.
                final = second.close_position("AAPL", 52.0)
                self.assertEqual(final["quantity"], 3)
                self.assertFalse(final["partial"])
                self.assertEqual(second.repository.closed_trades()[0]["quantity"], 5)
            finally:
                second.close()
        finally:
            if os.path.exists(database.name):
                os.unlink(database.name)


class HttpPartialCloseTests(unittest.TestCase):
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

    def post(self, body):
        request = urlrequest.Request(
            f"http://127.0.0.1:{self.port}/api/positions/close",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Owner-Token": OWNER_TOKEN},
            method="POST")
        try:
            with urlrequest.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def get_positions(self):
        with urlrequest.urlopen(f"http://127.0.0.1:{self.port}/api/positions",
                                timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_partial_close_reports_the_remainder_over_http(self):
        status, outcome = self.post({"symbol": "AAPL", "price": 51.0, "quantity": 2})
        self.assertEqual(status, 200)
        self.assertTrue(outcome["closed"])
        self.assertTrue(outcome["partial"])
        self.assertEqual(outcome["quantity"], 2)
        self.assertEqual(outcome["remaining"], 3)
        self.assertEqual(outcome["execution"], "PAPER")
        positions = self.get_positions()["positions"]
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["quantity"], 3)
        self.assertEqual(positions[0]["entry_price"],
                         self.runtime.portfolio.positions[0].average_entry_price)
        self.assertIsNone(positions[0]["unrealized_pnl"],
                          "an unpriced remainder never invents a P&L")

    def test_a_quantity_the_position_does_not_hold_is_refused(self):
        before = self.get_positions()["positions"]
        for quantity in (6, 0, -1, 2.5, "2", True):
            with self.subTest(quantity=quantity):
                status, outcome = self.post(
                    {"symbol": "AAPL", "price": 51.0, "quantity": quantity})
                self.assertEqual(status, 400)
                self.assertEqual(outcome["code"], "INVALID_QUANTITY")
        self.assertEqual(self.get_positions()["positions"], before,
                         "a refused partial close never changes state")


if __name__ == "__main__":
    unittest.main()
