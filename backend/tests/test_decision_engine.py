"""Phase 12: the autonomous decision engine explains itself and recovers."""
import unittest
from datetime import datetime, timedelta, timezone

from backend.database import SQLiteJournalRepository
from backend.execution import MockExecutionProvider
from backend.market_data import Bar
from backend.orchestrator import (
    APPROVED,
    CANDIDATE,
    DATA_FRESHNESS,
    DECISION_EVENT,
    EXECUTION,
    REJECTED,
    RISK,
    SESSION,
    SKIPPED,
    STRATEGY,
    TradingOrchestrator,
    OrchestratorConfig,
    OrchestratorStatus,
)
from backend.paper_trading import PaperTradingEngine
from backend.portfolio import Portfolio
from backend.risk import RiskEngine
from backend.scanner.models import Direction as OpportunityDirection, Opportunity, SymbolResult
from backend.scanner.universe import SymbolUniverse
from backend.strategies.models import Direction, StrategySignal
from backend.tests.test_orchestrator import FixtureScanner, FixtureStrategy, candidate, setup

NOW = datetime(2024, 1, 2, 15, tzinfo=timezone.utc)


class ClockMarketData:
    def __init__(self, bar_age: timedelta = timedelta(minutes=1)):
        self.bar_age = bar_age

    def get_historical(self, symbol, start, end):
        latest = NOW - self.bar_age
        return (Bar(symbol, latest - timedelta(minutes=1), 50, 51, 49, 50, 100),
                Bar(symbol, latest, 50, 51, 49, 50, 100))


class NoSetupStrategy:
    name = "fixture_no_setup"

    def evaluate(self, bars):
        symbol = bars[-1].symbol
        return StrategySignal(symbol, NOW, Direction.NONE, None,
                              "Trend and momentum conditions do not align", self.name)


def make_orchestrator(*, symbols=("AAA",), candidates=None, journal=None,
                      market=None, strategy=None, risk_rejections=(),
                      max_data_age=None, clock=lambda: NOW,
                      session_submission="regular", provider_extended_hours=None):
    universe = SymbolUniverse(symbols)
    portfolio = Portfolio(100_000)
    risk = RiskEngine()
    paper = PaperTradingEngine(risk, portfolio, MockExecutionProvider(),
                               journal=journal, clock=clock,
                               session_submission=session_submission)
    scanner = FixtureScanner(
        candidates if candidates is not None
        else [candidate(symbol) for symbol in symbols])
    orchestrator = TradingOrchestrator(
        market_data=market if market is not None else ClockMarketData(),
        scanner=scanner,
        strategy=strategy if strategy is not None else FixtureStrategy(),
        risk_engine=risk, paper_trading=paper, portfolio=portfolio,
        config=OrchestratorConfig(universe, timedelta(seconds=1), timedelta(days=1),
                                  max_data_age=max_data_age),
        clock=clock, journal=journal,
        session_submission=session_submission,
        provider_extended_hours=provider_extended_hours)
    orchestrator._status = OrchestratorStatus.RUNNING
    return orchestrator


