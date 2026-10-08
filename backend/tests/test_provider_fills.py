"""Phase 10 checkpoint 2 proof: durable fill identity and the fill journal.

These tests hold the guarantees CP2 exists for:

- every provider fill receives exactly one durable identity before it is
  merged: the provider's own number when the provider supplied one, a
  deterministic digest of the execution's identifying fields when it did
  not - never a random identifier as a fallback;
- that identity is scope-qualified by the connection and provider instance
  that saw it, survives serialization and a process restart, and is never
  rewritten once assigned;
- the ``order_fill_journal`` table stores one row per fill per provider
  scope, idempotently, and its insertion result distinguishes a new row
  from a replay;
- a fill that cannot be persisted is never recorded as if it had been:
  persistence failures surface as :class:`FillPersistenceError` instead of
  disappearing.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from backend.database import SQLiteJournalRepository
from backend.database.serialization import to_json
from backend.execution import (
    Fill,
    LocalPaperAdapter,
    OrderRequest,
    OrderType,
    paper_fill_id,
)
from backend.execution.fill_identity import (
    deterministic_fill_key,
    fill_identity,
    normalize_fill,
)
from backend.orders import (
    FillPersistenceError,
    OrderIntent,
    OrderLifecycleService,
    OrderStatus,
    order_from_dict,
)
from backend.orders.provider import snapshot_from_adapter
from backend.pipeline import PipelineCode
from backend.strategies.models import Direction

from backend.tests.test_order_lifecycle import fill, order_intent
from backend.tests.test_pipeline import CLOCK, NOW, make_intent, make_pipeline, trade_setup


def lifecycle(journal=None) -> OrderLifecycleService:
    return OrderLifecycleService(journal=journal, clock=CLOCK, restore=False)


def opened(service_: OrderLifecycleService, *, client: str = "CLIENT-1",
           provider_order_id: str = "PROV-1",
           connection_id: str = "paper",
           provider_instance_id: str = "inst-1",
           status=OrderStatus.SUBMITTED):
    """A submitted order with a known provider scope."""
    service_.create(order_intent(client), connection_id=connection_id,
                    provider_instance_id=provider_instance_id)
    service_.mark_submitted(client, provider_order_id,
                            provider_instance_id=provider_instance_id)
    if status is not OrderStatus.SUBMITTED:
        service_.apply_status(client, status)
    return service_.get(client)


class PaperVenueIdentityTests(unittest.TestCase):
    def test_the_paper_venue_identifies_its_fill_and_the_journal_holds_one_row(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            adapter = LocalPaperAdapter(clock=CLOCK)
            pipeline, _, _, _, _ = make_pipeline(adapter=adapter, journal=journal)
            result = pipeline.execute(make_intent(setup=trade_setup("ABC"),
                                                  limit_price=100.0,
                                                  requested_quantity=10))
            self.assertEqual(result.code, PipelineCode.ACCEPTED,
                             (result.code, result.reason))
            order = result.order
            self.assertIs(order.status, OrderStatus.FILLED)
            self.assertEqual(len(order.fills), 1)

            rows = journal.rows("fills")
            self.assertEqual(len(rows), 1, "one paper fill is one durable row")
            row = rows[0]
            expected = paper_fill_id(order.provider_order_id, 1)
            self.assertTrue(expected.endswith("-F1"))
            self.assertEqual(order.fills[0].provider_fill_id, expected)
            self.assertEqual(row["provider_fill_id"], expected)
            self.assertEqual(row["fill_id"], order.fills[0].fill_id)
            self.assertIn(expected, row["fill_id"],
                          "the provider's own number forms the identity")
            self.assertEqual(row["client_order_id"], order.client_order_id)
            self.assertEqual(row["order_id"], order.client_order_id)
            self.assertEqual(row["connection_id"], "paper")
            self.assertEqual(row["provider_instance_id"],
                             adapter.provider_instance_id)
            self.assertEqual(row["symbol"], "ABC")
            self.assertEqual(row["quantity"], 10.0)
            self.assertEqual(row["price"], 100.0)
        finally:
            journal.close()

    def test_a_paper_fill_number_is_reproducible_across_every_read(self):
        adapter = LocalPaperAdapter(clock=CLOCK)
        placed = adapter.place_order(OrderRequest("ABC", Direction.LONG, 1, 100.0))
        expected = paper_fill_id(placed.order_id, 1)
        self.assertEqual(placed.fills[0].provider_fill_id, expected)
        self.assertEqual(adapter.get_order(placed.order_id).fills[0].provider_fill_id,
                         expected)
        self.assertEqual(adapter.reconcile_orders()[0].fills[0].provider_fill_id,
                         expected)
        other = adapter.place_order(OrderRequest("XYZ", Direction.LONG, 1, 100.0))
        self.assertNotEqual(other.fills[0].provider_fill_id, expected,
                            "two orders never share a fill number")

    def test_the_reconciliation_snapshot_reports_the_providers_own_number(self):
        adapter = LocalPaperAdapter(clock=CLOCK)
        placed = adapter.place_order(OrderRequest("ABC", Direction.LONG, 1, 100.0))
        snapshot = snapshot_from_adapter(adapter, now=NOW, connection_id="paper")
        self.assertEqual(len(snapshot.fills), 1)
        self.assertEqual(snapshot.fills[0].provider_fill_id,
                         paper_fill_id(placed.order_id, 1),
                         "the provider's number is what reconciliation matches on")


class ReplayIdempotencyTests(unittest.TestCase):
    def test_the_same_fill_replayed_ten_times_is_one_row(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            service_ = lifecycle(journal)
            order = opened(service_)
            reported = fill(40, 100.0, order_id=order.client_order_id,
                            provider_fill_id="PF-1",
                            provider_order_id=order.provider_order_id)
            for _ in range(10):
                updated = service_.record_fill(order.client_order_id, reported)
            self.assertEqual(len(updated.fills), 1)
            self.assertEqual(updated.filled_quantity, 40.0)
            rows = journal.rows("fills")
            self.assertEqual(len(rows), 1, "ten replays must not be ten rows")
            self.assertEqual(rows[0]["provider_fill_id"], "PF-1")
            self.assertEqual(rows[0]["fill_id"], updated.fills[0].fill_id)
            self.assertEqual(len(journal.rows("orders")), 1)
        finally:
            journal.close()

    def test_the_journal_says_whether_a_fill_was_newly_inserted(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            identified = normalize_fill(
                Fill(10, 100.0, NOW), connection_id="paper",
                provider_instance_id="inst-1", client_order_id="CLIENT-1",
                provider_order_id="PROV-1")
            arguments = dict(order_id="CLIENT-1", client_order_id="CLIENT-1",
                             symbol="ABC", connection_id="paper",
                             provider_instance_id="inst-1")
            self.assertTrue(journal.record_provider_fill(identified, **arguments),
                            "the first write inserts the row")
            for _ in range(9):
                self.assertFalse(
                    journal.record_provider_fill(identified, **arguments),
                    "a replay reports that it did not insert anything new")
            self.assertEqual(len(journal.rows("fills")), 1)
        finally:
            journal.close()


class DistinctProviderNumberTests(unittest.TestCase):
    def test_identical_fields_with_different_provider_numbers_stay_two_fills(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            service_ = lifecycle(journal)
            order = opened(service_)
            service_.record_fill(order.client_order_id,
                                 fill(40, 100.0, order_id=order.client_order_id,
                                      provider_fill_id="PF-1"))
            service_.record_fill(order.client_order_id,
                                 fill(40, 100.0, order_id=order.client_order_id,
                                      provider_fill_id="PF-2"))
            stored = service_.get(order.client_order_id)
            self.assertEqual(len(stored.fills), 2,
                             "the provider distinguishes them, so the platform must too")
            self.assertEqual(stored.filled_quantity, 80.0)
            self.assertNotEqual(stored.fills[0].fill_id, stored.fills[1].fill_id)
            rows = journal.rows("fills")
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["provider_fill_id"] for row in rows},
                             {"PF-1", "PF-2"})
        finally:
            journal.close()

    def test_a_supplied_provider_number_becomes_the_identity_verbatim(self):
        identified = normalize_fill(
            Fill(40, 100.0, NOW, provider_fill_id="PF-9"),
            connection_id="paper", provider_instance_id="inst-1",
            client_order_id="CLIENT-1", provider_order_id="PROV-1")
        self.assertEqual(identified.fill_id, "paper:inst-1:PF-9")
        rescoped = normalize_fill(identified, connection_id="elsewhere",
                                  provider_instance_id="another-instance")
        self.assertEqual(rescoped.fill_id, "paper:inst-1:PF-9",
                         "an identity, once assigned, is never rewritten")

    def test_the_scoped_identity_travels_into_the_journal(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            service_ = lifecycle(journal)
            order = opened(service_)
            updated = service_.record_fill(
                order.client_order_id,
                fill(40, 100.0, order_id=order.client_order_id,
                     provider_fill_id="PF-7"))
            self.assertEqual(updated.fills[0].fill_id, "paper:inst-1:PF-7")
            rows = journal.rows("fills")
            self.assertEqual(rows[0]["fill_id"], "paper:inst-1:PF-7")
            self.assertEqual(rows[0]["provider_order_id"], "PROV-1")
        finally:
            journal.close()


class RestartIdentityTests(unittest.TestCase):
    def _restart(self, path: str):
        return SQLiteJournalRepository(path)

    def test_a_fallback_identity_is_identical_after_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "fills.sqlite3")
            first_journal = self._restart(path)
            try:
                first = lifecycle(first_journal)
                order = opened(first, client="RESTART-1")
                identified = first.record_fill(
                    order.client_order_id,
                    fill(40, 100.0, order_id=order.client_order_id))
                identity = identified.fills[0].fill_id
                self.assertTrue(identity.startswith("paper:inst-1:FILL-"),
                                identity)
                rows = first_journal.rows("fills")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["fill_id"], identity)
            finally:
                first_journal.close()

            second_journal = self._restart(path)
            try:
                second = OrderLifecycleService(journal=second_journal, clock=CLOCK)
                restored = second.get("RESTART-1")
                self.assertIsNotNone(restored)
                self.assertEqual(restored.fills[0].fill_id, identity,
                                 "identity survives serialization and restore")
                replayed = second.record_fill(
                    "RESTART-1", fill(40, 100.0, order_id="RESTART-1"))
                self.assertEqual(replayed.fills[0].fill_id, identity,
                                 "a new process derives the same identity")
                self.assertEqual(len(replayed.fills), 1)
                self.assertEqual(len(second_journal.rows("fills")), 1)
            finally:
                second_journal.close()

    def test_a_provider_number_identity_is_identical_after_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "fills.sqlite3")
            first_journal = self._restart(path)
            try:
                first = lifecycle(first_journal)
                order = opened(first, client="RESTART-2")
                identified = first.record_fill(
                    order.client_order_id,
                    fill(40, 100.0, order_id=order.client_order_id,
                         provider_fill_id="PF-RESTART"))
                identity = identified.fills[0].fill_id
                self.assertEqual(identity, "paper:inst-1:PF-RESTART")
            finally:
                first_journal.close()

            second_journal = self._restart(path)
            try:
                second = OrderLifecycleService(journal=second_journal, clock=CLOCK)
                replayed = second.record_fill(
                    "RESTART-2",
                    fill(40, 100.0, order_id="RESTART-2",
                         provider_fill_id="PF-RESTART"))
                self.assertEqual(replayed.fills[0].fill_id, identity)
                self.assertEqual(len(replayed.fills), 1)
                self.assertEqual(len(second_journal.rows("fills")), 1)
            finally:
                second_journal.close()


class SerializationStabilityTests(unittest.TestCase):
    def test_the_identity_survives_a_serialization_round_trip(self):
        service_ = lifecycle()
        order = opened(service_)
        updated = service_.record_fill(
            order.client_order_id,
            fill(40, 100.0, order_id=order.client_order_id,
                 provider_fill_id="PF-1"))
        identity = updated.fills[0].fill_id
        rebuilt = order_from_dict(json.loads(to_json(updated)))
        self.assertEqual(rebuilt.fills[0].fill_id, identity)

    def test_the_fallback_key_is_stable_and_discriminating(self):
        base = dict(client_order_id="C1", provider_order_id="P1",
                    quantity=100.0, price=100.0, timestamp=NOW)
        key = deterministic_fill_key(**base)
        self.assertEqual(key, deterministic_fill_key(
            **{**base, "quantity": 100, "price": 100.00}))
        utc_now = datetime(2024, 1, 8, 15, tzinfo=timezone.utc)
        other_zone = utc_now.astimezone(timezone(timedelta(hours=5)))
        self.assertEqual(
            deterministic_fill_key(**{**base, "timestamp": utc_now}),
            deterministic_fill_key(**{**base, "timestamp": other_zone}),
            "the same instant in any time zone is the same key")
        self.assertNotEqual(key, deterministic_fill_key(**{**base, "quantity": 60.0}))
        self.assertNotEqual(key, deterministic_fill_key(**{**base, "price": 101.0}))
        self.assertNotEqual(key, deterministic_fill_key(
            **{**base, "timestamp": NOW + timedelta(seconds=1)}))
        self.assertNotEqual(key, deterministic_fill_key(
            **{**base, "client_order_id": "C2"}))
        self.assertNotEqual(key, deterministic_fill_key(
            **{**base, "provider_order_id": "P2"}))
        # Field boundaries cannot be forged by moving characters across them.
        self.assertNotEqual(
            deterministic_fill_key(client_order_id="ab",
                                   provider_order_id="c",
                                   quantity=1.0, price=1.0, timestamp=NOW),
            deterministic_fill_key(client_order_id="a",
                                   provider_order_id="bc",
                                   quantity=1.0, price=1.0, timestamp=NOW))

    def test_identity_shape_holds_when_the_scope_is_unknown(self):
        derived = deterministic_fill_key(quantity=1.0, price=1.0, timestamp=NOW)
        self.assertEqual(fill_identity(quantity=1.0, price=1.0, timestamp=NOW),
                         f"-:-:{derived}")
        self.assertEqual(fill_identity(quantity=1.0, price=1.0, timestamp=NOW,
                                       provider_fill_id=" PF-1 "),
                         "-:-:PF-1", "provider numbers are adopted, only stripped")


class ProviderScopeTests(unittest.TestCase):
    def test_the_same_provider_number_on_two_scopes_is_two_fills(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            service_ = lifecycle(journal)
            opened(service_, client="SCOPE-A", provider_order_id="PROV-A",
                   provider_instance_id="inst-1")
            opened(service_, client="SCOPE-B", provider_order_id="PROV-B",
                   provider_instance_id="inst-2")
            service_.record_fill("SCOPE-A",
                                 fill(40, 100.0, order_id="SCOPE-A",
                                      provider_fill_id="PF-SAME"))
            service_.record_fill("SCOPE-B",
                                 fill(40, 100.0, order_id="SCOPE-B",
                                      provider_fill_id="PF-SAME"))
            rows = journal.rows("fills")
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["fill_id"] for row in rows},
                             {"paper:inst-1:PF-SAME", "paper:inst-2:PF-SAME"})
            # ...while the same scope replaying it stays one fill in one row.
            replayed = service_.record_fill(
                "SCOPE-A", fill(40, 100.0, order_id="SCOPE-A",
                                provider_fill_id="PF-SAME"))
            self.assertEqual(len(replayed.fills), 1)
            self.assertEqual(len(journal.rows("fills")), 2)
        finally:
            journal.close()

    def test_identity_is_scoped_not_globally_unique_at_the_storage_layer(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            identified = normalize_fill(
                Fill(10, 100.0, NOW, provider_fill_id="PF-X"),
                connection_id="paper", provider_instance_id="inst-1",
                client_order_id="C1")
            self.assertTrue(journal.record_provider_fill(
                identified, order_id="C1", connection_id="paper",
                provider_instance_id="inst-1"))
            self.assertFalse(journal.record_provider_fill(
                identified, order_id="C1", connection_id="paper",
                provider_instance_id="inst-1"), "same scope is a replay")
            self.assertTrue(journal.record_provider_fill(
                identified, order_id="C1", connection_id="paper",
                provider_instance_id="inst-2"), "another instance is another fill")
            self.assertTrue(journal.record_provider_fill(
                identified, order_id="C1", connection_id="other",
                provider_instance_id="inst-1"), "another connection is another fill")
            self.assertEqual(len(journal.rows("fills")), 3)
        finally:
            journal.close()


class MultipleFillTests(unittest.TestCase):
    def test_two_distinct_fills_for_one_order_both_persist(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            service_ = lifecycle(journal)
            order = opened(service_)
            service_.record_fill(order.client_order_id,
                                 fill(40, 100.0, order_id=order.client_order_id,
                                      provider_fill_id="PF-1"))
            service_.record_fill(order.client_order_id,
                                 fill(60, 102.0, order_id=order.client_order_id,
                                      provider_fill_id="PF-2"))
            stored = service_.get(order.client_order_id)
            self.assertEqual(len(stored.fills), 2)
            self.assertEqual(stored.filled_quantity, 100.0)
            self.assertIs(stored.status, OrderStatus.FILLED)
            rows = journal.rows("fills")
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["quantity"] for row in rows}, {40.0, 60.0})
            self.assertEqual({row["provider_fill_id"] for row in rows},
                             {"PF-1", "PF-2"})
            self.assertEqual(len({row["fill_id"] for row in rows}), 2)
            events = [event["event_type"] for event in journal.events()]
            self.assertIn("ORDER_PARTIAL_FILL", events)
            self.assertIn("ORDER_FILL_RECEIVED", events)
        finally:
            journal.close()


class FailClosedTests(unittest.TestCase):
    class UnwritableJournal:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise RuntimeError("database unavailable")
            return fail

    class FilllessJournal:
        def record_order(self, order):
            pass

        def record_event(self, *args, **kwargs):
            pass

    def test_a_fill_that_cannot_be_persisted_never_enters_the_order(self):
        service_ = lifecycle(self.UnwritableJournal())
        order = opened(service_)
        with self.assertRaises(FillPersistenceError) as caught:
            service_.record_fill(order.client_order_id,
                                 fill(40, 100.0, order_id=order.client_order_id,
                                      provider_fill_id="PF-1"))
        error = caught.exception
        self.assertEqual(error.fill_id, "paper:inst-1:PF-1")
        self.assertEqual(error.order_id, order.client_order_id)
        self.assertIn("database unavailable", str(error))
        stored = service_.get(order.client_order_id)
        self.assertEqual(stored.fills, (),
                         "a failed write must never look like a recorded fill")
        self.assertEqual(stored.filled_quantity, 0.0)
        self.assertIs(stored.status, OrderStatus.SUBMITTED)

    def test_apply_provider_order_fails_closed_before_the_merge(self):
        service_ = lifecycle(self.UnwritableJournal())
        order = opened(service_)
        provider_order = SimpleNamespace(
            order_id=order.provider_order_id, status=OrderStatus.FILLED,
            fills=(fill(40, 100.0, order_id=order.client_order_id),))
        with self.assertRaises(FillPersistenceError):
            service_.apply_provider_order(order.client_order_id, provider_order)
        stored = service_.get(order.client_order_id)
        self.assertEqual(stored.fills, ())
        self.assertEqual(stored.filled_quantity, 0.0)
        self.assertIs(stored.status, OrderStatus.SUBMITTED,
                      "the status must not advance past fills that never landed")

    def test_a_journal_that_cannot_store_fills_says_so(self):
        service_ = lifecycle(self.FilllessJournal())
        order = opened(service_)
        with self.assertRaises(FillPersistenceError) as caught:
            service_.record_fill(order.client_order_id,
                                 fill(40, 100.0, order_id=order.client_order_id))
        self.assertIn("cannot store durable provider fills", str(caught.exception))
        self.assertEqual(service_.get(order.client_order_id).fills, ())

    def test_no_journal_attached_means_nothing_pretends_to_be_durable(self):
        service_ = lifecycle()
        order = opened(service_)
        updated = service_.record_fill(order.client_order_id,
                                       fill(40, 100.0,
                                            order_id=order.client_order_id))
        self.assertEqual(len(updated.fills), 1)

    def test_a_fill_arriving_during_a_cancel_is_refused_not_kept(self):
        class FillRefusingJournal:
            """A real journal that accepts everything except a fill."""

            def __init__(self):
                self.real = SQLiteJournalRepository(":memory:")

            def __getattr__(self, name):
                return getattr(self.real, name)

            def record_provider_fill(self, *args, **kwargs):
                raise RuntimeError("database unavailable")

        class CancellingAdapter:
            def cancel_order(self, provider_order_id):
                return SimpleNamespace(
                    order_id=provider_order_id, status=OrderStatus.CANCELLED,
                    fills=(fill(40, 100.0, order_id="CLIENT-1",
                                provider_fill_id="PF-1"),))

        journal = FillRefusingJournal()
        try:
            service_ = OrderLifecycleService(journal=journal, clock=CLOCK,
                                             restore=False,
                                             executor=CancellingAdapter())
            order = opened(service_)
            with self.assertRaises(FillPersistenceError):
                service_.cancel(order.client_order_id)
            stored = service_.get(order.client_order_id)
            self.assertEqual(stored.fills, (),
                             "a refused fill must never become visible in memory")
            self.assertEqual(stored.filled_quantity, 0.0)
            self.assertIs(stored.status, OrderStatus.SUBMITTED,
                          "the cancellation must not half-apply either")
            self.assertEqual(len(journal.real.rows("fills")), 0)
            orders = journal.real.rows("orders")
            self.assertEqual(len(orders), 1)
            self.assertFalse(orders[0]["context_json"].get("fills"),
                             "the order row must not carry the refused fill, "
                             "or a restart would resurrect it")
        finally:
            journal.real.close()


class JournalSchemaTests(unittest.TestCase):
    def test_the_fill_journal_carries_scope_identity_and_a_uniqueness_rule(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            columns = [row["name"] for row in journal._query(
                "PRAGMA table_info(order_fill_journal)", ())]
            self.assertEqual(columns, [
                "connection_id", "provider_instance_id", "fill_id",
                "client_order_id", "provider_order_id", "order_id",
                "provider_fill_id", "quantity", "price", "timestamp", "fee",
                "symbol", "side", "context_json"])
            indexes = {row["name"]: row for row in journal._query(
                "PRAGMA index_list(order_fill_journal)", ())}
            self.assertIn("ix_fill_journal_order", indexes)
            self.assertIn("ux_fill_journal_provider", indexes)
            self.assertEqual(indexes["ux_fill_journal_provider"]["unique"], 1)
            self.assertEqual(indexes["ux_fill_journal_provider"]["partial"], 1)
            # The phase 9 table is left exactly as it was: CP2 adds a new
            # home for fills instead of migrating an existing one.
            legacy = [row["name"] for row in journal._query(
                "PRAGMA table_info(order_fills)", ())]
            self.assertIn("fill_id", legacy)
            self.assertIn("order_id", legacy)
        finally:
            journal.close()

    def test_a_fill_without_an_identity_is_refused_by_the_journal(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            with self.assertRaises(ValueError):
                journal.record_provider_fill(
                    Fill(10, 100.0, NOW), connection_id="paper")
            with self.assertRaises(ValueError):
                journal.record_provider_fill(
                    Fill(10, 100.0, None, fill_id="paper:inst:PF-1"),
                    connection_id="paper")
            self.assertEqual(journal.rows("fills"), ())
        finally:
            journal.close()


if __name__ == "__main__":
    unittest.main()
