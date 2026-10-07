"""Phase 9 proof: order lifecycle, fills, reconciliation, and cancellation.

These tests hold the platform's model of an order rather than a provider's:

- one canonical status vocabulary with a state machine that refuses illegal
  moves instead of quietly jumping status
- executed quantity that is always derived from the recorded fills, so an
  order can never claim more than the executions behind it
- open orders that reserve risk capacity, so two intents cannot size
  themselves against the same capital
- a read-only reconciler whose clean answer is only allowed when the
  provider's snapshot said it was complete and error-free
- cancellation outcomes that stay honest about what the provider did
- journal persistence so a restart rebuilds the open-order picture, and the
  HTTP surface that exposes all of it
"""
import json
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib import request as urlrequest
from urllib.error import HTTPError

from backend.config import load_config
from backend.connections import ExecutionTimeoutError
from backend.core.runtime import ApplicationRuntime
from backend.core.server import _json_default, create_server
from backend.database import SQLiteJournalRepository
from backend.database.serialization import to_json
from backend.execution import OrderStatus, OrderType, normalize_order_status
from backend.orders import (
    ORDER_EVENTS,
    CancellationOutcome,
    DiscrepancyKind,
    Fill,
    InvalidOrderTransition,
    Order,
    OrderIntent,
    OrderLifecycleService,
    OrderStatus as CanonicalOrderStatus,
    OrderStore,
    ProviderStateSnapshot,
    RemoteFill,
    RemoteOrder,
    RemotePosition,
    ReconcileStatus,
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
    can_transition,
    fill_totals,
    next_states,
    order_from_dict,
    with_status,
)
from backend.pipeline import PipelineCode
from backend.portfolio import PendingOrderReservation, PortfolioSnapshot
from backend.risk import RiskCode, RiskConfig
from backend.risk.sizing import sizing_terms
from backend.strategies.models import Direction
from backend.testing import ScriptedAdapter

from backend.tests.test_pipeline import (
    SpyAdapter,
    make_intent,
    make_pipeline,
    trade_setup,
)

NOW = datetime(2024, 1, 8, 15, tzinfo=timezone.utc)
LATER = datetime(2024, 1, 8, 15, 0, 1, tzinfo=timezone.utc)
CLOCK = lambda: NOW  # noqa: E731 - deterministic fixture clock
OWNER_TOKEN = "owner-order-lifecycle"


# --------------------------------------------------------------------- fixtures
def order_intent(client="CLIENT-1", *, symbol="ABC", quantity=100.0,
                 limit_price=100.0, stop_price=None,
                 direction=Direction.LONG) -> OrderIntent:
    return OrderIntent(client_order_id=client, symbol=symbol, direction=direction,
                       quantity=quantity, order_type=OrderType.LIMIT,
                       limit_price=limit_price, stop_price=stop_price)


def fill(quantity, price, *, order_id="CLIENT-1", fill_id=None,
         provider_fill_id=None, provider_order_id=None) -> Fill:
    return Fill(quantity=quantity, price=price, timestamp=NOW, order_id=order_id,
                fill_id=fill_id, provider_fill_id=provider_fill_id,
                provider_order_id=provider_order_id, side=Direction.LONG)


def service(journal=None, *, executor=None) -> OrderLifecycleService:
    return OrderLifecycleService(journal=journal, executor=executor, clock=CLOCK,
                                 restore=False)


def submitted_order(service_, *, client="CLIENT-1", provider_order_id="PROV-1",
                    status=OrderStatus.ACCEPTED) -> Order:
    service_.create(order_intent(client))
    service_.mark_submitted(client, provider_order_id)
    if status is not OrderStatus.SUBMITTED:
        service_.apply_status(client, status)
    return service_.get(client)


class RecordingExecutor:
    """A provider that answers cancellation exactly as each test scripts it."""

    def __init__(self, *, status=OrderStatus.CANCELLED, raises=None,
                 fills=()) -> None:
        self.status = status
        self.raises = raises
        self.fills = fills
        self.calls: list[str] = []

    def cancel_order(self, provider_order_id: str):
        self.calls.append(provider_order_id)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(order_id=provider_order_id, status=self.status,
                               fills=self.fills)


class OrderStateMachineTests(unittest.TestCase):
    def test_the_documented_lifecycle_chain_is_legal(self):
        chain = [OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
                 OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED]
        for source, target in zip(chain, chain[1:]):
            self.assertTrue(can_transition(source, target),
                            f"{source.value} -> {target.value} must be legal")

    def test_every_terminal_status_is_final(self):
        self.assertEqual(TERMINAL_STATUSES, frozenset({
            OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED,
            OrderStatus.EXPIRED, OrderStatus.FAILED}))
        for status in TERMINAL_STATUSES:
            self.assertEqual(next_states(status), frozenset({status}),
                             f"{status.value} must never move again")

    def test_illegal_transitions_are_refused_rather_than_jumping_status(self):
        filled = Order(order_intent(), OrderStatus.FILLED, NOW)
        cancelled = Order(order_intent(), OrderStatus.CANCELLED, NOW)
        fresh = Order(order_intent(), OrderStatus.NEW, NOW)
        for source, target in ((filled, OrderStatus.CANCELLED),
                               (cancelled, OrderStatus.FILLED),
                               (fresh, OrderStatus.FILLED),
                               (fresh, OrderStatus.ACCEPTED)):
            self.assertFalse(can_transition(source.status, target),
                             f"{source.status.value} -> {target.value} must be refused")
            with self.assertRaises(InvalidOrderTransition):
                with_status(source, target, NOW)

    def test_reapplying_the_current_status_is_idempotent(self):
        order = Order(order_intent(), OrderStatus.SUBMITTED, NOW,
                      provider_order_id="PROV-1")
        again = with_status(order, OrderStatus.SUBMITTED, LATER)
        self.assertIs(again.status, OrderStatus.SUBMITTED)
        self.assertEqual(again.updated_at, LATER)
        self.assertEqual(again.provider_order_id, "PROV-1")

    def test_an_unknown_state_recovers_but_a_terminal_state_does_not(self):
        self.assertTrue(can_transition(OrderStatus.UNKNOWN, OrderStatus.ACCEPTED))
        self.assertTrue(can_transition(OrderStatus.UNKNOWN, OrderStatus.UNKNOWN))
        self.assertFalse(can_transition(OrderStatus.FILLED, OrderStatus.UNKNOWN))
        self.assertFalse(can_transition(OrderStatus.CANCELLED, OrderStatus.UNKNOWN))
        # UNKNOWN is only reachable from a state we do not fully understand.
        for status in (OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.PENDING,
                       OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED):
            self.assertIn(OrderStatus.UNKNOWN, VALID_TRANSITIONS[status],
                          f"{status.value} must be able to fall back to UNKNOWN")

    def test_a_provider_new_word_is_not_the_local_only_new_state(self):
        self.assertIs(OrderStatus.NEW, CanonicalOrderStatus.NEW)
        self.assertIs(normalize_order_status("new"), OrderStatus.ACCEPTED)
        self.assertIsNot(normalize_order_status("new"), OrderStatus.NEW)

    def test_next_states_always_includes_the_current_state(self):
        for status in OrderStatus:
            self.assertIn(status, next_states(status))


