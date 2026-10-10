"""HTTP endpoints the trading terminal reads: candles, watchlist, scanner, decisions."""
import json
import threading
import unittest
from datetime import datetime, timezone
from urllib import request as urlrequest
from urllib.error import HTTPError

from backend.config import load_config
from backend.core.runtime import ApplicationRuntime
from backend.core.server import _json_default, create_server
from backend.orchestrator import OrchestratorStatus
from backend.testing import FixtureHistoryProvider

CLOCK = datetime(2024, 1, 2, 15, 29, 0, 1, tzinfo=timezone.utc)


def build_runtime(*, env=None, with_fixture_data=True):
    config = load_config(env=dict(env) if env else {})
    provider = (FixtureHistoryProvider(config.trading.universe, end=CLOCK)
                if with_fixture_data else None)
    return ApplicationRuntime(database_path=":memory:", owner_token="owner-test",
                              clock=lambda: CLOCK, config=config,
                              market_data_provider=provider)


class TradingEndpointTests(unittest.TestCase):
    def get(self, port, path):
        with urlrequest.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_status(self, port, path):
        try:
            self.get(port, path)
            return 200
        except HTTPError as error:
            return error.code

    def server(self, runtime):
        server = create_server(runtime, "127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, port, thread

    def test_candles_serve_validated_history_when_a_provider_is_connected(self):
        runtime = build_runtime()
        server, port, thread = self.server(runtime)
        try:
            payload = self.get(port, "/api/candles?symbol=AAPL&timeframe=1min&days=2")
            self.assertEqual(payload["state"], "OK")
            self.assertEqual(payload["symbol"], "AAPL")
            self.assertGreater(payload["bar_count"], 0)
            self.assertEqual(len(payload["candles"]), payload["bar_count"])
            self.assertEqual(payload["freshness"], "FRESH")
            self.assertTrue(payload["timeframes"])
            json.dumps(payload, default=_json_default)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5); runtime.close()

    def test_candles_report_not_configured_instead_of_inventing_history(self):
        runtime = build_runtime(with_fixture_data=False)
        server, port, thread = self.server(runtime)
        try:
            payload = self.get(port, "/api/candles?symbol=AAPL")
            self.assertEqual(payload["state"], "NOT_CONFIGURED")
            self.assertEqual(payload["candles"], [])
            self.assertIn("no market data provider", payload["reason"])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5); runtime.close()

    def test_candles_reject_missing_symbol_and_unknown_timeframe(self):
        runtime = build_runtime()
        server, port, thread = self.server(runtime)
        try:
            self.assertEqual(self.get_status(port, "/api/candles"), 400)
            self.assertEqual(self.get_status(port, "/api/candles?symbol=AAPL&timeframe=3weeks"), 400)
            self.assertEqual(self.get_status(port, "/api/candles?symbol=AAPL&days=abc"), 400)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5); runtime.close()

    def test_watchlist_prices_the_universe_with_indicators(self):
        runtime = build_runtime()
        server, port, thread = self.server(runtime)
        try:
            payload = self.get(port, "/api/watchlist")
            self.assertEqual(payload["state"], "OK")
            universe = runtime.orchestrator.config.universe.symbols
            self.assertEqual([row["symbol"] for row in payload["rows"]], list(universe))
            for row in payload["rows"]:
                self.assertEqual(row["state"], "PRICED")
                self.assertIsNotNone(row["last_price"])
                self.assertIsNotNone(row["ema"])
                self.assertIsNotNone(row["rsi"])
                self.assertIsNotNone(row["atr"])
                self.assertEqual(row["indicator_state"], "OK")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5); runtime.close()

    def test_watchlist_without_a_provider_reports_honest_gaps(self):
        runtime = build_runtime(with_fixture_data=False)
        server, port, thread = self.server(runtime)
        try:
            payload = self.get(port, "/api/watchlist")
            self.assertEqual(payload["state"], "NOT_CONFIGURED")
            for row in payload["rows"]:
                self.assertEqual(row["state"], "UNPRICED")
                self.assertIsNone(row["last_price"])
                self.assertTrue(row["reason"])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5); runtime.close()

    def test_scanner_lists_every_symbol_with_its_reasons(self):
        runtime = build_runtime()
        server, port, thread = self.server(runtime)
        try:
            payload = self.get(port, "/api/scanner")
            universe = runtime.orchestrator.config.universe.symbols
            self.assertEqual(payload["symbol_count"], len(universe))
            self.assertEqual(payload["eligible_count"],
                             sum(1 for row in payload["symbols"] if row["eligible"]))
            for row in payload["symbols"]:
                self.assertIn(row["symbol"], universe)
                self.assertIsNotNone(row["opportunity"])
                json.dumps(row, default=_json_default)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5); runtime.close()

    def test_decisions_expose_the_engine_summary_and_records(self):
        runtime = build_runtime()
        server, port, thread = self.server(runtime)
        try:
            payload = self.get(port, "/api/decisions")
            self.assertIn("summary", payload)
            self.assertEqual(payload["summary"]["total"], 0)
            self.assertEqual(payload["decisions"], [])

            runtime.orchestrator._status = OrchestratorStatus.RUNNING
            runtime.orchestrator.run_once()
            payload = self.get(port, "/api/decisions?limit=5")
            self.assertGreaterEqual(payload["summary"]["total"], 1)
            self.assertEqual(payload["summary"]["latest_cycle"], 1)
            self.assertTrue(payload["decisions"])
            self.assertEqual(self.get_status(port, "/api/decisions?limit=9999"), 400)
            json.dumps(payload, default=_json_default)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5); runtime.close()

    def test_market_data_credentials_never_reach_the_browser(self):
        runtime = build_runtime(env={"MARKET_DATA_PROVIDER": "polygon",
                                     "POLYGON_API_KEY": "test-key-do-not-leak"},
                                with_fixture_data=False)
        server, port, thread = self.server(runtime)
        try:
            for path in ("/api/candles?symbol=AAPL", "/api/watchlist",
                         "/api/scanner", "/api/decisions", "/api/status"):
                body = json.dumps(self.get(port, path), default=_json_default)
                self.assertNotIn("test-key-do-not-leak", body)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5); runtime.close()


if __name__ == "__main__":
    unittest.main()
