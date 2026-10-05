import unittest
import threading
from datetime import datetime, timedelta, timezone

from backend.execution import MockExecutionProvider
from backend.ai import AIAnalysisService
from backend.database import SQLiteJournalRepository
from backend.market_data import Bar
from backend.orchestrator import OrchestratorConfig, OrchestratorStatus, TradingOrchestrator
from backend.paper_trading import PaperTradingEngine
from backend.portfolio import Portfolio
from backend.risk import RiskConfig, RiskEngine
from backend.scanner.models import Direction as OpportunityDirection, Opportunity, SymbolResult
from backend.scanner.universe import SymbolUniverse
from backend.strategies.models import Direction, StrategySignal, TradeSetup


NOW = datetime(2024, 1, 2, 15, tzinfo=timezone.utc)


def candidate(symbol):
    return Opportunity(symbol, NOW, OpportunityDirection.LONG, 1, 50, 47.5, 55, 2.5, 5, 2, ("fixture",), True)


def setup(symbol):
    return TradeSetup(symbol, Direction.LONG, 50, 47.5, 55, 2.5, 5, 2, NOW, "fixture", .5, "fixture")


class FixtureScanner:
    def __init__(self, candidates, failures=()):
        self.candidates = candidates
        self.failures = failures
        self.calls = 0

    def scan(self, universe):
        self.calls += 1
        return tuple(self.candidates)

    def scan_detailed(self, universe):
        candidates = self.scan(universe)
        return candidates, tuple(SymbolResult(symbol, None, "fixture scanner data failure") for symbol in self.failures)


class FixtureMarketData:
    def __init__(self, failures=()):
        self.failures = set(failures)

    def get_historical(self, symbol, start, end):
        if symbol in self.failures:
            raise RuntimeError("fixture market data failure")
        return (Bar(symbol, NOW - timedelta(minutes=1), 50, 51, 49, 50, 100), Bar(symbol, NOW, 50, 51, 49, 50, 100))


class FixtureStrategy:
    def __init__(self, failures=(), events=None):
        self.failures = set(failures)
        self.events = events if events is not None else []

    def evaluate(self, bars):
        symbol = bars[-1].symbol
        self.events.append(("strategy", symbol))
        if symbol in self.failures:
            raise RuntimeError("fixture strategy failure")
        return StrategySignal(symbol, NOW, Direction.LONG, setup(symbol), "fixture", "fixture")


class EventRisk(RiskEngine):
    def __init__(self, config=None, *, reject=(), events=None):
        super().__init__(config)
        self.reject = set(reject)
        self.events = events if events is not None else []

    def evaluate(self, proposed, portfolio):
        self.events.append(("risk", proposed.symbol))
        decision = super().evaluate(proposed, portfolio)
        if proposed.symbol in self.reject:
            from dataclasses import replace
            from backend.risk.models import RiskStatus
            return replace(decision, status=RiskStatus.REJECTED, quantity=0, reasons=("fixture rejection",))
        return decision


class EventExecution(MockExecutionProvider):
    def __init__(self, events, **kwargs):
        super().__init__(**kwargs)
        self.events = events

    def place_order(self, request):
        self.events.append(("execution", request.symbol))
        return super().place_order(request)


class FailingPortfolio(Portfolio):
    def open_position(self, symbol, direction, quantity, price, timestamp=None):
        if symbol == "BADPORT":
            raise RuntimeError("fixture portfolio update failure")
        return super().open_position(symbol, direction, quantity, price, timestamp)


