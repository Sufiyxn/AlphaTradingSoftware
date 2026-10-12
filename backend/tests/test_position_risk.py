"""Phase 10 checkpoint 6 proof: stop and target can move after entry.

CP6 is the phase's *risk changes* requirement: stop and target are fixed when a
trade opens, so the levels automatic exit uses can never be tightened. These
tests hold the guarantees the checkpoint promises:

- the change is fail-closed: only a **risk-reducing** stop move is accepted,
  the new stop must stay on the losing side of entry, the target beyond entry,
  and the resulting reward:risk must keep the configured floor (2:1);
- prices are always explicit, positive and finite - nothing is derived or
  defaulted, so no move can invent a level;
- exactly one `RISK_CHANGED` position event carries the old and new values;
- entry price, quantity and fills are untouched;
- the automatic exit uses the new levels immediately, and a restart restores
  them (not the original entry levels);
- the HTTP surface keeps the owner-token guard: 401 without it, 400 for the
  refusals above, 404 for a symbol with no open position, 200 only for a
  change that actually happened, and no credential in the response.
"""
import json
import os
import tempfile
import threading
import unittest
from urllib import request as urlrequest
from urllib.error import HTTPError

from backend.core.server import create_server
from backend.portfolio.models import PriceState
from backend.tests.test_position_exits import (
    CLOCK,
    OWNER_TOKEN,
    build_runtime,
    open_position,
)

FLOOR = 2.0


def risk_events(repository) -> list:
    return [row for row in repository.rows("position_events")
            if row["event_type"] == "RISK_CHANGED"]


def trade_row(repository) -> dict:
    rows = repository.rows("trades")
    return rows[0] if rows else {}


class RiskAdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.runtime = build_runtime()
        open_position(self.runtime)
        self.position = self.runtime.portfolio.positions[0]
        self.entry = self.position.average_entry_price
        self.record = self.runtime.paper.trades[0]
        self.order_id = self.record.order_id

    def tearDown(self):
        self.runtime.close()

    def adjust(self, **kwargs):
        return self.runtime.change_position_risk("AAPL", **kwargs)

    def test_a_tightened_stop_is_accepted_and_journaled_once(self):
        outcome = self.adjust(stop_price=49.5)
        self.assertTrue(outcome["changed"])
        self.assertEqual(outcome["symbol"], "AAPL")
        self.assertEqual(outcome["execution"], "PAPER")
        self.assertEqual(outcome["reason"], "OWNER_ADJUSTMENT")
        self.assertEqual(outcome["stop_price"], {"old": 48.0, "new": 49.5})
        self.assertEqual(outcome["target_price"], {"old": 54.0, "new": 54.0})
        self.assertGreaterEqual(outcome["reward_risk_ratio"], FLOOR)
        record = self.runtime.paper.trades[0]
        self.assertEqual(record.stop_price, 49.5)
        self.assertEqual(record.target_price, 54.0)
        row = trade_row(self.runtime.repository)
        self.assertEqual(row["stop_price"], 49.5)
        self.assertEqual(row["target_price"], 54.0)
        events = risk_events(self.runtime.repository)
        self.assertEqual(len(events), 1, "exactly one RISK_CHANGED event")
        self.assertEqual(events[0]["context_json"]["stop_price"],
                         {"old": 48.0, "new": 49.5})
        self.assertEqual(events[0]["context_json"]["target_price"],
                         {"old": 54.0, "new": 54.0})
        self.assertEqual(events[0]["context_json"]["reason"], "OWNER_ADJUSTMENT")

    def test_entry_quantity_and_fills_are_untouched(self):
        rows_before = trade_row(self.runtime.repository)
        fills_before = self.runtime.repository.rows("fills")
        outcome = self.adjust(stop_price=49.5, target_price=55.0)
        self.assertTrue(outcome["changed"])
        row = trade_row(self.runtime.repository)
        for field in ("entry_price", "quantity", "exit_timestamp", "exit_price",
                      "pnl", "order_id", "entry_timestamp"):
            self.assertEqual(row[field], rows_before[field],
                             f"{field} must not move with the risk levels")
        self.assertEqual(self.runtime.repository.rows("fills"), fills_before)
        self.assertEqual(self.runtime.portfolio.positions[0].quantity,
                         self.position.quantity)
        self.assertEqual(self.runtime.portfolio.positions[0].average_entry_price,
                         self.entry)
        self.assertEqual(row["context_json"]["setup"],
                         rows_before["context_json"]["setup"],
                         "the approved entry setup is an audit record, not the live level")

    def test_a_loosened_or_repeated_stop_is_refused(self):
        for stop in (48.0, 47.0, 46.5):
            with self.subTest(stop=stop):
                outcome = self.adjust(stop_price=stop)
                self.assertFalse(outcome["changed"])
                self.assertEqual(outcome["code"], "RISK_NOT_REDUCING")
        self.assertEqual(self.runtime.paper.trades[0].stop_price, 48.0)
        self.assertEqual(trade_row(self.runtime.repository)["stop_price"], 48.0)
        self.assertEqual(risk_events(self.runtime.repository), [],
                         "a refused move journals nothing")

    def test_a_stop_through_entry_is_refused(self):
        outcome = self.adjust(stop_price=round(self.entry + 0.5, 2))
        self.assertFalse(outcome["changed"])
        self.assertEqual(outcome["code"], "STOP_THROUGH_ENTRY")
        self.assertEqual(self.runtime.paper.trades[0].stop_price, 48.0)

    def test_a_target_that_breaks_the_floor_is_refused(self):
        outcome = self.adjust(stop_price=49.5, target_price=50.5)
        self.assertFalse(outcome["changed"])
        self.assertEqual(outcome["code"], "REWARD_RISK_TOO_LOW")
        self.assertEqual(self.runtime.paper.trades[0].target_price, 54.0)

    def test_a_target_must_stay_beyond_entry(self):
        outcome = self.adjust(target_price=round(self.entry - 1.0, 2))
        self.assertFalse(outcome["changed"])
        self.assertEqual(outcome["code"], "TARGET_NOT_BEYOND_ENTRY")
        self.assertEqual(self.runtime.paper.trades[0].target_price, 54.0)

    def test_impossible_requests_are_refused(self):
        for body in ({"stop_price": 0}, {"stop_price": -1},
                     {"stop_price": "49.5"}, {"stop_price": True},
                     {"stop_price": float("nan")}, {"target_price": 0},
                     {}):
            with self.subTest(body=body):
                outcome = self.adjust(**body)
                self.assertFalse(outcome["changed"])
                self.assertEqual(outcome["code"],
                                 "INVALID_PAYLOAD" if not body else "INVALID_PRICE")
        missing = self.runtime.change_position_risk("MSFT", stop_price=49.5)
        self.assertFalse(missing["changed"])
        self.assertEqual(missing["code"], "POSITION_NOT_FOUND")

    def test_the_tightened_stop_drives_the_automatic_exit_immediately(self):
        self.assertTrue(self.adjust(stop_price=49.5)["changed"])
        price = 49.25  # above the original 48.00 stop, below the new 49.50 stop
        self.runtime.portfolio.update_price("AAPL", price, timestamp=CLOCK,
                                            state=PriceState.FRESH)
        closed = self.runtime.paper.on_quote("AAPL", CLOCK, price)
        self.assertEqual(len(closed), 1, "the new level is live at once")
        self.assertEqual(self.runtime.portfolio.positions, ())
        row = trade_row(self.runtime.repository)
        self.assertEqual(row["context_json"]["close"]["reason"], "STOP")

    def test_without_the_adjustment_the_same_quote_would_not_exit(self):
        price = 49.25
        self.runtime.portfolio.update_price("AAPL", price, timestamp=CLOCK,
                                            state=PriceState.FRESH)
        closed = self.runtime.paper.on_quote("AAPL", CLOCK, price)
        self.assertEqual(closed, (), "the original stop does not fire at 49.25")
        self.assertEqual(len(self.runtime.portfolio.positions), 1)

    def test_the_adjusted_levels_survive_a_restart(self):
        database = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        database.close()
        os.unlink(database.name)
        try:
            first = build_runtime(database_path=database.name)
            try:
                open_position(first)
                outcome = first.change_position_risk("AAPL", stop_price=49.5,
                                                     target_price=55.0)
                self.assertTrue(outcome["changed"])
            finally:
                first.close()

            second = build_runtime(database_path=database.name)
            try:
                record = second.paper.trades[0]
                self.assertEqual(record.stop_price, 49.5)
                self.assertEqual(record.target_price, 55.0)
                price = 49.25
                second.portfolio.update_price("AAPL", price, timestamp=CLOCK,
                                              state=PriceState.FRESH)
                closed = second.paper.on_quote("AAPL", CLOCK, price)
                self.assertEqual(len(closed), 1,
                                 "the restored level drives the exit, not the entry one")
                self.assertEqual(second.portfolio.positions, ())
            finally:
                second.close()
        finally:
            if os.path.exists(database.name):
                os.unlink(database.name)