class OrderIdentityTests(unittest.TestCase):
    def test_client_order_id_is_never_a_symbol_or_a_timestamp(self):
        self.assertEqual(order_intent("abc-123").client_order_id, "abc-123")
        self.assertEqual(order_intent("abc-123").reference_price, 100.0)

        pipeline, _, _, _, _ = make_pipeline()
        result = pipeline.execute(make_intent(setup=trade_setup("ABC")))
        self.assertEqual(result.code, PipelineCode.ACCEPTED)
        self.assertEqual(len(result.order.order_id), 32)
        self.assertEqual(result.order.order_id, result.order.client_order_id)
        self.assertNotIn("ABC", result.order.order_id)
        self.assertNotIn("2024", result.order.order_id)
        int(result.order.order_id, 16)  # a uuid4 hex string, not a readable name

    def test_provider_order_id_is_only_ever_what_the_provider_answered(self):
        lifecycle = service()
        created = lifecycle.create(order_intent())
        self.assertIsNone(created.provider_order_id)
        acknowledged = lifecycle.mark_submitted(created.client_order_id, "PROV-7")
        self.assertEqual(acknowledged.provider_order_id, "PROV-7")
        # A provider that answers without an id never erases the one we have.
        still = lifecycle.mark_submitted(created.client_order_id, None)
        self.assertEqual(still.provider_order_id, "PROV-7")

    def test_the_order_exposes_the_journal_compatible_request_and_id(self):
        order = service().create(order_intent(symbol="aapl", quantity=42.0))
        self.assertEqual(order.order_id, order.client_order_id)
        self.assertEqual(order.request.symbol, "aapl")
        self.assertEqual(order.request.quantity, 42.0)
        self.assertIs(order.request.order_type, OrderType.LIMIT)
        self.assertEqual(order.symbol, "aapl")

    def test_reference_price_is_the_orders_own_price_never_a_quote(self):
        self.assertEqual(order_intent(limit_price=100, stop_price=95).reference_price, 100)
        self.assertEqual(order_intent(limit_price=None, stop_price=95).reference_price, 95)
        self.assertIsNone(order_intent(limit_price=None, stop_price=None).reference_price)

    def test_describe_is_json_ready_and_carries_no_secrets(self):
        lifecycle = service()
        order = submitted_order(lifecycle)
        lifecycle.record_fill(order.client_order_id,
                              fill(40, 100.0, provider_fill_id="PF-1"))
        document = lifecycle.get(order.client_order_id).describe()
        encoded = json.dumps(document, default=_json_default)
        self.assertEqual(json.loads(encoded)["status"], "PARTIALLY_FILLED")
        self.assertEqual(document["order_id"], document["client_order_id"])
        self.assertEqual(document["filled_quantity"], 40.0)
        self.assertEqual(document["remaining_quantity"], 60.0)
        for forbidden in ("api_key", "api_secret", "password", "token"):
            self.assertNotIn(forbidden, encoded.lower())