class TradingOrchestratorTests(unittest.TestCase):
    def make_orchestrator(self, symbols=("AAA",), *, candidates=None, market_failures=(), strategy_failures=(), risk_rejections=(), risk_config=None, execution_fail=False, clock=lambda: NOW, ai_analysis=None, journal=None):
        events = []
        universe = SymbolUniverse(symbols)
        portfolio = Portfolio(100_000)
        risk = EventRisk(risk_config, reject=risk_rejections, events=events)
        execution = EventExecution(events, fail_orders=execution_fail)
        paper = PaperTradingEngine(risk, portfolio, execution, journal=journal)
        scanner = FixtureScanner(candidates if candidates is not None else [candidate(symbol) for symbol in symbols])
        orchestrator = TradingOrchestrator(
            market_data=FixtureMarketData(market_failures), scanner=scanner,
            strategy=FixtureStrategy(strategy_failures, events), risk_engine=risk,
            paper_trading=paper, portfolio=portfolio,
            config=OrchestratorConfig(universe, timedelta(seconds=1), timedelta(days=1)), clock=clock,
            ai_analysis=ai_analysis, journal=journal,
        )
        return orchestrator, scanner, paper, portfolio, events, execution

    def test_one_complete_cycle_and_ordering(self):
        orchestrator, _, paper, portfolio, events, _ = self.make_orchestrator()
        orchestrator.start = lambda: None
        # run_once is intentionally a single-cycle operation while active.
        orchestrator._status = OrchestratorStatus.RUNNING
        result = orchestrator.run_once()
        self.assertEqual(len(result.approved_trades), 1)
        self.assertEqual([event[0] for event in events], ["strategy", "risk", "execution"])
        self.assertEqual(len(portfolio.positions), 1)
        self.assertEqual(len(paper.trades), 1)
        self.assertEqual(orchestrator.state.last_scan_timestamp, NOW)

    def test_multiple_symbols_single_cycle(self):
        orchestrator, _, _, portfolio, events, _ = self.make_orchestrator(("AAA", "BBB"))
        orchestrator._status = OrchestratorStatus.RUNNING
        result = orchestrator.run_once()
        self.assertEqual(len(result.approved_trades), 2)
        self.assertEqual({p.symbol for p in portfolio.positions}, {"AAA", "BBB"})
        self.assertEqual(sum(event[0] == "execution" for event in events), 2)

    def test_scanner_invalid_candidate_never_reaches_strategy_or_execution(self):
        invalid = Opportunity("BAD", NOW, OpportunityDirection.LONG, 0, None, None, None, None, None, None, (), False)
        orchestrator, _, _, _, events, execution = self.make_orchestrator(("BAD",), candidates=[invalid])
        orchestrator._status = OrchestratorStatus.RUNNING
        result = orchestrator.run_once()
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual(events, [])
        self.assertEqual(execution.get_orders(), ())

    def test_price_at_or_above_candidate_limit_is_rejected_before_strategy(self):
        from dataclasses import replace
        orchestrator, _, _, _, events, execution = self.make_orchestrator(
            ("BOUNDARY",), candidates=[replace(candidate("BOUNDARY"), entry_price=100)]
        )
        orchestrator._status = OrchestratorStatus.RUNNING
        result = orchestrator.run_once()
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual(events, [])
        self.assertEqual(execution.get_orders(), ())

    def test_ai_unavailable_is_advisory_and_never_blocks_risk_or_execution(self):
        class Unavailable:
            def analyze(self, proposed, market_context=()):
                raise RuntimeError("local analysis unavailable")

        orchestrator, _, paper, _, events, _ = self.make_orchestrator(
            ai_analysis=AIAnalysisService(Unavailable()))
        result = orchestrator.run_once()
        self.assertEqual(len(result.approved_trades), 1)
        self.assertEqual([event[0] for event in events], ["strategy", "risk", "execution"])
        self.assertTrue(any("AI analysis unavailable" in error for error in orchestrator.state.errors))
        self.assertEqual(len(paper.trades), 1)

    def test_ai_reject_is_reported_as_advice_and_risk_remains_authoritative(self):
        from dataclasses import replace
        from backend.ai import AIRecommendation, MockAIAnalysisProvider

        class RejectAdvice:
            def analyze(self, proposed, market_context=()):
                return replace(MockAIAnalysisProvider().analyze(proposed, market_context),
                               recommendation=AIRecommendation.REJECT)

        orchestrator, _, _, _, events, _ = self.make_orchestrator(
            ai_analysis=AIAnalysisService(RejectAdvice()))
        result = orchestrator.run_once()
        self.assertEqual(len(result.ai_analyses), 1)
        self.assertEqual(result.ai_analyses[0].recommendation, AIRecommendation.REJECT)
        self.assertEqual(len(result.approved_trades), 1)
        self.assertEqual([event[0] for event in events], ["strategy", "risk", "execution"])

    def test_orchestrator_persists_setup_risk_execution_and_trade_lifecycle(self):
        from backend.ai import MockAIAnalysisProvider
        journal = SQLiteJournalRepository(":memory:")
        try:
            orchestrator, _, paper, _, _, _ = self.make_orchestrator(
                journal=journal, ai_analysis=AIAnalysisService(MockAIAnalysisProvider()))
            result = orchestrator.run_once()
            self.assertEqual(len(result.approved_trades), 1)
            self.assertEqual(len(result.ai_analyses), 1)
            self.assertEqual(len(journal.rows("signals")), 1)
            self.assertEqual(len(journal.rows("risk_decisions")), 1)
            self.assertEqual(len(journal.rows("ai_analyses")), 1)
            self.assertEqual(len(journal.rows("orders")), 1)
            self.assertEqual(len(journal.open_trades()), 1)
            paper.close("AAA", 55, NOW)
            self.assertEqual(len(journal.closed_trades()), 1)
            self.assertEqual(journal.closed_trades()[0]["context_json"]["setup"]["symbol"], "AAA")
            self.assertEqual(journal.closed_trades()[0]["context_json"]["ai_analysis"]["recommendation"], "SUPPORT")
        finally:
            journal.close()

    def test_database_failure_does_not_block_paper_trade(self):
        class BrokenJournal:
            def __getattr__(self, name):
                def fail(*args, **kwargs):
                    raise RuntimeError("database unavailable")
                return fail

        orchestrator, _, _, portfolio, _, _ = self.make_orchestrator(journal=BrokenJournal())
        result = orchestrator.run_once()
        self.assertEqual(len(result.approved_trades), 1)
        self.assertEqual(len(portfolio.positions), 1)

    def test_risk_rejection_never_reaches_execution(self):
        orchestrator, _, _, _, events, execution = self.make_orchestrator(risk_rejections=("AAA",))
        orchestrator._status = OrchestratorStatus.RUNNING
        result = orchestrator.run_once()
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual([e[0] for e in events], ["strategy", "risk"])
        self.assertEqual(execution.get_orders(), ())

    def test_missing_market_data_and_strategy_failures_are_isolated(self):
        orchestrator, _, _, portfolio, events, execution = self.make_orchestrator(
            ("GOOD", "NODATA", "BADSTRAT"), market_failures=("NODATA",), strategy_failures=("BADSTRAT",)
        )
        orchestrator._status = OrchestratorStatus.RUNNING
        result = orchestrator.run_once()
        self.assertEqual([trade.symbol for trade in result.approved_trades], ["GOOD"])
        self.assertEqual(len(result.errors), 2)
        self.assertEqual(len(portfolio.positions), 1)
        self.assertEqual(len(execution.get_orders()), 1)

    def test_execution_failure_and_state_error_recorded(self):
        orchestrator, _, _, portfolio, _, _ = self.make_orchestrator(execution_fail=True)
        orchestrator._status = OrchestratorStatus.RUNNING
        result = orchestrator.run_once()
        self.assertEqual(len(result.errors), 1)
        self.assertIn("did not fill", result.errors[0])
        self.assertEqual(portfolio.positions, ())
        self.assertTrue(orchestrator.state.errors)

    def test_portfolio_update_failure_does_not_stop_other_symbols(self):
        events = []
        portfolio = FailingPortfolio(100_000)
        risk = EventRisk(events=events)
        execution = EventExecution(events)
        paper = PaperTradingEngine(risk, portfolio, execution)
        orchestrator = TradingOrchestrator(
            market_data=FixtureMarketData(),
            scanner=FixtureScanner([candidate("BADPORT"), candidate("GOOD")]),
            strategy=FixtureStrategy(events=events), risk_engine=risk,
            paper_trading=paper, portfolio=portfolio,
            config=OrchestratorConfig(SymbolUniverse(["BADPORT", "GOOD"])), clock=lambda: NOW,
        )
        orchestrator._status = OrchestratorStatus.RUNNING
        result = orchestrator.run_once()
        self.assertEqual([trade.symbol for trade in result.approved_trades], ["GOOD"])
        self.assertTrue(any("portfolio update failure" in error for error in result.errors))
        self.assertEqual([position.symbol for position in portfolio.positions], ["GOOD"])

    def test_position_and_exposure_limits_respected(self):
        config = RiskConfig(max_simultaneous_positions=1, max_portfolio_exposure_fraction=.02)
        orchestrator, _, _, portfolio, _, execution = self.make_orchestrator(("AAA", "BBB"), risk_config=config)
        orchestrator._status = OrchestratorStatus.RUNNING
        orchestrator.run_once()
        self.assertEqual(len(portfolio.positions), 1)
        self.assertEqual(len(execution.get_orders()), 1)

    def test_run_once_works_without_starting_repeating_loop(self):
        orchestrator, scanner, _, _, _, execution = self.make_orchestrator()
        orchestrator.stop()
        result = orchestrator.run_once()
        self.assertEqual(result.cycle, 1)
        self.assertEqual(scanner.calls, 1)
        self.assertEqual(len(execution.get_orders()), 1)
        self.assertEqual(orchestrator.state.status, OrchestratorStatus.STOPPED)

    def test_start_and_stop_lifecycle(self):
        orchestrator, scanner, _, _, _, _ = self.make_orchestrator()
        completed = threading.Event()
        original_scan = scanner.scan

        def observed_scan(universe):
            result = original_scan(universe)
            completed.set()
            return result

        scanner.scan = observed_scan
        orchestrator.config = OrchestratorConfig(SymbolUniverse(["AAA"]), timedelta(hours=1), timedelta(days=1))
        worker = threading.Thread(target=orchestrator.start)
        worker.start()
        self.assertTrue(completed.wait(2))
        self.assertEqual(orchestrator.state.status, OrchestratorStatus.RUNNING)
        orchestrator.stop()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(orchestrator.state.status, OrchestratorStatus.STOPPED)
        self.assertEqual(scanner.calls, 1)

    def test_repeated_cycles_are_deterministic_and_counted(self):
        orchestrator, scanner, _, _, _, _ = self.make_orchestrator()
        orchestrator._status = OrchestratorStatus.RUNNING
        one = orchestrator.run_once()
        orchestrator.paper_trading.close("AAA", 50, NOW)
        two = orchestrator.run_once()
        self.assertEqual((one.cycle, two.cycle), (1, 2))
        self.assertEqual(scanner.calls, 2)
        self.assertEqual(orchestrator.state.current_cycle, 2)
        self.assertEqual(orchestrator.state.candidates_found, 2)

    def test_scanner_failure_is_recorded(self):
        orchestrator, _, _, _, _, execution = self.make_orchestrator()
        orchestrator.scanner.scan = lambda universe: (_ for _ in ()).throw(RuntimeError("scanner down"))
        orchestrator._status = OrchestratorStatus.RUNNING
        result = orchestrator.run_once()
        self.assertEqual(result.candidates, ())
        self.assertTrue(result.errors)
        self.assertEqual(execution.get_orders(), ())

    def test_scanner_detailed_failure_is_visible_in_cycle_and_state(self):
        orchestrator, scanner, _, _, _, _ = self.make_orchestrator(("GOOD", "MISSING"))
        scanner.failures = ("MISSING",)
        result = orchestrator.run_once()
        self.assertEqual(result.rejected_count, 1)
        self.assertTrue(any("MISSING" in error for error in result.errors))
        self.assertEqual(orchestrator.state.candidates_rejected, 1)
        self.assertTrue(any("MISSING" in error for error in orchestrator.state.errors))


if __name__ == "__main__":
    unittest.main()