class HttpRiskAdjustmentTests(unittest.TestCase):
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

    def post(self, body, token=OWNER_TOKEN):
        request = urlrequest.Request(
            f"http://127.0.0.1:{self.port}/api/positions/risk",
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

    def test_the_change_requires_the_owner_token(self):
        status, body = self.post({"symbol": "AAPL", "stop_price": 49.5},
                                 token=None)
        self.assertEqual(status, 401)
        self.assertEqual(self.runtime.paper.trades[0].stop_price, 48.0)

    def test_refusals_keep_their_status_codes(self):
        for body, code in (({"symbol": "AAPL"}, "INVALID_PAYLOAD"),
                           ({"symbol": "AAPL", "stop_price": 0}, "INVALID_PRICE"),
                           ({"symbol": "AAPL", "stop_price": 47.0},
                            "RISK_NOT_REDUCING"),
                           ({"symbol": "MSFT", "stop_price": 49.5},
                            "POSITION_NOT_FOUND")):
            with self.subTest(code=code):
                status, outcome = self.post(body)
                self.assertEqual(status, 404 if code == "POSITION_NOT_FOUND" else 400)
                self.assertFalse(outcome["changed"])
                self.assertEqual(outcome["code"], code)
        self.assertEqual(self.runtime.paper.trades[0].stop_price, 48.0)

    def test_a_tightened_stop_is_served_by_the_positions_api(self):
        status, outcome = self.post({"symbol": "AAPL", "stop_price": 49.5,
                                     "target_price": 55.0})
        self.assertEqual(status, 200)
        self.assertTrue(outcome["changed"])
        self.assertEqual(outcome["stop_price"], {"old": 48.0, "new": 49.5})
        self.assertEqual(outcome["target_price"], {"old": 54.0, "new": 55.0})
        self.assertEqual(outcome["execution"], "PAPER")
        position = self.get_json("/api/positions")["positions"][0]
        self.assertEqual(position["stop_price"], 49.5)
        self.assertEqual(position["target_price"], 55.0)
        self.assertEqual(position["entry_price"],
                         self.runtime.portfolio.positions[0].average_entry_price)
        self.assertEqual(position["quantity"], 5)
        serialized = json.dumps(outcome)
        self.assertNotIn(OWNER_TOKEN, serialized)


if __name__ == "__main__":
    unittest.main()
