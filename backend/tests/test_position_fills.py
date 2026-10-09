"""Phase 10 checkpoint 3 proof: positions are booked from durable fills.

These tests hold the guarantees CP3 exists for:

- a fill the provider reports *after* the order was submitted still reaches
  the position - a position is created from executions, not from the
  pipeline's immediate answer;
- booking is keyed by the fill's durable identity and gated on the fill
  journal's own insert-versus-replay answer, so replaying an execution never
  books it twice, before or after a restart;
- the trade record follows the position, so a restart rebuilds what the fills
  actually describe and exits close what was really bought;
- an order that fills later with no trade yet opens one on the setup it was
  risk-approved with - and a rejected or merely submitted order never does;
- a durable fill no trade accounts for is reported as a divergence at restart
  instead of being silently lost or turned into an invented position.
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from backend.database import SQLiteJournalRepository
from backend.execution import (
    Fill,
    MockTradingPlatformAdapter,
    OrderRequest,
    OrderStatus,
)
from backend.execution.models import Order as ProviderOrder
from backend.orders import OrderIntent, OrderLifecycleService
from backend.paper_trading import PaperTradingEngine, reconcile_journal
from backend.portfolio import Portfolio
from backend.risk import RiskEngine
from backend.strategies.models import Direction, TradeSetup

NOW = datetime(2024, 1, 8, 15, tzinfo=timezone.utc)
CLOCK = lambda: NOW  # noqa: E731 - the deterministic test clock


def trade_setup(symbol="ABC", entry=100.0, risk=5.0) -> TradeSetup:
    return TradeSetup(symbol, Direction.LONG, entry, entry - risk,
                      entry + risk * 2.0, risk, risk * 2.0, 2.0, NOW,
                      "cp3", 0.5, "deterministic")


class DeferredAdapter(MockTradingPlatformAdapter):
    """Accepts an order without filling it; the test hands the fill over later.

    This is the shape a real venue has: submission and execution are separate
    events, so the platform must open the position from the fill whenever it
    arrives rather than only inside the call that submitted the order.
    """

    def place_order(self, request: OrderRequest) -> ProviderOrder:
        order = ProviderOrder(self._mint_order_id(), request, OrderStatus.ACCEPTED,
                              0, None, self.clock(), ())
        self._orders[order.order_id] = order
        return order

    def deliver(self, order_id: str, *, quantity: float, price: float,
                timestamp: datetime, status=OrderStatus.FILLED) -> ProviderOrder:
        existing = self._orders[order_id]
        fill = Fill(quantity, price, timestamp)
        answered = ProviderOrder(existing.order_id, existing.request, status,
                                 quantity, price, existing.created_at, (fill,))
        self._orders[order_id] = answered
        return answered


def engine(*, adapter=None, journal=None, portfolio=None):
    portfolio = portfolio or Portfolio(100_000, clock=CLOCK)
    return PaperTradingEngine(RiskEngine(), portfolio, adapter, journal=journal,
                              clock=CLOCK), portfolio


def position_quantity(portfolio) -> float:
    return sum(position.quantity for position in portfolio.positions)


def event_types(repository, event_type: str) -> list:
    return [row for row in repository.events(limit=200)
            if row.get("event_type") == event_type]


class LateFillBookingTests(unittest.TestCase):
    def test_a_fill_reported_after_acceptance_becomes_a_position(self):
        adapter = MockTradingPlatformAdapter(partial_fill_quantity=2, clock=CLOCK)
        paper, portfolio = engine(adapter=adapter)
        result = paper.submit(trade_setup(), requested_quantity=10)
        self.assertEqual(result.order.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(position_quantity(portfolio), 2)
        self.assertEqual(paper.trades[0].quantity, 2)

        answered = adapter.get_order(result.order.provider_order_id)
        rest = Fill(8.0, 100.0, NOW)
        updated = paper.pipeline.orders.apply_provider_order(
            result.order.client_order_id,
            SimpleNamespace(order_id=answered.order_id,
                            status=OrderStatus.FILLED,
                            fills=tuple(answered.fills) + (rest,),
                            rejection_reason=None),
            now=NOW)

        self.assertEqual(updated.filled_quantity, 10)
        self.assertEqual(position_quantity(portfolio), 10,
                         "the late execution must reach the position")
        self.assertEqual(paper.trades[0].quantity, 10,
                         "the trade record must follow the position")
        self.assertAlmostEqual(
            portfolio.cash,
            100_000 - sum(fill.quantity * fill.price for fill in updated.fills),
            places=6, msg="cash follows the executions, slippage included")

    def test_replaying_the_same_late_answer_never_books_twice(self):
        adapter = MockTradingPlatformAdapter(partial_fill_quantity=2, clock=CLOCK)
        paper, portfolio = engine(adapter=adapter)
        result = paper.submit(trade_setup(), requested_quantity=10)
        answered = adapter.get_order(result.order.provider_order_id)
        answer = SimpleNamespace(order_id=answered.order_id,
                                 status=OrderStatus.FILLED,
                                 fills=tuple(answered.fills) + (Fill(8.0, 100.0, NOW),),
                                 rejection_reason=None)

        updated = paper.pipeline.orders.apply_provider_order(
            result.order.client_order_id, answer, now=NOW)
        expected_cash = 100_000 - sum(
            fill.quantity * fill.price for fill in updated.fills)
        for _ in range(2):
            paper.pipeline.orders.apply_provider_order(
                result.order.client_order_id, answer, now=NOW)

        self.assertEqual(position_quantity(portfolio), 10)
        self.assertEqual(paper.trades[0].quantity, 10)
        self.assertAlmostEqual(portfolio.cash, expected_cash, places=6,
                               msg="replays move neither the position nor cash")

    def test_an_order_accepted_without_a_fill_opens_its_position_when_it_fills(self):
        adapter = DeferredAdapter(clock=CLOCK)
        paper, portfolio = engine(adapter=adapter)
        result = paper.submit(trade_setup(), requested_quantity=10)
        self.assertEqual(result.code, "SUBMITTED")
        self.assertEqual(position_quantity(portfolio), 0)
        self.assertEqual(paper.trades, ())

        answered = adapter.get_order(result.order.provider_order_id)
        adapter.deliver(answered.order_id, quantity=10, price=100.0, timestamp=NOW)
        paper.pipeline.orders.apply_provider_order(
            result.order.client_order_id,
            adapter.get_order(answered.order_id), now=NOW)

        self.assertEqual(position_quantity(portfolio), 10)
        self.assertEqual(len(paper.trades), 1)
        self.assertEqual(paper.trades[0].quantity, 10)
        self.assertEqual(paper.trades[0].stop_price, 95.0,
                         "the trade opens on the setup risk approved")

    def test_the_initial_submission_still_books_exactly_once(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            paper, portfolio = engine(journal=journal)
            result = paper.submit(trade_setup(), requested_quantity=10)
            self.assertEqual(position_quantity(portfolio), 10)
            # Replaying the provider's own acknowledgement changes nothing.
            answered = SimpleNamespace(
                order_id=result.order.provider_order_id,
                status=OrderStatus.FILLED, fills=tuple(result.order.fills),
                rejection_reason=None)
            paper.pipeline.orders.apply_provider_order(
                result.order.client_order_id, answered, now=NOW)
            self.assertEqual(position_quantity(portfolio), 10)
            self.assertEqual(len(journal.rows("fills")), 1)
            self.assertEqual(len(journal.rows("position_events")), 1)
        finally:
            journal.close()


class DurableFillGateTests(unittest.TestCase):
    def service(self, journal):
        service = OrderLifecycleService(journal=journal, clock=CLOCK, restore=False)
        service.create(OrderIntent("CLIENT-1", "ABC", Direction.LONG, 10.0),
                       now=NOW, connection_id="paper",
                       provider_instance_id="inst-1")
        service.mark_submitted("CLIENT-1", "PROV-1", now=NOW,
                               provider_instance_id="inst-1")
        return service

    def test_a_fill_the_journal_already_held_is_not_announced(self):
        from backend.execution.fill_identity import normalize_fill

        journal = SQLiteJournalRepository(":memory:")
        try:
            service = self.service(journal)
            identified = normalize_fill(
                Fill(10.0, 100.0, NOW), connection_id="paper",
                provider_instance_id="inst-1", client_order_id="CLIENT-1",
                provider_order_id="PROV-1")
            journal.record_provider_fill(
                identified, order_id="CLIENT-1", client_order_id="CLIENT-1",
                symbol="ABC", connection_id="paper",
                provider_instance_id="inst-1")

            announced = []
            service.fill_observer = lambda order, fills: announced.append(fills)
            order = service.apply_provider_order(
                "CLIENT-1",
                SimpleNamespace(order_id="PROV-1", status=OrderStatus.FILLED,
                                fills=(Fill(10.0, 100.0, NOW),),
                                rejection_reason=None),
                now=NOW)

            self.assertEqual(order.filled_quantity, 10.0,
                             "the order still adopts the execution")
            self.assertEqual(announced, [],
                             "a fill the journal already held is never "
                             "announced, so it can never be booked again")
            discrepancies = event_types(journal, "RECONCILIATION_DISCREPANCY")
            self.assertTrue(any(
                (row.get("context_json") or {}).get("kind")
                == "DURABLE_FILL_UNKNOWN_TO_ORDER" for row in discrepancies),
                "the disagreement is reported rather than assumed fine")
        finally:
            journal.close()

    def test_a_new_fill_is_announced_once_with_its_durable_identity(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            service = self.service(journal)
            announced = []
            service.fill_observer = lambda order, fills: announced.append(
                (order.order_id, [fill.fill_id for fill in fills]))
            answer = SimpleNamespace(order_id="PROV-1", status=OrderStatus.FILLED,
                                     fills=(Fill(4.0, 100.0, NOW),),
                                     rejection_reason=None)
            service.apply_provider_order("CLIENT-1", answer, now=NOW)
            service.apply_provider_order("CLIENT-1", answer, now=NOW)

            self.assertEqual(len(announced), 1, "only the first delivery counts")
            self.assertEqual(announced[0][0], "CLIENT-1")
            fill_ids = announced[0][1]
            self.assertEqual(len(fill_ids), 1)
            self.assertIsInstance(fill_ids[0], str)
            self.assertTrue(fill_ids[0],
                            "the fill is announced under its durable identity")
            self.assertEqual(len(journal.rows("fills")), 1)
        finally:
            journal.close()


class RestartBookingTests(unittest.TestCase):
    def test_a_restart_rebuilds_the_position_the_fills_describe(self):
        database = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        database.close()
        os.unlink(database.name)
        try:
            first_journal = SQLiteJournalRepository(database.name)
            adapter = MockTradingPlatformAdapter(partial_fill_quantity=2, clock=CLOCK)
            first, portfolio = engine(adapter=adapter, journal=first_journal)
            result = first.submit(trade_setup(), requested_quantity=10)
            answered = adapter.get_order(result.order.provider_order_id)
            first.pipeline.orders.apply_provider_order(
                result.order.client_order_id,
                SimpleNamespace(order_id=answered.order_id,
                                status=OrderStatus.FILLED,
                                fills=tuple(answered.fills) + (Fill(8.0, 100.0, NOW),),
                                rejection_reason=None),
                now=NOW)
            self.assertEqual(position_quantity(portfolio), 10)
            client_order_id = result.order.client_order_id
            answer = SimpleNamespace(
                order_id=answered.order_id, status=OrderStatus.FILLED,
                fills=tuple(adapter.get_order(answered.order_id).fills),
                rejection_reason=None)
            first_journal.close()

            second_journal = SQLiteJournalRepository(database.name)
            try:
                second, restored = engine(journal=second_journal)
                summary = reconcile_journal(
                    portfolio=restored, engine=second, repository=second_journal,
                    starting_cash=100_000, clock=CLOCK)
                self.assertEqual(summary["open_positions"], 1)
                self.assertEqual(summary.get("fill_divergences"), 0,
                                 "a filled order with its trade is not a divergence")
                self.assertEqual(position_quantity(restored), 10)
                self.assertEqual(second.trades[0].quantity, 10)
                order_after_restart = next(
                    order for order in second.pipeline.orders.list()
                    if order.order_id == client_order_id)
                expected_cash = 100_000 - sum(
                    fill.quantity * fill.price for fill in order_after_restart.fills)
                self.assertAlmostEqual(restored.cash, expected_cash, places=6)

                # The same provider answer after a restart books nothing new.
                cash_before_replay = restored.cash
                second.pipeline.orders.apply_provider_order(
                    client_order_id, answer, now=NOW)
                self.assertEqual(position_quantity(restored), 10)
                self.assertEqual(second.trades[0].quantity, 10)
                self.assertAlmostEqual(restored.cash, cash_before_replay,
                                       places=6)
            finally:
                second_journal.close()
        finally:
            os.unlink(database.name)

    def test_a_durable_fill_no_trade_accounts_for_is_reported_not_invented(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            service = OrderLifecycleService(journal=journal, clock=CLOCK,
                                            restore=False)
            service.create(OrderIntent("CLIENT-9", "XYZ", Direction.LONG, 10.0),
                           now=NOW, connection_id="paper",
                           provider_instance_id="inst-1")
            service.mark_submitted("CLIENT-9", "PROV-9", now=NOW,
                                   provider_instance_id="inst-1")
            service.apply_provider_order(
                "CLIENT-9",
                SimpleNamespace(order_id="PROV-9", status=OrderStatus.FILLED,
                                fills=(Fill(10.0, 50.0, NOW),),
                                rejection_reason=None),
                now=NOW)
            self.assertEqual(len(journal.rows("fills")), 1)
            self.assertEqual(journal.open_trades(), ())

            portfolio = Portfolio(100_000, clock=CLOCK)
            restored, restored_portfolio = engine(journal=journal,
                                                  portfolio=portfolio)
            summary = reconcile_journal(
                portfolio=restored_portfolio, engine=restored,
                repository=journal, starting_cash=100_000, clock=CLOCK)

            self.assertEqual(summary["fill_divergences"], 1)
            self.assertEqual(restored_portfolio.positions, (),
                             "a position is never invented from a divergence")
            reported = [row for row in journal.events(limit=200)
                        if row.get("event_type") == "RECONCILIATION_DISCREPANCY"]
            self.assertTrue(any(
                (row.get("context_json") or {}).get("kind")
                == "DURABLE_FILL_WITHOUT_TRADE" for row in reported))
        finally:
            journal.close()


class JournalFreeBookingTests(unittest.TestCase):
    def test_late_fills_still_book_without_a_journal_attached(self):
        adapter = MockTradingPlatformAdapter(partial_fill_quantity=2, clock=CLOCK)
        paper, portfolio = engine(adapter=adapter)
        result = paper.submit(trade_setup(), requested_quantity=10)
        answered = adapter.get_order(result.order.provider_order_id)
        paper.pipeline.orders.apply_provider_order(
            result.order.client_order_id,
            SimpleNamespace(order_id=answered.order_id,
                            status=OrderStatus.FILLED,
                            fills=tuple(answered.fills) + (Fill(8.0, 100.0, NOW),),
                            rejection_reason=None),
            now=NOW)
        self.assertEqual(position_quantity(portfolio), 10)
        self.assertEqual(paper.trades[0].quantity, 10)

    def test_an_order_that_can_no_longer_fill_keeps_nothing(self):
        adapter = DeferredAdapter(clock=CLOCK)
        paper, portfolio = engine(adapter=adapter)
        result = paper.submit(trade_setup(), requested_quantity=10)
        self.assertEqual(result.code, "SUBMITTED")
        order_id = result.order.order_id
        self.assertIn(order_id, paper._setups,
                      "the approved setup is held while the order can still fill")

        paper.pipeline.orders.apply_status(order_id, OrderStatus.CANCELLED, NOW)
        paper._prune_pending_setups()

        self.assertNotIn(order_id, paper._setups)
        self.assertNotIn(order_id, paper._risks)
        self.assertNotIn(order_id, paper._analyses)
        self.assertEqual(portfolio.positions, (),
                         "an order that never executed opens no position")


if __name__ == "__main__":
    unittest.main()
