import json
import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from backend.core.connections import ConnectionCenter
from backend.core.runtime import ApplicationRuntime
from backend.core.server import _json_default


class RuntimeIntegrationTests(unittest.TestCase):
    def test_paper_cycle_is_multi_symbol_and_journaled_through_close(self):
        with patch.dict(os.environ, {}, clear=True):
            runtime = ApplicationRuntime(database_path=":memory:", owner_token="owner-test")
        try:
            cycle = runtime.orchestrator.run_once()
            self.assertGreaterEqual(len(cycle.approved_trades), 2)
            self.assertTrue(all(trade.entry_price < 100 for trade in cycle.approved_trades))
            self.assertTrue(all(runtime.paper._setups[trade.order_id].reward_risk_ratio >= 2.0
                                for trade in cycle.approved_trades))
            open_rows = runtime.repository.open_trades()
            self.assertEqual(len(open_rows), len(cycle.approved_trades))
            self.assertTrue(all(row["context_json"]["setup"] and row["context_json"]["risk"]
                                and row["context_json"]["ai_analysis"] for row in open_rows))
            self.assertGreaterEqual(len(runtime.repository.rows("orders")), 2)
            for trade in tuple(runtime.paper.trades):
                runtime.paper.on_bar(trade.symbol, trade.entry_timestamp + timedelta(minutes=1),
                                      trade.target_price + .01, trade.entry_price)
            result = runtime.performance.calculate(as_of=date(2024, 1, 2), portfolio=runtime.portfolio.snapshot())
            self.assertEqual(result.total_trades, len(cycle.approved_trades))
            self.assertGreaterEqual(result.evaluation_trades, 1)
            self.assertEqual(len(runtime.repository.closed_trades()), len(cycle.approved_trades))
            report = runtime.daily_reports.generate_on_demand(
                date(2024, 1, 2), runtime.portfolio.snapshot(), system_health="OK")
            reports = runtime.repository.list_daily_performance(start=date(2024, 1, 2), end=date(2024, 1, 2))
            self.assertEqual(report.trades, len(cycle.approved_trades))
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["context_json"]["qualification_status"], "NOT_QUALIFIED")
        finally:
            runtime.close()

    def test_ai_disabled_does_not_stop_paper_pipeline_and_live_stays_locked(self):
        with patch.dict(os.environ, {"AI_ENABLED": "false"}, clear=True):
            runtime = ApplicationRuntime(database_path=":memory:", owner_token="owner-test")
        try:
            result = runtime.orchestrator.run_once()
            self.assertGreaterEqual(len(result.approved_trades), 2)
            self.assertEqual(result.ai_analyses, ())
            readiness = runtime.connections.readiness()
            self.assertEqual(readiness["live_mode"], "LOCKED")
            self.assertTrue(any(item["key"] == "reward_risk" and item["passed"] for item in readiness["checks"]))
            self.assertTrue(any(item["key"] == "entry_limit" and item["passed"] for item in readiness["checks"]))
        finally:
            runtime.close()

    def test_connection_status_redacts_secret_values_and_external_test_does_not_connect(self):
        secret = "never-return-this-api-secret"
        with patch.dict(os.environ, {"MARKET_DATA_PROVIDER": "sample-provider",
                                     "MARKET_DATA_API_KEY": secret,
                                     "MARKET_DATA_BASE_URL": "https://provider.invalid"}, clear=True):
            runtime = ApplicationRuntime(database_path=":memory:", owner_token="owner-test")
            try:
                status = runtime.connections.status()
                serialized = json.dumps(status, default=_json_default)
                self.assertNotIn(secret, serialized)
                self.assertTrue(status["market_data"]["configured"])
                result = runtime.connections.test("market_data")
                self.assertEqual(result["status"], "UNSUPPORTED")
                self.assertFalse(result["success"])
                self.assertIn("no network request was made", result["error"])
            finally:
                runtime.close()

    def test_local_connection_checks_are_reported_and_live_is_never_unlockable(self):
        with patch.dict(os.environ, {}, clear=True):
            runtime = ApplicationRuntime(database_path=":memory:", owner_token="owner-test")
        try:
            for section in ("market_data", "trading_platform", "ai", "notifications"):
                self.assertTrue(runtime.connections.test(section)["success"])
            status = runtime.connections.status()
            self.assertEqual(status["market_data"]["connection"]["status"], "PASSED")
            self.assertEqual(status["ai"]["role"], "ADVISORY_ONLY")
            self.assertFalse(status["trading_platform"]["live_execution_available"])
            self.assertEqual(runtime.connections.readiness()["live_mode"], "LOCKED")
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