class DecisionLedgerTests(unittest.TestCase):
    def test_an_approved_cycle_leaves_risk_and_execution_decisions(self):
        orchestrator = make_orchestrator()
        result = orchestrator.run_once()
        self.assertEqual(len(result.approved_trades), 1)
        self.assertEqual([(d.stage, d.outcome) for d in result.decisions],
                         [(RISK, APPROVED), (EXECUTION, APPROVED)])
        self.assertEqual(result.decisions[1].symbol, "AAA")
        state = orchestrator.state
        self.assertEqual(state.decisions_total, 2)
        self.assertEqual(state.last_decision["stage"], EXECUTION)
        self.assertEqual(state.last_decision["outcome"], APPROVED)
        summary = orchestrator.decisions()["summary"]
        self.assertEqual(summary["approved"], 2)
        self.assertEqual(summary["rejected"], 0)
        self.assertEqual(summary["latest_cycle"], 1)

    def test_a_risk_rejection_is_recorded_with_its_reasons(self):
        orchestrator = make_orchestrator()
        orchestrator.risk_engine.set_kill_switch(True)
        result = orchestrator.run_once()
        self.assertEqual(result.approved_trades, ())
        risk_decisions = [d for d in result.decisions if d.stage == RISK]
        self.assertEqual(len(risk_decisions), 1)
        self.assertEqual(risk_decisions[0].outcome, REJECTED)
        self.assertTrue(risk_decisions[0].reasons)
        self.assertEqual(orchestrator.state.trades_rejected_by_risk, 1)
        stages = {d.stage for d in result.decisions}
        self.assertNotIn(EXECUTION, stages)

    def test_an_invalid_candidate_leaves_a_candidate_decision_with_reasons(self):
        bad = Opportunity("AAA", NOW, OpportunityDirection.LONG, 0.0, None, None,
                          None, None, None, None, ("setup failed reward/risk",), False)
        orchestrator = make_orchestrator(candidates=[bad])
        result = orchestrator.run_once()
        self.assertEqual(len(result.decisions), 1)
        decision = result.decisions[0]
        self.assertEqual((decision.stage, decision.outcome), (CANDIDATE, REJECTED))
        self.assertIn("setup failed reward/risk", decision.reasons)

    def test_a_strategy_without_a_setup_is_recorded_not_silently_dropped(self):
        orchestrator = make_orchestrator(strategy=NoSetupStrategy())
        result = orchestrator.run_once()
        self.assertEqual(len(result.decisions), 1)
        decision = result.decisions[0]
        self.assertEqual((decision.stage, decision.outcome), (STRATEGY, REJECTED))
        self.assertIn("Trend and momentum conditions do not align", decision.reasons)

    def test_a_scanner_data_failure_is_recorded_for_that_symbol(self):
        orchestrator = make_orchestrator(symbols=("AAA",))
        orchestrator.scanner.failures = ("AAA",)
        result = orchestrator.run_once()
        stages = [(d.stage, d.outcome, d.symbol) for d in result.decisions]
        self.assertIn(( "SCANNER", REJECTED, "AAA"), stages)

    def test_stale_bars_are_refused_before_the_strategy_runs(self):
        orchestrator = make_orchestrator(
            market=ClockMarketData(bar_age=timedelta(hours=2)),
            max_data_age=timedelta(seconds=300))
        events = []
        orchestrator.strategy.evaluate = lambda bars: events.append(bars) or FixtureStrategy().evaluate(bars)
        result = orchestrator.run_once()
        self.assertEqual(result.approved_trades, ())
        self.assertEqual(events, [], "no strategy evaluation on stale data")
        self.assertEqual(len(result.decisions), 1)
        decision = result.decisions[0]
        self.assertEqual((decision.stage, decision.outcome), (DATA_FRESHNESS, REJECTED))
        self.assertIn("freshness limit", decision.reasons[0])
        self.assertEqual(orchestrator.state.data_stale_rejected, 1)
        self.assertEqual(orchestrator.state.trades_rejected_by_risk, 0)

    def test_a_freshness_gate_disabled_by_config_never_refuses(self):
        orchestrator = make_orchestrator(market=ClockMarketData(bar_age=timedelta(hours=6)))
        result = orchestrator.run_once()
        self.assertEqual(len(result.approved_trades), 1)

    def test_a_session_skip_is_a_decision_not_an_error(self):
        saturday = datetime(2024, 1, 6, 15, tzinfo=timezone.utc)
        orchestrator = make_orchestrator(clock=lambda: saturday)
        result = orchestrator.run_once()
        self.assertTrue(result.session_skipped)
        self.assertEqual(result.errors, (), "a closed market is not an error")
        # Completed bars are still analyzed; the entry itself is refused.
        self.assertEqual(result.approved_trades, (),
                         "nothing is submitted while entries are blocked")
        self.assertEqual([(d.stage, d.outcome) for d in result.decisions],
                         [(SESSION, SKIPPED), (SESSION, REJECTED)])
        cycle_decision, entry_decision = result.decisions
        self.assertTrue(cycle_decision.reasons)
        self.assertIsNone(cycle_decision.symbol)
        self.assertEqual(entry_decision.symbol, "AAA")
        self.assertTrue(entry_decision.reasons)


class DecisionRecoveryTests(unittest.TestCase):
    def test_a_restart_resumes_cycle_numbering_and_keeps_the_ledger(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            first = make_orchestrator(journal=journal)
            first.run_once()
            first.run_once()
            decisions_seen = first.state.decisions_total
            self.assertEqual(first.state.current_cycle, 2)
            self.assertEqual(journal.events_by_type((DECISION_EVENT,), limit=10)[0]["event_type"],
                             DECISION_EVENT)

            second = make_orchestrator(journal=journal)
            self.assertEqual(second.state.current_cycle, 2,
                             "the cycle counter continues instead of restarting at zero")
            self.assertEqual(second.state.decisions_total, decisions_seen)
            self.assertEqual(second.decisions()["decisions"][0]["cycle"], 1)
            third_cycle = second.run_once()
            self.assertEqual(third_cycle.cycle, 3)
        finally:
            journal.close()

    def test_an_interrupted_cycle_is_journaled_counted_and_resumed(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            journal.record_event(NOW, "CYCLE_STARTED", "Trading cycle 7 started",
                                 context={"cycle": 7})
            orchestrator = make_orchestrator(journal=journal)
            state = orchestrator.state
            self.assertEqual(state.current_cycle, 7)
            self.assertEqual(state.interrupted_cycles, 1)
            events = [row["event_type"] for row in journal.events()]
            self.assertIn("CYCLE_INTERRUPTED", events)
            recovery = [d for d in orchestrator.decisions(limit=10)["decisions"]
                        if d["stage"] == "CYCLE"]
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0]["outcome"], SKIPPED)
            self.assertIn("never completed", recovery[0]["reasons"][0])
            self.assertEqual(orchestrator.run_once().cycle, 8)
        finally:
            journal.close()

    def test_decisions_survive_a_restart_from_the_journal_alone(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            orchestrator = make_orchestrator(journal=journal, symbols=("AAA", "BBB"))
            orchestrator.run_once()
            expected = orchestrator.decisions()["decisions"]
            restored = make_orchestrator(journal=journal, symbols=("AAA", "BBB"))
            payload = restored.decisions()
            self.assertEqual([d["stage"] for d in payload["decisions"]],
                             [d["stage"] for d in expected])
            self.assertEqual([d["outcome"] for d in payload["decisions"]],
                             [d["outcome"] for d in expected])
            self.assertEqual(payload["summary"]["latest_cycle"], 1)
        finally:
            journal.close()


if __name__ == "__main__":
    unittest.main()
