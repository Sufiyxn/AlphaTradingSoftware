import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from backend.config import load_config
from backend.core.runtime import ApplicationRuntime
from backend.core.server import _json_default, _positions_payload
from backend.database import SQLiteJournalRepository
from backend.market_data import MarketDataBridge, MarketDataService, Quote
from backend.market_data.bridge import CONNECTED, NOT_CONFIGURED, PRICED, STALE
from backend.paper_trading import (
    PaperTradingEngine,
    PaperTradeRecord,
    PositionValuationService,
    reconcile_journal,
)
from backend.portfolio import Portfolio
from backend.portfolio.models import PriceState
from backend.risk import RiskEngine
from backend.strategies.models import Direction
from backend.testing import FixtureHistoryProvider


NOW = datetime(2024, 1, 5, 15, 0, tzinfo=timezone.utc)
RUNTIME_CLOCK = datetime(2024, 1, 2, 15, 29, 0, 1, tzinfo=timezone.utc)


class QuoteProvider:
    is_configured = True
    provider = "test_fixture"

    def __init__(self, quotes=()):
        self._quotes = tuple(quotes)

    def get_historical(self, symbol, start, end):
        return ()

    def get_latest(self, symbols):
        wanted = {symbol.strip().upper() for symbol in symbols}
        return tuple(quote for quote in self._quotes if quote.symbol in wanted)


class UnreachableProvider:
    is_configured = False
    provider = "none"
    reason = "no market data provider is connected"

    def get_historical(self, symbol, start, end):
        raise AssertionError("an unconfigured provider must never be queried")

    def get_latest(self, symbols):
        raise AssertionError("an unconfigured provider must never be queried")


def harness(provider, *, stale_after=300.0, clock=None, config=None):
    current = clock or (lambda: NOW)
    portfolio = Portfolio(100_000, clock=current)
    engine = PaperTradingEngine(RiskEngine(), portfolio, config=config, clock=current)
    bridge = MarketDataBridge(MarketDataService(provider), clock=current,
                              stale_after_seconds=stale_after)
    return portfolio, engine, PositionValuationService(bridge, portfolio, engine)


def open_paper(portfolio, engine, symbol="AAA", *, direction=Direction.LONG, quantity=10,
               entry=100.0, stop=95.0, target=110.0, opened_at=NOW, order_id=None):
    portfolio.open_position(symbol, direction, quantity, entry, opened_at)
    engine.restore_trade(PaperTradeRecord(symbol, direction, opened_at, entry, stop, target,
                                          quantity, "valuation-test",
                                          order_id or f"PAPER-{symbol}"))