class FillAccountingTests(unittest.TestCase):
    def test_filled_quantity_is_the_sum_of_the_fills(self):
        quantity, _ = fill_totals((fill(40, 100.0), fill(60, 102.0)))
        self.assertEqual(quantity, 100.0)

    def test_average_fill_price_is_volume_weighted_not_averaged(self):
        _, average = fill_totals((fill(1, 100.0), fill(9, 110.0)))
        self.assertEqual(average, 109.0)
        quantity, empty = fill_totals(())
        self.assertEqual(quantity, 0.0)
        self.assertIsNone(empty)

    def test_a_partial_fill_then_the_rest_reaches_filled(self):
        lifecycle = service()
        order = submitted_order(lifecycle, status=OrderStatus.SUBMITTED)
        first = lifecycle.record_fill(order.client_order_id, fill(40, 100.0))
        self.assertIs(first.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(first.filled_quantity, 40.0)
        self.assertEqual(first.remaining_quantity, 60.0)
        second = lifecycle.record_fill(order.client_order_id, fill(60, 102.0))
        self.assertIs(second.status, OrderStatus.FILLED)
        self.assertEqual(second.filled_quantity, 100.0)
        self.assertAlmostEqual(second.average_fill_price, 101.2)
        self.assertEqual(second.remaining_quantity, 0.0)
        self.assertFalse(second.is_open)

    def test_submitting_an_order_creates_no_fill_at_all(self):
        lifecycle = service()
        order = submitted_order(lifecycle, status=OrderStatus.SUBMITTED)
        self.assertEqual(order.fills, ())
        self.assertEqual(order.filled_quantity, 0.0)
        self.assertEqual(order.remaining_quantity, order.quantity)
        self.assertTrue(order.is_open)
        self.assertFalse(order.has_fills)

    def test_replaying_the_same_provider_fill_never_double_counts(self):
        lifecycle = service()
        order = submitted_order(lifecycle, status=OrderStatus.SUBMITTED)
        reported = fill(40, 100.0, provider_fill_id="PF-1",
                        provider_order_id=order.provider_order_id)
        once = lifecycle.record_fill(order.client_order_id, reported)
        twice = lifecycle.record_fill(order.client_order_id, reported)
        self.assertEqual(len(twice.fills), 1)
        self.assertEqual(twice.filled_quantity, 40.0)
        self.assertEqual(once.filled_quantity, twice.filled_quantity)

    def test_quantity_beyond_the_fills_is_journaled_and_never_invented(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            lifecycle = service(journal)
            order = submitted_order(lifecycle, status=OrderStatus.SUBMITTED)
            lifecycle.note_fill_gap(order.client_order_id, 50.0)
            recorded = lifecycle.get(order.client_order_id)
            self.assertEqual(recorded.fills, ())
            self.assertEqual(recorded.filled_quantity, 0.0)
            findings = [event for event in journal.events()
                        if event["event_type"] == "RECONCILIATION_DISCREPANCY"]
            self.assertTrue(findings)
            self.assertEqual(findings[0]["context_json"]["reported_filled_quantity"], 50.0)
            self.assertEqual(findings[0]["context_json"]["recorded_fill_quantity"], 0.0)
        finally:
            journal.close()

    def test_a_fill_that_arrives_after_a_terminal_state_is_a_discrepancy(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            lifecycle = service(journal)
            order = submitted_order(lifecycle, status=OrderStatus.CANCELLED)
            updated = lifecycle.record_fill(order.client_order_id, fill(10, 100.0))
            self.assertIs(updated.status, OrderStatus.CANCELLED,
                          "a cancelled order is never rewritten by a late fill")
            self.assertEqual(updated.filled_quantity, 10.0)
            self.assertIn("RECONCILIATION_DISCREPANCY",
                          [event["event_type"] for event in journal.events()])
        finally:
            journal.close()

    def test_order_round_trips_through_the_journals_serialized_form(self):

        lifecycle = service()
        order = submitted_order(lifecycle)
        order = lifecycle.record_fill(order.client_order_id,
                                      fill(40, 100.0, provider_fill_id="PF-1"))
        rebuilt = order_from_dict(json.loads(to_json(order)))
        self.assertEqual(rebuilt.client_order_id, order.client_order_id)
        self.assertIs(rebuilt.status, order.status)
        self.assertEqual(rebuilt.provider_order_id, order.provider_order_id)
        self.assertEqual(rebuilt.filled_quantity, order.filled_quantity)
        self.assertEqual(len(rebuilt.fills), 1)
        self.assertEqual(rebuilt.intent.symbol, order.intent.symbol)
        self.assertEqual(rebuilt.fills[0].provider_fill_id, "PF-1")
        with self.assertRaises(ValueError):
            order_from_dict({"status": "FILLED"})


class PipelineLifecycleTests(unittest.TestCase):
    def test_the_order_travels_new_submitted_filled_through_the_pipeline(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            pipeline, _, _, _, _ = make_pipeline(journal=journal)
            result = pipeline.execute(make_intent())
            self.assertEqual(result.code, PipelineCode.ACCEPTED)
            order = result.order
            self.assertIsInstance(order, Order)
            self.assertIs(order.status, OrderStatus.FILLED)
            self.assertEqual(order.order_id, order.client_order_id)
            self.assertTrue(order.provider_order_id)
            self.assertTrue(order.has_fills)
            self.assertEqual(len(order.fills), 1)
            self.assertEqual(order.filled_quantity, order.quantity)
            self.assertFalse(order.is_open)
            # NEW is a pre-submission state only; it never survives a submit.
            self.assertEqual([item for item in pipeline.orders.list()
                              if item.status is OrderStatus.NEW], [])
            self.assertEqual(len(pipeline.orders.pending_reservations()), 0)
            events = [event["event_type"] for event in journal.events()]
            self.assertIn("EXECUTION_SUBMITTED", events)
            self.assertIn("ORDER_SUBMITTED", events)
            self.assertIn("ORDER_FILL_RECEIVED", events)
        finally:
            journal.close()

    def test_an_accepted_but_unfilled_order_is_submitted_not_rejected(self):
        pipeline, _, adapter, _, _ = make_pipeline(
            adapter=ScriptedAdapter(authenticated=True, clock=CLOCK))
        result = pipeline.execute(make_intent(limit_price=100.0))
        self.assertEqual(result.code, PipelineCode.SUBMITTED)
        self.assertIsNotNone(result.order)
        self.assertIs(result.order.status, OrderStatus.SUBMITTED)
        self.assertEqual(len(adapter.get_orders()), 1)
        self.assertIn("has not filled it", result.reason)
        self.assertEqual(len(pipeline.orders.pending_reservations()), 1)

    def test_a_failed_submission_leaves_no_order_and_journals_the_attempt(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            adapter = SpyAdapter(raises=ExecutionTimeoutError("timed out"),
                                 clock=CLOCK)
            pipeline, _, _, _, _ = make_pipeline(adapter=adapter, journal=journal)
            result = pipeline.execute(make_intent())
            self.assertEqual(result.code, PipelineCode.PROVIDER_TIMEOUT)
            self.assertIsNone(result.order)
            self.assertEqual(pipeline.orders.list(), (),
                             "an order the platform cannot prove must not exist")
            self.assertEqual(pipeline.orders.pending_reservations(), ())
            failed = [event for event in journal.events()
                      if event["event_type"] == "ORDER_SUBMISSION_FAILED"]
            self.assertTrue(failed, "the attempt must still be auditable")
            self.assertEqual(failed[0]["context_json"]["symbol"], "ABC")
            self.assertEqual(failed[0]["context_json"]["quantity"], 200.0)
        finally:
            journal.close()

    def test_a_risk_rejected_intent_never_reaches_the_store_or_the_adapter(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            pipeline, _, adapter, _, _ = make_pipeline(journal=journal)
            result = pipeline.execute(make_intent(setup=trade_setup("ABC", ratio=0.5)))
            self.assertEqual(result.code, PipelineCode.RISK_REJECTED)
            self.assertEqual(result.risk.code, RiskCode.REWARD_RISK_TOO_LOW)
            self.assertIsNone(result.order)
            self.assertEqual(pipeline.orders.list(), ())
            self.assertEqual(adapter.requests, [])
            self.assertNotIn("ORDER_CREATED",
                             [event["event_type"] for event in journal.events()])
        finally:
            journal.close()

    def test_an_open_order_reserves_capacity_for_the_next_intent(self):
        pipeline, _, adapter, _, _ = make_pipeline(
            adapter=ScriptedAdapter(authenticated=True, clock=CLOCK))
        first = pipeline.execute(make_intent(setup=trade_setup("AAA"),
                                             limit_price=100.0,
                                             requested_quantity=10))
        self.assertEqual(first.code, PipelineCode.SUBMITTED)
        reservations = pipeline.orders.pending_reservations()
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0].symbol, "AAA")
        self.assertEqual(reservations[0].notional, 10 * 100.0)

        second = pipeline.execute(make_intent(setup=trade_setup("AAA"),
                                              limit_price=100.0,
                                              requested_quantity=10))
        self.assertEqual(second.code, PipelineCode.RISK_REJECTED)
        self.assertEqual(second.risk.code, RiskCode.DUPLICATE_SYMBOL)
        self.assertEqual(second.risk.pending_exposure, 10 * 100.0)
        self.assertEqual(len(adapter.get_orders()), 1,
                         "the second intent must not have reached the provider")

    def test_pending_orders_count_toward_the_maximum_position_slots(self):
        pipeline, _, adapter, _, _ = make_pipeline(
            adapter=ScriptedAdapter(authenticated=True, clock=CLOCK))
        for symbol in ("AAA", "BBB", "CCC", "DDD", "EEE"):
            result = pipeline.execute(make_intent(setup=trade_setup(symbol),
                                                  limit_price=100.0,
                                                  requested_quantity=10))
            self.assertEqual(result.code, PipelineCode.SUBMITTED, symbol)
        self.assertEqual(len(pipeline.orders.pending_reservations()), 5)
        overflow = pipeline.execute(make_intent(setup=trade_setup("FFF"),
                                                limit_price=100.0,
                                                requested_quantity=10))
        self.assertEqual(overflow.code, PipelineCode.RISK_REJECTED)
        self.assertEqual(overflow.risk.code, RiskCode.MAX_POSITIONS_REACHED)
        self.assertEqual(len(adapter.get_orders()), 5)

    def test_a_fill_releases_the_capacity_it_used_to_reserve(self):
        pipeline, _, _, _, _ = make_pipeline()
        result = pipeline.execute(make_intent())
        self.assertEqual(result.code, PipelineCode.ACCEPTED)
        self.assertEqual(pipeline.orders.pending_reservations(), ())
        order = pipeline.orders.get(result.order.client_order_id)
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.remaining_quantity, 0.0)

    def test_concurrent_intents_size_against_the_same_open_orders(self):
        pipeline, _, adapter, _, _ = make_pipeline(
            adapter=ScriptedAdapter(authenticated=True, clock=CLOCK))
        results: list = []

        def submit():
            results.append(pipeline.execute(
                make_intent(setup=trade_setup("AAA"), limit_price=100.0,
                            requested_quantity=10)))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        codes = [result.code for result in results]
        self.assertEqual(len(codes), 2)
        self.assertEqual(codes.count(PipelineCode.SUBMITTED), 1, codes)
        self.assertEqual(codes.count(PipelineCode.RISK_REJECTED), 1, codes)
        self.assertEqual(len(adapter.get_orders()), 1,
                         "only one intent may commit capacity for the symbol")
        self.assertEqual(len(pipeline.orders.list()), 1)


class PendingReservationTests(unittest.TestCase):
    def test_sizing_reserves_pending_notional_from_exposure_cash_and_symbol(self):
        snapshot = PortfolioSnapshot(cash=100_000.0, equity=100_000.0,
                                     available_capital=100_000.0, exposure=0.0,
                                     realized_pnl=0.0, unrealized_pnl=0.0,
                                     daily_pnl=0.0, trade_count=0, positions=())
        config = RiskConfig()
        reserved = replace(snapshot, pending_reservations=(
            PendingOrderReservation(order_id="CLIENT-1", symbol="ABC",
                                    direction=Direction.LONG,
                                    remaining_quantity=100.0,
                                    reference_price=100.0),))
        base = sizing_terms(100.0, 95.0, snapshot, config,
                            symbol="ABC", direction=Direction.LONG)
        tighter = sizing_terms(100.0, 95.0, reserved, config,
                               symbol="ABC", direction=Direction.LONG)
        self.assertEqual(base["portfolio_exposure"], 500)
        self.assertEqual(tighter["portfolio_exposure"], 400)
        self.assertEqual(base["cash"], 1000)
        self.assertEqual(tighter["cash"], 900)
        self.assertEqual(base["symbol_exposure"], 250)
        self.assertEqual(tighter["symbol_exposure"], 150)
        self.assertEqual(reserved.pending_notional, 10_000.0)

    def test_a_reservation_without_a_price_still_holds_its_symbol_slot(self):
        lifecycle = service()
        lifecycle.create(order_intent(limit_price=None, stop_price=None,
                                      quantity=10.0))
        reservations = lifecycle.pending_reservations()
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0].notional, 0.0,
                         "an order with no price must not invent notional")

    def test_terminal_orders_never_reserve_anything(self):
        lifecycle = service()
        order = submitted_order(lifecycle, status=OrderStatus.CANCELLED)
        self.assertEqual(lifecycle.pending_reservations(), ())
        self.assertFalse(order.is_open)


class CancellationTests(unittest.TestCase):
    def test_cancelling_an_open_order_closes_it_at_the_provider(self):
        executor = RecordingExecutor(status=OrderStatus.CANCELLED)
        lifecycle = service(executor=executor)
        order = submitted_order(lifecycle, status=OrderStatus.ACCEPTED)
        result = lifecycle.cancel(order.client_order_id)
        self.assertIs(result.outcome, CancellationOutcome.CANCELLED)
        self.assertTrue(result.success)
        self.assertFalse(result.visible_failure)
        self.assertEqual(executor.calls, ["PROV-1"])
        self.assertIs(lifecycle.get(order.client_order_id).status,
                      OrderStatus.CANCELLED)
        self.assertEqual(lifecycle.pending_reservations(), ())

    def test_cancelling_twice_is_reported_as_already_cancelled(self):
        executor = RecordingExecutor(status=OrderStatus.CANCELLED)
        lifecycle = service(executor=executor)
        order = submitted_order(lifecycle, status=OrderStatus.ACCEPTED)
        lifecycle.cancel(order.client_order_id)
        again = lifecycle.cancel(order.client_order_id)
        self.assertIs(again.outcome, CancellationOutcome.ALREADY_CANCELLED)
        self.assertTrue(again.success)
        self.assertEqual(executor.calls, ["PROV-1"],
                         "an already-cancelled order needs no second provider call")

    def test_a_filled_order_can_no_longer_be_cancelled(self):
        executor = RecordingExecutor()
        lifecycle = service(executor=executor)
        order = submitted_order(lifecycle, status=OrderStatus.FILLED)
        result = lifecycle.cancel(order.client_order_id)
        self.assertIs(result.outcome, CancellationOutcome.NOT_CANCELABLE)
        self.assertTrue(result.visible_failure)
        self.assertIs(lifecycle.get(order.client_order_id).status, OrderStatus.FILLED)
        self.assertEqual(executor.calls, [])

    def test_a_provider_failure_never_changes_local_state(self):
        executor = RecordingExecutor(raises=ConnectionError("no route to provider"))
        lifecycle = service(executor=executor)
        order = submitted_order(lifecycle, status=OrderStatus.ACCEPTED)
        result = lifecycle.cancel(order.client_order_id)
        self.assertIs(result.outcome, CancellationOutcome.PROVIDER_ERROR)
        self.assertTrue(result.visible_failure)
        still = lifecycle.get(order.client_order_id)
        self.assertIs(still.status, OrderStatus.ACCEPTED)
        self.assertTrue(still.is_open,
                        "an order we could not cancel is still open and reserved")
        self.assertEqual(len(lifecycle.pending_reservations()), 1)

    def test_a_provider_that_refuses_is_reported_not_hidden(self):
        executor = RecordingExecutor(raises=NotImplementedError("no cancellation"))
        lifecycle = service(executor=executor)
        order = submitted_order(lifecycle, status=OrderStatus.ACCEPTED)
        result = lifecycle.cancel(order.client_order_id)
        self.assertIs(result.outcome, CancellationOutcome.REJECTED_BY_PROVIDER)
        self.assertTrue(result.visible_failure)
        self.assertIs(lifecycle.get(order.client_order_id).status,
                      OrderStatus.ACCEPTED)

        accepting = RecordingExecutor(status=OrderStatus.ACCEPTED)
        lifecycle = service(executor=accepting)
        order = submitted_order(lifecycle, status=OrderStatus.ACCEPTED)
        unchanged = lifecycle.cancel(order.client_order_id)
        self.assertIs(unchanged.outcome, CancellationOutcome.REJECTED_BY_PROVIDER)
        self.assertIs(lifecycle.get(order.client_order_id).status,
                      OrderStatus.ACCEPTED)

    def test_a_not_yet_submitted_order_closes_locally_without_a_call(self):
        executor = RecordingExecutor()
        lifecycle = service(executor=executor)
        created = lifecycle.create(order_intent())
        result = lifecycle.cancel(created.client_order_id)
        self.assertIs(result.outcome, CancellationOutcome.CANCELLED)
        self.assertTrue(result.success)
        self.assertEqual(executor.calls, [])
        self.assertIs(lifecycle.get(created.client_order_id).status,
                      OrderStatus.CANCELLED)

    def test_an_unknown_order_is_reported_as_not_found(self):
        lifecycle = service(executor=RecordingExecutor())
        result = lifecycle.cancel("does-not-exist")
        self.assertIs(result.outcome, CancellationOutcome.ORDER_NOT_FOUND)
        self.assertTrue(result.visible_failure)
        self.assertFalse(result.success)

    def test_cancellation_outcomes_are_journaled(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            lifecycle = service(journal, executor=RecordingExecutor())
            order = submitted_order(lifecycle, status=OrderStatus.ACCEPTED)
            lifecycle.cancel(order.client_order_id)
            lifecycle.cancel(order.client_order_id)
            lifecycle.cancel("missing")
            events = [event["event_type"] for event in journal.events()]
            self.assertIn("ORDER_CANCELLED", events)
            self.assertIn("ORDER_CANCELLATION_FAILED", events)
        finally:
            journal.close()


class ReconciliationTests(unittest.TestCase):
    def build(self, *, status=OrderStatus.ACCEPTED, filled=0.0,
              provider_order_id="PROV-1", client="CLIENT-1"):
        lifecycle = service()
        lifecycle.create(order_intent(client))
        lifecycle.mark_submitted(client, provider_order_id)
        if status is not OrderStatus.SUBMITTED:
            lifecycle.apply_status(client, status)
        if filled:
            lifecycle.record_fill(client, fill(filled, 100.0, order_id=client,
                                               provider_fill_id=f"PF-{client}"))
            if lifecycle.get(client).status is not status:
                lifecycle.apply_status(client, status)
        return lifecycle

    def remote(self, *, provider_order_id="PROV-1", client="CLIENT-1",
               status=OrderStatus.ACCEPTED, filled=0.0, symbol="ABC",
               quantity=100.0) -> RemoteOrder:
        return RemoteOrder(provider_order_id=provider_order_id, symbol=symbol,
                           quantity=quantity, filled_quantity=filled,
                           status=status, client_order_id=client)

    def snapshot(self, *, orders=(), fills=(), positions=(), complete=True,
                 error=None) -> ProviderStateSnapshot:
        return ProviderStateSnapshot(orders=tuple(orders), fills=tuple(fills),
                                     positions=tuple(positions), complete=complete,
                                     error=error, retrieved_at=NOW,
                                     connection_id="paper", provider_id="mock")

    def test_an_agreeing_state_is_a_clean_match(self):
        lifecycle = self.build()
        report = lifecycle.reconciler.reconcile(
            self.snapshot(orders=(self.remote(),)))
        self.assertIs(report.status, ReconcileStatus.MATCH)
        self.assertTrue(report.clean)
        self.assertFalse(report.unresolved)
        self.assertEqual(report.discrepancies, ())
        self.assertEqual(report.matched_order_count, 1)
        self.assertEqual(report.local_order_count, 1)

    def test_status_and_quantity_drift_are_reported(self):
        lifecycle = self.build(status=OrderStatus.PARTIALLY_FILLED, filled=40.0)
        report = lifecycle.reconciler.reconcile(
            self.snapshot(orders=(self.remote(status=OrderStatus.ACCEPTED,
                                              filled=50.0),)))
        self.assertIs(report.status, ReconcileStatus.DISCREPANCIES)
        self.assertFalse(report.clean)
        self.assertTrue(report.has(DiscrepancyKind.STATUS_MISMATCH))
        self.assertTrue(report.has(DiscrepancyKind.QUANTITY_MISMATCH))
        self.assertFalse(report.has(DiscrepancyKind.PARTIAL_FILL_MISMATCH))

    def test_a_provider_error_is_unresolved_and_never_a_match(self):
        lifecycle = self.build()
        report = lifecycle.reconciler.reconcile(
            self.snapshot(orders=(self.remote(),), complete=False,
                          error="the provider is unreachable"))
        self.assertIs(report.status, ReconcileStatus.PROVIDER_ERROR)
        self.assertFalse(report.clean)
        self.assertTrue(report.unresolved)
        self.assertEqual(report.discrepancies, (),
                         "absence of findings here means unknown, not fine")

    def test_an_incomplete_snapshot_is_unresolved_and_never_a_match(self):
        lifecycle = self.build()
        report = lifecycle.reconciler.reconcile(
            self.snapshot(orders=(self.remote(),), complete=False))
        self.assertIs(report.status, ReconcileStatus.INCOMPLETE)
        self.assertFalse(report.clean)
        self.assertTrue(report.unresolved)
        self.assertEqual(report.discrepancies, ())

    def test_an_open_order_missing_from_the_provider_is_reported(self):
        lifecycle = self.build()
        report = lifecycle.reconciler.reconcile(self.snapshot())
        self.assertTrue(report.has(DiscrepancyKind.LOCAL_ORDER_MISSING_REMOTELY))
        self.assertIs(report.status, ReconcileStatus.DISCREPANCIES)

    def test_a_never_submitted_order_is_not_expected_remotely(self):
        lifecycle = service()
        lifecycle.create(order_intent())
        report = lifecycle.reconciler.reconcile(self.snapshot())
        self.assertFalse(report.has(DiscrepancyKind.LOCAL_ORDER_MISSING_REMOTELY))
        self.assertIs(report.status, ReconcileStatus.MATCH)

    def test_a_terminal_order_absent_from_the_provider_is_not_a_finding(self):
        lifecycle = self.build(status=OrderStatus.CANCELLED)
        report = lifecycle.reconciler.reconcile(self.snapshot())
        self.assertFalse(report.has(DiscrepancyKind.LOCAL_ORDER_MISSING_REMOTELY))
        self.assertIs(report.status, ReconcileStatus.MATCH)

    def test_a_provider_order_we_do_not_know_is_reported(self):
        lifecycle = self.build()
        report = lifecycle.reconciler.reconcile(self.snapshot(
            orders=(self.remote(provider_order_id="PROV-UNKNOWN",
                                client="GHOST"),)))
        self.assertTrue(report.has(DiscrepancyKind.REMOTE_ORDER_UNKNOWN_LOCALLY))

    def test_duplicate_provider_identifiers_are_reported(self):
        lifecycle = self.build()
        report = lifecycle.reconciler.reconcile(self.snapshot(
            orders=(self.remote(), self.remote())))
        self.assertTrue(report.has(DiscrepancyKind.DUPLICATE_PROVIDER_ORDER))

    def test_a_provider_execution_we_never_recorded_is_reported(self):
        lifecycle = self.build()
        report = lifecycle.reconciler.reconcile(self.snapshot(
            orders=(self.remote(),),
            fills=(RemoteFill(quantity=25.0, price=101.0, timestamp=NOW,
                              provider_fill_id="PF-999",
                              provider_order_id="PROV-1"),)))
        self.assertTrue(report.has(DiscrepancyKind.FILL_MISSING_LOCALLY))

    def test_a_recorded_provider_fill_is_not_reported_again(self):
        lifecycle = self.build(status=OrderStatus.PARTIALLY_FILLED, filled=25.0)
        report = lifecycle.reconciler.reconcile(self.snapshot(
            orders=(self.remote(status=OrderStatus.PARTIALLY_FILLED, filled=25.0),),
            fills=(RemoteFill(quantity=25.0, price=100.0, timestamp=NOW,
                              provider_fill_id="PF-CLIENT-1",
                              provider_order_id="PROV-1"),)))
        self.assertFalse(report.has(DiscrepancyKind.FILL_MISSING_LOCALLY))
        self.assertIs(report.status, ReconcileStatus.MATCH)

    def test_a_terminal_local_order_the_provider_still_shows_open(self):
        lifecycle = self.build(status=OrderStatus.FILLED, filled=100.0)
        report = lifecycle.reconciler.reconcile(self.snapshot(
            orders=(self.remote(status=OrderStatus.ACCEPTED),)))
        self.assertTrue(report.has(DiscrepancyKind.TERMINAL_LOCAL_OPEN_REMOTELY))

    def test_position_drift_is_only_compared_when_a_book_exists(self):
        lifecycle = self.build()
        empty = lifecycle.reconciler.reconcile(
            self.snapshot(orders=(self.remote(),)))
        self.assertFalse(empty.has(DiscrepancyKind.POSITION_MISSING_REMOTELY))

        from backend.portfolio.models import Position
        local = Position(symbol="ABC", direction=Direction.LONG, quantity=10,
                         average_entry_price=100.0, market_price=None,
                         opened_at=NOW)
        report = lifecycle.reconciler.reconcile(
            self.snapshot(orders=(self.remote(),),
                          positions=(RemotePosition(symbol="ABC", quantity=7.0),)),
            positions=(local,))
        self.assertTrue(report.has(DiscrepancyKind.POSITION_QUANTITY_MISMATCH))

        unknown = lifecycle.reconciler.reconcile(
            self.snapshot(orders=(self.remote(),),
                          positions=(RemotePosition(symbol="XYZ", quantity=5.0),)))
        self.assertTrue(unknown.has(DiscrepancyKind.POSITION_UNKNOWN_LOCALLY))

    def test_reconciliation_never_mutates_local_state(self):
        lifecycle = self.build(status=OrderStatus.PARTIALLY_FILLED, filled=40.0)
        before = lifecycle.get("CLIENT-1")
        lifecycle.reconciler.reconcile(self.snapshot(
            orders=(self.remote(status=OrderStatus.FILLED, filled=100.0),)))
        after = lifecycle.get("CLIENT-1")
        self.assertEqual(before, after)
        self.assertIs(after.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(after.filled_quantity, 40.0)

    def test_every_reconciliation_finding_is_journaled(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            lifecycle = OrderLifecycleService(journal=journal, clock=CLOCK,
                                              restore=False)
            lifecycle.create(order_intent())
            lifecycle.mark_submitted("CLIENT-1", "PROV-1")
            lifecycle.apply_status("CLIENT-1", OrderStatus.ACCEPTED)
            lifecycle.reconcile(self.snapshot())
            findings = [event for event in journal.events()
                        if event["event_type"] == "RECONCILIATION_DISCREPANCY"]
            self.assertTrue(findings)
            self.assertEqual(findings[0]["context_json"]["kind"],
                             DiscrepancyKind.LOCAL_ORDER_MISSING_REMOTELY.value)
            self.assertIn("RECONCILIATION_DISCREPANCY", ORDER_EVENTS)
            self.assertIn("RECONCILIATION_CLEAN", ORDER_EVENTS)
        finally:
            journal.close()

    def test_report_is_json_ready(self):
        lifecycle = self.build()
        document = lifecycle.reconciler.reconcile(
            self.snapshot(orders=(self.remote(),))).to_dict()
        self.assertEqual(json.loads(json.dumps(document))["status"], "MATCH")


class PersistenceAndRestartTests(unittest.TestCase):
    def test_orders_fills_and_lifecycle_events_are_journaled(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            lifecycle = service(journal)
            order = submitted_order(lifecycle, status=OrderStatus.SUBMITTED)
            lifecycle.record_fill(order.client_order_id,
                                  fill(40, 100.0, provider_fill_id="PF-1"))
            rows = journal.rows("orders")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "PARTIALLY_FILLED")
            fills = journal.rows("fills")
            self.assertEqual(len(fills), 1)
            self.assertEqual(fills[0]["quantity"], 40.0)
            events = [event["event_type"] for event in journal.events()]
            for name in ("ORDER_CREATED", "ORDER_SUBMITTED",
                         "ORDER_PARTIAL_FILL"):
                self.assertIn(name, events)
            self.assertTrue(set(ORDER_EVENTS) >= {
                "ORDER_CREATED", "ORDER_SUBMITTED", "ORDER_STATUS_CHANGED",
                "ORDER_FILL_RECEIVED", "ORDER_REJECTED", "ORDER_CANCELLED",
                "ORDER_SUBMISSION_FAILED", "ORDER_CANCELLATION_FAILED"})
        finally:
            journal.close()

    def test_open_orders_are_restored_after_a_restart(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            first = service(journal)
            order = submitted_order(first, client="RESTART-1",
                                    status=OrderStatus.PARTIALLY_FILLED)
            first.record_fill(order.client_order_id,
                              fill(40, 100.0, provider_fill_id="PF-1"))

            restarted = OrderLifecycleService(journal=journal, clock=CLOCK)
            self.assertEqual(restarted.restored_count, 1)
            restored = restarted.get("RESTART-1")
            self.assertIsNotNone(restored)
            self.assertIs(restored.status, OrderStatus.PARTIALLY_FILLED)
            self.assertEqual(restored.provider_order_id, "PROV-1")
            self.assertEqual(restored.filled_quantity, 40.0)
            self.assertEqual(restored.intent.symbol, "ABC")
            self.assertEqual(len(restarted.pending_reservations()), 1)
        finally:
            journal.close()

    def test_a_restart_sees_the_capacity_a_fresh_process_already_committed(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            first = service(journal)
            submitted_order(first, client="RESTART-2",
                            status=OrderStatus.SUBMITTED)
            restarted = OrderLifecycleService(journal=journal, clock=CLOCK)
            self.assertEqual(len(restarted.pending_reservations()), 1)
            self.assertEqual(restarted.pending_reservations()[0].remaining_quantity,
                             100.0)
        finally:
            journal.close()

    def test_the_store_survives_concurrent_writes(self):
        store = OrderStore()
        errors: list = []

        def writer(index: int):
            try:
                for step in range(50):
                    intent = order_intent(f"ID-{index}-{step}")
                    store.create(intent, NOW)
                    store.apply(store.require(intent.client_order_id),
                                OrderStatus.SUBMITTED, LATER)
            except Exception as exc:  # noqa: BLE001 - surfaced as a test failure
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(index,))
                   for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(store), 200)
        self.assertTrue(all(order.status is OrderStatus.SUBMITTED
                            for order in store.list()))


class HttpOrderEndpointTests(unittest.TestCase):
    def setUp(self):
        config = load_config(env={"OWNER_CONTROL_TOKEN": OWNER_TOKEN})
        self.runtime = ApplicationRuntime(database_path=":memory:",
                                          owner_token=OWNER_TOKEN, clock=CLOCK,
                                          config=config)
        self.server = create_server(self.runtime, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.runtime.close()

    def get_json(self, path):
        with urlrequest.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, path, body, token=OWNER_TOKEN):
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

    def test_orders_endpoint_lists_the_normalized_orders(self):
        order = self.runtime.orders.create(order_intent(symbol="AAPL"))
        document = self.get_json("/api/orders")
        self.assertEqual(document["count"], 1)
        self.assertEqual(document["orders"][0]["order_id"], order.client_order_id)
        self.assertEqual(document["orders"][0]["status"], "NEW")
        self.assertEqual(document["pending_reservation_count"], 1)
        filtered = self.get_json("/api/orders?symbol=MSFT")
        self.assertEqual(filtered["count"], 0)
        open_only = self.get_json("/api/orders?open=true")
        self.assertEqual(open_only["count"], 1)
        self.assertNotIn("api_key",
                         json.dumps(document, default=_json_default).lower())

    def test_cancel_endpoint_needs_the_owner_token(self):
        order = self.runtime.orders.create(order_intent())
        unauthorized, payload = self.post_json(
            "/api/orders/cancel", {"order_id": order.client_order_id}, token=None)
        self.assertEqual(unauthorized, 401)
        self.assertIn("owner", payload["error"].lower())
        self.assertIs(self.runtime.orders.get(order.client_order_id).status,
                      OrderStatus.NEW)

    def test_cancel_endpoint_reports_each_outcome_honestly(self):
        order = self.runtime.orders.create(order_intent())
        status, payload = self.post_json("/api/orders/cancel",
                                         {"order_id": order.client_order_id})
        self.assertEqual(status, 200)
        self.assertEqual(payload["outcome"], "CANCELLED")
        self.assertTrue(payload["success"])
        again, payload = self.post_json("/api/orders/cancel",
                                        {"order_id": order.client_order_id})
        self.assertEqual(again, 200)
        self.assertEqual(payload["outcome"], "ALREADY_CANCELLED")
        missing, payload = self.post_json("/api/orders/cancel",
                                          {"order_id": "nope"})
        self.assertEqual(missing, 404)
        self.assertEqual(payload["outcome"], "ORDER_NOT_FOUND")
        blank, payload = self.post_json("/api/orders/cancel", {})
        self.assertEqual(blank, 400)

    def test_reconcile_endpoint_is_read_only_and_owner_guarded(self):
        order = self.runtime.orders.create(order_intent())
        denied, _ = self.post_json("/api/orders/reconcile", {}, token=None)
        self.assertEqual(denied, 401)
        status, payload = self.post_json("/api/orders/reconcile", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "MATCH")
        self.assertTrue(payload["clean"])
        self.assertEqual(payload["discrepancy_count"], 0)
        self.assertIs(self.runtime.orders.get(order.client_order_id).status,
                      OrderStatus.NEW, "reconciliation must never rewrite state")


class VocabularyAndArchitectureTests(unittest.TestCase):
    def test_every_lifecycle_event_name_is_a_known_journal_event(self):
        self.assertEqual(len(set(ORDER_EVENTS)), len(ORDER_EVENTS))
        self.assertIn("ORDER_CREATED", ORDER_EVENTS)
        self.assertIn("ORDER_SUBMISSION_FAILED", ORDER_EVENTS)

    def test_orders_package_never_calls_place_order_itself(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1] / "orders"
        offenders = []
        for source in sorted(root.glob("*.py")):
            text = source.read_text(encoding="utf-8")
            if "place_order(" in text:
                offenders.append(source.name)
        self.assertEqual(offenders, [],
                         "orders may only cancel, never reach an adapter directly")

    def test_order_types_are_exposed_from_the_orders_package(self):
        self.assertIs(OrderStatus, CanonicalOrderStatus)
        self.assertTrue(callable(order_from_dict))
        self.assertTrue(callable(fill_totals))
        self.assertIn(OrderStatus.SUBMITTED, VALID_TRANSITIONS)
        self.assertTrue(issubclass(InvalidOrderTransition, Exception))


if __name__ == "__main__":
    unittest.main()
