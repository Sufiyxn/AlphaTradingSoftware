import unittest
from datetime import timedelta

from backend.admin import LocalOwnerAuthenticator, OwnerControlPlane
from backend.database import SQLiteJournalRepository
from backend.orchestrator import OrchestratorConfig, OrchestratorStatus, TradingOrchestrator
from backend.paper_trading import PaperTradingEngine
from backend.portfolio import Portfolio
from backend.risk import RiskEngine
from backend.scanner.universe import SymbolUniverse


class EmptyScanner:
    def scan(self, universe):
        return ()


def control_fixture(*, shutdown=None):
    portfolio = Portfolio(10_000)
    risk = RiskEngine()
    orchestrator = TradingOrchestrator(
        market_data=object(), scanner=EmptyScanner(), strategy=object(), risk_engine=risk,
        paper_trading=PaperTradingEngine(risk, portfolio), portfolio=portfolio,
        config=OrchestratorConfig(SymbolUniverse(["ABC"]), timedelta(hours=1)),
    )
    repository = SQLiteJournalRepository(":memory:")
    controls = OwnerControlPlane(LocalOwnerAuthenticator("test-owner-token", "qa-owner"),
                                 orchestrator, repository, shutdown=shutdown)
    return controls, orchestrator, risk, repository


class OwnerControlTests(unittest.TestCase):
    def test_authentication_status_and_recent_audit_logs(self):
        controls, _, _, repository = control_fixture()
        try:
            with self.assertRaises(PermissionError):
                controls.inspect_status("wrong")
            state = controls.inspect_status("test-owner-token")
            self.assertEqual(state["actor"], "qa-owner")
            self.assertEqual(state["orchestrator"].status, OrchestratorStatus.STOPPED)
            self.assertTrue(controls.inspect_recent_logs("test-owner-token")["events"])
        finally:
            if controls._worker is not None:
                controls._worker.join(1)
            repository.close()

    def test_pause_resume_and_approved_runtime_configuration(self):
        controls, orchestrator, _, repository = control_fixture()
        try:
            controls.pause("test-owner-token")
            before = orchestrator.state.current_cycle
            paused_result = orchestrator.run_once()
            self.assertEqual(paused_result.cycle, before)
            self.assertEqual(orchestrator.state.status, OrchestratorStatus.PAUSED)
            controls.update_runtime_config("test-owner-token", "scan_interval_seconds", 0.5)
            self.assertEqual(orchestrator.config.scan_interval, timedelta(seconds=.5))
            with self.assertRaises(ValueError):
                controls.update_runtime_config("test-owner-token", "max_candidate_price", 101)
            with self.assertRaises(ValueError):
                controls.update_runtime_config("test-owner-token", "risk_fraction", .9)
            controls.resume("test-owner-token")
            self.assertEqual(orchestrator.state.status, OrchestratorStatus.RUNNING)
            controls.terminate("test-owner-token")
            self.assertTrue(controls.wait_for_termination(.5))
        finally:
            if controls._worker is not None:
                controls._worker.join(1)
            repository.close()

    def test_emergency_stop_latches_risk_and_orchestrator_and_persists_trigger(self):
        controls, orchestrator, risk, repository = control_fixture()
        try:
            controls.emergency_stop("test-owner-token", "operator safety action")
            self.assertTrue(risk.kill_switch_enabled)
            self.assertEqual(orchestrator.state.status, OrchestratorStatus.EMERGENCY_STOPPED)
            cycle = orchestrator.state.current_cycle
            self.assertEqual(orchestrator.run_once().cycle, cycle)
            with self.assertRaises(RuntimeError):
                orchestrator.resume()
            event = next(item for item in repository.events() if item["event_type"] == "OWNER_EMERGENCY_STOP")
            self.assertEqual(event["context_json"]["triggered_by"], "qa-owner")
            self.assertEqual(event["context_json"]["reason"], "operator safety action")
        finally:
            repository.close()

    def test_termination_signals_host_shutdown_callback(self):
        calls = []
        controls, orchestrator, _, repository = control_fixture(shutdown=lambda: calls.append("shutdown"))
        try:
            controls.terminate("test-owner-token")
            self.assertEqual(calls, ["shutdown"])
            self.assertTrue(controls.termination_requested)
            self.assertEqual(orchestrator.state.status, OrchestratorStatus.STOPPED)
            event = next(item for item in repository.events() if item["event_type"] == "OWNER_REQUESTED_TERMINATION")
            self.assertEqual(event["context_json"]["triggered_by"], "qa-owner")
        finally:
            repository.close()


if __name__ == "__main__":
    unittest.main()