class PositionValuationTests(unittest.TestCase):
    def test_open_position_without_a_provider_stays_unpriced_with_no_zero_pnl(self):
        portfolio = Portfolio(10_000)
        position = portfolio.open_position("AAA", Direction.LONG, 10, 100.0, NOW)
        self.assertIsNone(position.market_price)
        self.assertIsNone(position.unrealized_pnl)
        self.assertEqual(position.price_state, PriceState.UNPRICED)
        snapshot = portfolio.snapshot()
        self.assertEqual(snapshot.unrealized_pnl, 0.0)
        self.assertEqual(snapshot.equity, 9_000.0)
        self.assertEqual(snapshot.exposure, 1_000.0)
        self.assertEqual(snapshot.valuation_status, "PARTIAL")
        self.assertEqual(snapshot.priced_position_count, 0)
        self.assertEqual(snapshot.unpriced_position_count, 1)

    def test_fresh_quote_marks_the_position_and_values_it(self):
        portfolio, engine, valuation = harness(QuoteProvider((Quote("AAA", NOW, 105.0, 10),)))
        open_paper(portfolio, engine)
        report = valuation.refresh(["AAA"])
        self.assertEqual(report.availability, CONNECTED)
        position = portfolio.positions[0]
        self.assertEqual(position.price_state, PriceState.FRESH)
        self.assertEqual(position.market_price, 105.0)
        self.assertEqual(position.unrealized_pnl, 50.0)
        self.assertEqual(position.price_as_of, NOW)
        self.assertTrue(position.auto_exit_enabled)
        snapshot = portfolio.snapshot()
        self.assertEqual(snapshot.valuation_status, "COMPLETE")
        self.assertEqual(snapshot.equity, 100_050.0)

    def test_automatic_stop_loss_fires_only_on_a_fresh_real_quote(self):
        portfolio, engine, valuation = harness(QuoteProvider((Quote("AAA", NOW, 94.0, 10),)))
        open_paper(portfolio, engine)
        closed = valuation.refresh(["AAA"])
        self.assertEqual(closed.by_symbol["AAA"].state, PRICED)
        self.assertEqual(portfolio.positions, ())
        self.assertLess(portfolio.snapshot().realized_pnl, 0)
        self.assertEqual(engine.trades, ())

    def test_automatic_target_fires_on_a_fresh_real_quote(self):
        portfolio, engine, valuation = harness(QuoteProvider((Quote("AAA", NOW, 112.0, 10),)))
        open_paper(portfolio, engine)
        valuation.refresh(["AAA"])
        self.assertEqual(portfolio.positions, ())
        self.assertGreater(portfolio.snapshot().realized_pnl, 0)
        self.assertEqual(portfolio.snapshot().trade_count, 1)

    def test_stale_quote_does_not_trigger_automatic_stop_loss(self):
        stale_at = NOW - timedelta(seconds=900)
        portfolio, engine, valuation = harness(
            QuoteProvider((Quote("AAA", stale_at, 94.0, 10),)))
        open_paper(portfolio, engine)
        report = valuation.refresh(["AAA"])
        self.assertEqual(report.availability, STALE)
        self.assertEqual(len(portfolio.positions), 1)
        self.assertEqual(len(engine.trades), 1)
        self.assertEqual(portfolio.snapshot().realized_pnl, 0.0)

    def test_stale_position_reports_real_price_timestamp_and_blocked_auto_exit(self):
        stale_at = NOW - timedelta(seconds=900)
        portfolio, engine, valuation = harness(
            QuoteProvider((Quote("AAA", stale_at, 94.0, 10),)))
        open_paper(portfolio, engine)
        valuation.refresh(["AAA"])
        position = portfolio.positions[0]
        self.assertEqual(position.price_state, PriceState.STALE)
        self.assertEqual(position.market_price, 94.0)
        self.assertEqual(position.price_as_of, stale_at)
        self.assertEqual(position.unrealized_pnl, -60.0)
        self.assertFalse(position.auto_exit_enabled)
        self.assertIn("staleness window", position.price_reason)

    def test_a_position_ages_to_stale_across_a_closed_session_without_inventing_a_price(self):
        # Friday 15:55 ET a real quote marks the position FRESH. After the
        # regular close, and all Saturday, no new quote arrives: the same real
        # price must age to STALE - never stay FRESH, never be replaced by an
        # invented value, and never trigger an automatic exit.
        holder = {"at": datetime(2026, 10, 9, 19, 55, tzinfo=timezone.utc)}
        clock = lambda: holder["at"]  # noqa: E731 - mutable fixture clock
        quote_at = holder["at"]
        portfolio, engine, valuation = harness(
            QuoteProvider((Quote("AAA", quote_at, 100.0, 10),)),
            stale_after=300.0, clock=clock)
        open_paper(portfolio, engine, opened_at=quote_at)
        valuation.refresh(["AAA"])
        self.assertEqual(portfolio.positions[0].price_state, PriceState.FRESH)

        holder["at"] = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)  # Saturday
        report = valuation.refresh(["AAA"])
        position = portfolio.positions[0]
        self.assertEqual(position.price_state, PriceState.STALE)
        self.assertEqual(position.market_price, 100.0)
        self.assertEqual(position.price_as_of, quote_at)
        self.assertFalse(position.auto_exit_enabled)
        self.assertEqual(len(engine.trades), 1, "no automatic exit fired on a stale mark")
        self.assertEqual(report.availability, STALE)

    def test_missing_quote_leaves_the_position_unpriced_with_a_reason(self):
        portfolio, engine, valuation = harness(QuoteProvider(()))
        open_paper(portfolio, engine)
        report = valuation.refresh(["AAA"])
        self.assertNotEqual(report.availability, CONNECTED)
        position = portfolio.positions[0]
        self.assertEqual(position.price_state, PriceState.UNPRICED)
        self.assertIsNone(position.market_price)
        self.assertIsNone(position.unrealized_pnl)
        self.assertIn("no quote", position.price_reason)
        self.assertEqual(engine.trades[0].symbol, "AAA")

    def test_unconfigured_provider_never_reaches_the_position(self):
        portfolio, engine, valuation = harness(UnreachableProvider())
        open_paper(portfolio, engine)
        report = valuation.refresh(["AAA"])
        self.assertEqual(report.availability, NOT_CONFIGURED)
        position = portfolio.positions[0]
        self.assertEqual(position.price_state, PriceState.UNPRICED)
        self.assertIsNone(position.market_price)
        self.assertEqual(len(portfolio.positions), 1)

    def test_unpriced_position_requires_an_explicit_price_to_close(self):
        portfolio, engine, _ = harness(UnreachableProvider())
        open_paper(portfolio, engine)
        with self.assertRaises(ValueError):
            portfolio.close_position("AAA")
        with self.assertRaises(ValueError):
            engine.close("AAA", None)

    def test_owner_can_close_an_unpriced_position_with_a_real_price(self):
        portfolio, engine, _ = harness(UnreachableProvider())
        open_paper(portfolio, engine, entry=100.0)
        pnl = engine.close("AAA", 105.0, timestamp=NOW)
        self.assertEqual(portfolio.positions, ())
        self.assertEqual(engine.trades, ())
        self.assertGreater(pnl, 0)
        self.assertEqual(portfolio.snapshot().trade_count, 1)

    def test_engine_refuses_a_non_positive_quote(self):
        portfolio, engine, valuation = harness(QuoteProvider((Quote("AAA", NOW, 115.0, 10),)))
        open_paper(portfolio, engine)
        valuation.refresh(["AAA"])
        with self.assertRaises(ValueError):
            engine.on_quote("AAA", NOW, 0)


