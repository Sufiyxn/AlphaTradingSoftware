import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from backend.config import load_config
from backend.core.connections import ConnectionCenter
from backend.core.runtime import ApplicationRuntime
from backend.core.server import _json_default
from backend.testing import FixtureHistoryProvider

CLOCK = datetime(2024, 1, 2, 15, 29, 0, 1, tzinfo=timezone.utc)


def build_runtime(*, env=None, with_fixture_data=True, owner_token="owner-test",
                  database_path=":memory:"):
    """Construct a runtime with either injected test data or nothing connected."""
    config = load_config(env=dict(env) if env else {})
    provider = None
    if with_fixture_data:
        provider = FixtureHistoryProvider(config.trading.universe, end=CLOCK)
    return ApplicationRuntime(
        database_path=database_path, owner_token=owner_token, clock=lambda: CLOCK,
        config=config, market_data_provider=provider)


class RuntimeIntegrationTests(unittest.TestCase):
    def test_paper_cycle_is_multi_symbol_and_journaled_through_close(self):
        runtime = build_runtime()
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
            # Real quotes mark the positions before any automatic exit runs.
            report = runtime.refresh_market()
            self.assertEqual(report.availability, "CONNECTED")
            for trade in tuple(runtime.paper.trades):
                runtime.paper.on_bar(trade.symbol, trade.entry_timestamp + timedelta(minutes=1),
                                      trade.target_price + .01, trade.entry_price)
            result = runtime.performance.calculate(as_of=datetime(2024, 1, 2).date(),
                                                   portfolio=runtime.portfolio.snapshot())
            self.assertEqual(result.total_trades, len(cycle.approved_trades))
            self.assertGreaterEqual(result.evaluation_trades, 1)
            self.assertEqual(len(runtime.repository.closed_trades()), len(cycle.approved_trades))
            report = runtime.daily_reports.generate_on_demand(
                datetime(2024, 1, 2).date(), runtime.portfolio.snapshot(), system_health="OK")
            reports = runtime.repository.list_daily_performance(
                start=datetime(2024, 1, 2).date(), end=datetime(2024, 1, 2).date())
            self.assertEqual(report.trades, len(cycle.approved_trades))
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["context_json"]["qualification_status"], "NOT_QUALIFIED")
        finally:
            runtime.close()

    def test_ai_disabled_does_not_stop_paper_pipeline_and_live_stays_locked(self):
        runtime = build_runtime(env={"AI_ENABLED": "false"})
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
        runtime = build_runtime(
            with_fixture_data=False,
            env={"MARKET_DATA_PROVIDER": "sample-provider",
                 "MARKET_DATA_API_KEY": secret,
                 "MARKET_DATA_BASE_URL": "https://provider.invalid"})
        try:
            status = runtime.connections.status()
            serialized = json.dumps(status, default=_json_default)
            self.assertNotIn(secret, serialized)
            self.assertFalse(status["market_data"]["configured"])
            self.assertEqual(status["market_data"]["state"], "NOT_CONFIGURED")
            self.assertIn("not a known market data provider", status["market_data"]["reason"])
            result = runtime.connections.test("market_data")
            self.assertEqual(result["status"], "NOT_CONFIGURED")
            self.assertFalse(result["success"])
            self.assertIn("no network request was made", result["error"])
        finally:
            runtime.close()

    def test_disconnected_runtime_reports_not_configured_and_never_invents_data(self):
        runtime = build_runtime(with_fixture_data=False, env={})
        try:
            status = runtime.status()
            self.assertEqual(status["market_data_provider"], "none")
            self.assertEqual(status["market_data_state"], "NOT_CONFIGURED")
            self.assertEqual(status["mode"], "PAPER")
            self.assertEqual(status["execution_adapter"], "local_paper")
            self.assertEqual(runtime.market_data.get_latest(("AAPL", "BAC")), ())
            self.assertEqual(runtime.market_data.get_historical(
                "AAPL", CLOCK - timedelta(days=90), CLOCK), ())
            cycle = runtime.orchestrator.run_once()
            self.assertEqual(cycle.approved_trades, ())
            self.assertEqual(runtime.portfolio.positions, ())
            self.assertEqual(runtime.connections.test("market_data")["status"], "NOT_CONFIGURED")
            self.assertEqual(runtime.connections.test("trading_platform")["status"], "NOT_CONFIGURED")
            readiness = runtime.connections.readiness()
            self.assertEqual(readiness["market_data_state"], "NOT_CONFIGURED")
            self.assertEqual(readiness["broker_state"], "NOT_CONFIGURED")
            self.assertTrue(readiness["not_configured"])
        finally:
            runtime.close()

    def test_local_connection_checks_are_reported_and_live_is_never_unlockable(self):
        runtime = build_runtime(with_fixture_data=False, env={})
        try:
            status = runtime.connections.status()
            self.assertEqual(status["market_data"]["connection"]["status"], "NOT_TESTED")
            self.assertEqual(status["ai"]["role"], "ADVISORY_ONLY")
            self.assertEqual(status["paper_account"]["mode"], "SIMULATED")
            self.assertEqual(status["paper_account"]["market_data_authority"], "NONE")
            self.assertFalse(status["trading_platform"]["live_execution_available"])
            self.assertEqual(runtime.connections.readiness()["live_mode"], "LOCKED")
            for section in ("ai", "notifications"):
                self.assertTrue(runtime.connections.test(section)["success"], section)
            for section in ("market_data", "trading_platform"):
                result = runtime.connections.test(section)
                self.assertFalse(result["success"], section)
                self.assertEqual(result["status"], "NOT_CONFIGURED", section)
                self.assertIn("no network request was made", result["error"])
        finally:
            runtime.close()

    def test_product_runtime_wires_no_test_fixture(self):
        runtime = build_runtime(with_fixture_data=False, env={})
        try:
            self.assertEqual(type(runtime.market_data_provider).__name__,
                             "UnconfiguredMarketDataProvider")
            self.assertEqual(runtime.execution.identify(), "local_paper")
            self.assertEqual(type(runtime.scanner._analyzer).__name__, "StrategyAnalyzer")
            self.assertEqual(type(runtime.paper_adapter).__name__, "LocalPaperAdapter")
        finally:
            runtime.close()


class ConnectionCenterTests(unittest.TestCase):
    def _center(self, *, config, runtime):
        return ConnectionCenter(
            config=config, market_data=runtime.market_data,
            universe=runtime.orchestrator.config.universe, execution=runtime.execution,
            performance=runtime.performance, portfolio=runtime.portfolio, risk=runtime.risk,
            notifier=runtime.notifier, owner_controls_enabled=False, clock=lambda: CLOCK,
            repository=runtime.repository)

    def test_unknown_section_is_rejected(self):
        runtime = build_runtime(with_fixture_data=False, env={})
        try:
            center = self._center(config=runtime.config, runtime=runtime)
            with self.assertRaises(ValueError):
                center.test("not-a-section")
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