class PositionsApiTests(unittest.TestCase):
    def build_runtime(self, *, with_fixture_data):
        config = load_config(env={})
        provider = (FixtureHistoryProvider(config.trading.universe, end=RUNTIME_CLOCK)
                    if with_fixture_data else None)
        return ApplicationRuntime(database_path=":memory:", owner_token="owner-test",
                                  clock=lambda: RUNTIME_CLOCK, config=config,
                                  market_data_provider=provider)

    def test_positions_payload_exposes_unpriced_state_and_availability(self):
        runtime = self.build_runtime(with_fixture_data=False)
        try:
            open_paper(runtime.portfolio, runtime.paper, "AAPL", entry=50.0,
                       opened_at=RUNTIME_CLOCK, stop=45.0, target=60.0)
            payload = _positions_payload(runtime)
            position = payload["positions"][0]
            self.assertEqual(position["price_state"], "UNPRICED")
            self.assertIsNone(position["current_price"])
            self.assertIsNone(position["unrealized_pnl"])
            self.assertIsNone(position["r_multiple"])
            self.assertIsNone(position["price_age_seconds"])
            self.assertFalse(position["auto_exit_enabled"])
            self.assertTrue(position["price_reason"])
            self.assertEqual(payload["portfolio"].valuation_status, "PARTIAL")
            self.assertEqual(payload["unpriced_position_count"], 1)
            self.assertEqual(payload["market_data_availability"], NOT_CONFIGURED)
            self.assertTrue(payload["market_data_availability_reason"])
            serialized = json.dumps(payload, default=_json_default)
            self.assertIn('"current_price": null', serialized)
            self.assertIn('"unrealized_pnl": null', serialized)
        finally:
            runtime.close()

    def test_positions_payload_reports_priced_state_with_a_real_quote(self):
        runtime = self.build_runtime(with_fixture_data=True)
        try:
            open_paper(runtime.portfolio, runtime.paper, "AAPL", entry=50.0,
                       opened_at=RUNTIME_CLOCK, stop=45.0, target=60.0)
            report = runtime.refresh_market()
            self.assertEqual(report.availability, CONNECTED)
            payload = _positions_payload(runtime)
            position = payload["positions"][0]
            self.assertEqual(position["price_state"], "FRESH")
            self.assertIsNotNone(position["current_price"])
            self.assertIsNotNone(position["price_as_of"])
            self.assertEqual(position["price_age_seconds"], 60.0)
            self.assertIsNone(position["price_reason"])
            self.assertTrue(position["auto_exit_enabled"])
            self.assertIsNotNone(position["unrealized_pnl"])
            self.assertEqual(payload["portfolio"].valuation_status, "COMPLETE")
            self.assertEqual(payload["market_data_availability"], CONNECTED)
            self.assertIsNone(payload["market_data_availability_reason"])
        finally:
            runtime.close()

    def test_runtime_status_exposes_availability_and_valuation_state(self):
        runtime = self.build_runtime(with_fixture_data=False)
        try:
            status = runtime.status()
            self.assertEqual(status["market_data_availability"], NOT_CONFIGURED)
            self.assertTrue(status["market_data_availability_reason"])
            self.assertIsNone(status["market_data_last_quote_at"])
            self.assertEqual(status["market_data_stale_after_seconds"], 300.0)
            self.assertEqual(status["valuation_status"], "COMPLETE")
            open_paper(runtime.portfolio, runtime.paper, "AAPL", entry=50.0,
                       opened_at=RUNTIME_CLOCK, stop=45.0, target=60.0)
            self.assertEqual(runtime.status()["valuation_status"], "PARTIAL")
            readiness = runtime.connections.readiness()
            self.assertEqual(readiness["market_data_availability"], NOT_CONFIGURED)
            self.assertTrue(readiness["market_data_availability_reason"])
        finally:
            runtime.close()


class JournalReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteJournalRepository(":memory:")

    def tearDown(self):
        self.repository.close()

    def seed(self):
        portfolio = Portfolio(100_000, clock=lambda: NOW)
        engine = PaperTradingEngine(RiskEngine(), portfolio, journal=self.repository,
                                    clock=lambda: NOW)
        open_paper(portfolio, engine, "AAA", entry=100.0, opened_at=NOW - timedelta(hours=1),
                   order_id="PAPER-AAA")
        self.repository.record_trade_open(engine.trades[0])
        open_paper(portfolio, engine, "BBB", entry=50.0, quantity=5,
                   opened_at=NOW - timedelta(hours=1), order_id="PAPER-BBB")
        self.repository.record_trade_open(engine.trades[1])
        engine.close("BBB", 60.0, timestamp=NOW)
        return portfolio, engine

    def test_restart_reconciles_positions_cash_and_realized_pnl(self):
        original, _ = self.seed()
        portfolio = Portfolio(100_000, clock=lambda: NOW)
        engine = PaperTradingEngine(RiskEngine(), portfolio, journal=self.repository,
                                    clock=lambda: NOW)
        summary = reconcile_journal(portfolio=portfolio, engine=engine,
                                    repository=self.repository, starting_cash=100_000,
                                    clock=lambda: NOW)
        self.assertTrue(summary["restored"])
        self.assertEqual(summary["open_positions"], 1)
        self.assertEqual(summary["closed_trades"], 1)
        self.assertEqual(summary["skipped_rows"], 0)
        self.assertEqual([position.symbol for position in portfolio.positions], ["AAA"])
        self.assertEqual([trade.symbol for trade in engine.trades], ["AAA"])
        self.assertEqual(portfolio.trade_count, 1)
        self.assertAlmostEqual(portfolio.realized_pnl, original.realized_pnl, places=6)
        self.assertAlmostEqual(portfolio.cash, original.cash, places=6)
        self.assertAlmostEqual(portfolio.daily_pnl, original.realized_pnl, places=6)
        events = [row for row in self.repository.events(limit=50)
                  if row.get("event_type") == "JOURNAL_RECONCILED"]
        self.assertEqual(len(events), 1)

    def test_restart_marks_restored_positions_unpriced_until_a_real_quote(self):
        self.seed()
        portfolio, engine, valuation = harness(UnreachableProvider())
        reconcile_journal(portfolio=portfolio, engine=engine, repository=self.repository,
                          starting_cash=100_000, clock=lambda: NOW)
        position = portfolio.positions[0]
        self.assertEqual(position.price_state, PriceState.UNPRICED)
        self.assertIsNone(position.market_price)
        self.assertIsNone(position.unrealized_pnl)
        self.assertIn("awaiting a real quote", position.price_reason)
        valuation.refresh(["AAA"])
        self.assertEqual(portfolio.positions[0].price_state, PriceState.UNPRICED)
        self.assertIsNone(portfolio.positions[0].market_price)

    def test_empty_journal_is_a_no_op(self):
        portfolio = Portfolio(100_000, clock=lambda: NOW)
        engine = PaperTradingEngine(RiskEngine(), portfolio, clock=lambda: NOW)
        summary = reconcile_journal(portfolio=portfolio, engine=engine,
                                    repository=self.repository, starting_cash=100_000,
                                    clock=lambda: NOW)
        self.assertFalse(summary["restored"])
        self.assertEqual(portfolio.positions, ())
        self.assertEqual(portfolio.cash, 100_000.0)
        self.assertEqual(portfolio.trade_count, 0)

    def test_runtime_restart_restores_open_trades_from_the_journal(self):
        database = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        database.close()
        os.unlink(database.name)
        try:
            config = load_config(env={})
            first = ApplicationRuntime(
                database_path=database.name, owner_token="owner-test",
                clock=lambda: RUNTIME_CLOCK, config=config,
                market_data_provider=FixtureHistoryProvider(config.trading.universe,
                                                             end=RUNTIME_CLOCK))
            try:
                cycle = first.orchestrator.run_once()
                self.assertGreaterEqual(len(cycle.approved_trades), 2)
                self.assertTrue(all(position.market_price is None
                                    for position in first.portfolio.positions))
                expected_positions = [position.symbol for position in first.portfolio.positions]
                expected_cash = first.portfolio.cash
            finally:
                first.close()

            second = ApplicationRuntime(
                database_path=database.name, owner_token="owner-test",
                clock=lambda: RUNTIME_CLOCK, config=config, market_data_provider=None)
            try:
                self.assertEqual(second.reconciliation["open_positions"],
                                 len(expected_positions))
                self.assertEqual([position.symbol for position in second.portfolio.positions],
                                 expected_positions)
                self.assertEqual([trade.symbol for trade in second.paper.trades],
                                 expected_positions)
                self.assertAlmostEqual(second.portfolio.cash, expected_cash, places=6)
                for position in second.portfolio.positions:
                    self.assertEqual(position.price_state, PriceState.UNPRICED)
                    self.assertIsNone(position.market_price)
                    self.assertIsNone(position.unrealized_pnl)
                payload = _positions_payload(second)
                self.assertEqual(payload["unpriced_position_count"], len(expected_positions))
                self.assertEqual(payload["portfolio"].valuation_status, "PARTIAL")
            finally:
                second.close()
        finally:
            os.unlink(database.name)


if __name__ == "__main__":
    unittest.main()
