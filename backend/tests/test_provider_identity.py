"""Phase 10 CP1 proof: durable provider-order identity.

A provider order number means nothing on its own. This module holds the
identity the platform actually relies on - ``connection_id +
provider_instance_id + provider_order_id`` - and the rules that keep it
meaning one order:

- the platform's own ``client_order_id`` travels to the provider and comes
  back on the normalized remote order, so reconciliation can prefer identity
  it recognizes instead of guessing;
- the provider instance and its order sequence are durable, so a restart
  continues numbering instead of repeating identities it already handed out;
- registering an identity another local order already owns is refused fail
  closed rather than silently adopted;
- reconciliation only compares orders inside one connection/instance scope
  and refuses an ambiguous match;
- a submitted order can still be cancelled after a restart, against the
  identity the provider gave it before the restart.
"""
import tempfile
import unittest
from pathlib import Path

from backend.connections import ConnectionManager
from backend.database import SQLiteJournalRepository
from backend.execution import (
    ExecutionMode,
    LocalPaperAdapter,
    MockTradingPlatformAdapter,
    OrderRequest,
    OrderStatus,
)
from backend.execution.identity import ConnectionInstanceRegistry
from backend.orders import (
    CancellationOutcome,
    DiscrepancyKind,
    Order,
    OrderIntent,
    OrderLifecycleService,
    OrderReconciler,
    OrderStore,
    ProviderIdentityCollision,
    ProviderStateSnapshot,
    ReconcileStatus,
    RemoteOrder,
)
from backend.orders.provider import snapshot_from_adapter
from backend.pipeline import PipelineCode, TradingPipeline
from backend.portfolio import Portfolio
from backend.strategies.models import Direction
from backend.testing import ScriptedAdapter

from backend.tests.test_pipeline import (
    CLOCK,
    NOW,
    SpyAdapter,
    SpyRisk,
    make_intent,
    make_manager,
    make_pipeline,
    registry_for,
    trade_setup,
)

GHOST_IDENTITY = "MOCK-00000001-000001"


def intent(client: str, *, connection_id=None) -> OrderIntent:
    return OrderIntent(client_order_id=client, symbol="ABC",
                       direction=Direction.LONG, quantity=100.0,
                       limit_price=100.0, connection_id=connection_id)


def lifecycle(journal=None) -> OrderLifecycleService:
    return OrderLifecycleService(journal=journal, clock=CLOCK, restore=False)


def submitted(service, *, client="CLIENT-1", provider_order_id="PROV-1",
              connection_id="paper", provider_instance_id="instance-1",
              status=OrderStatus.ACCEPTED) -> Order:
    service.create(intent(client), connection_id=connection_id,
                   provider_instance_id=provider_instance_id)
    service.mark_submitted(client, provider_order_id,
                           provider_instance_id=provider_instance_id)
    if status is not OrderStatus.SUBMITTED:
        service.apply_status(client, status)
    return service.get(client)


def remote(*, provider_order_id="PROV-1", client="CLIENT-1",
           status=OrderStatus.ACCEPTED, filled=0.0) -> RemoteOrder:
    return RemoteOrder(provider_order_id=provider_order_id, symbol="ABC",
                       quantity=100.0, filled_quantity=filled, status=status,
                       client_order_id=client)


def snapshot(*, orders=(), complete=True, error=None,
             connection_id="paper", provider_instance_id="instance-1") -> ProviderStateSnapshot:
    return ProviderStateSnapshot(orders=tuple(orders), complete=complete,
                                 error=error, retrieved_at=NOW,
                                 connection_id=connection_id,
                                 provider_id="mock",
                                 provider_instance_id=provider_instance_id)


class ReissuedIdentityAdapter(SpyAdapter):
    """A provider whose own counter reset: it re-issues an identity it gave out."""

    def place_order(self, request):
        self.requests.append(request)
        self.trail.append("adapter")
        from backend.execution.models import Order as ProviderOrder
        return ProviderOrder(GHOST_IDENTITY, request, OrderStatus.SUBMITTED,
                             0, None, NOW)


class ClientOrderIdTests(unittest.TestCase):
    def test_the_client_order_id_is_optional_and_never_blank(self):
        self.assertIsNone(OrderRequest("ABC", Direction.LONG, 1, 100.0).client_order_id)
        carrying = OrderRequest("ABC", Direction.LONG, 1, 100.0,
                                client_order_id="CLIENT-1")
        self.assertEqual(carrying.client_order_id, "CLIENT-1")
        blank = OrderRequest("ABC", Direction.LONG, 1, 100.0, client_order_id="   ")
        self.assertIsNone(blank.client_order_id, "an empty id is not an identity")

    def test_the_intent_builds_its_request_with_the_same_id(self):
        request = intent("CLIENT-1").to_order_request()
        self.assertEqual(request.client_order_id, "CLIENT-1")

    def test_the_pipeline_hands_the_orders_own_id_to_the_provider(self):
        pipeline, _, adapter, _, _ = make_pipeline()
        result = pipeline.execute(make_intent(setup=trade_setup("ABC")))

        self.assertEqual(result.code, PipelineCode.ACCEPTED)
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(adapter.requests[0].client_order_id,
                         result.order.client_order_id,
                         "the provider must receive the id the platform owns")
        self.assertEqual(len(result.order.client_order_id), 32)

    def test_the_normalized_remote_order_carries_the_echoed_id(self):
        adapter = MockTradingPlatformAdapter(clock=CLOCK)
        adapter.place_order(OrderRequest("ABC", Direction.LONG, 1, 100.0,
                                         client_order_id="CLIENT-1"))
        echoed = snapshot_from_adapter(adapter, now=NOW, connection_id="paper")
        self.assertEqual(echoed.orders[0].client_order_id, "CLIENT-1")
        self.assertEqual(echoed.provider_instance_id, adapter.provider_instance_id)

        silent = MockTradingPlatformAdapter(clock=CLOCK)
        silent.place_order(OrderRequest("ABC", Direction.LONG, 1, 100.0))
        plain = snapshot_from_adapter(silent, now=NOW, connection_id="paper")
        self.assertIsNone(plain.orders[0].client_order_id,
                          "a provider that does not echo the id never gets one invented")


class DurableInstanceTests(unittest.TestCase):
    def test_the_instance_id_and_sequence_recover_in_a_new_process(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            first = ConnectionInstanceRegistry(journal)
            self.assertTrue(first.durable)
            identity = first.bind("paper")
            self.assertEqual([identity.next_sequence(), identity.next_sequence()],
                             [1, 2])

            second = ConnectionInstanceRegistry(journal)
            recovered = second.bind("paper")
            self.assertEqual(recovered.instance_id, identity.instance_id,
                             "a new process must recover the instance, not mint a rival")
            self.assertEqual(recovered.next_sequence(), 3,
                             "the sequence continues instead of repeating")
        finally:
            journal.close()

    def test_the_first_writer_owns_the_connection_instance(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            self.assertIsNone(journal.connection_instance("paper"))
            created = journal.bind_connection_instance(
                "paper", instance_id="abc12345", created_at=NOW)
            self.assertEqual(created["instance_id"], "abc12345")
            overwritten = journal.bind_connection_instance(
                "paper", instance_id="ffffffff", created_at=NOW)
            self.assertEqual(overwritten["instance_id"], "abc12345",
                             "a later bind must not replace a durable identity")
            self.assertEqual(journal.next_provider_sequence("paper"), 1)
            self.assertEqual(journal.next_provider_sequence("paper"), 2)
            with self.assertRaises(KeyError):
                journal.next_provider_sequence("never-bound")
        finally:
            journal.close()

    def test_the_record_and_the_adapter_report_the_same_instance(self):
        adapter = MockTradingPlatformAdapter(clock=CLOCK)
        manager = make_manager(adapter)
        record = manager.get("paper")

        self.assertIsNotNone(record.provider_instance_id)
        self.assertEqual(adapter.provider_instance_id, record.provider_instance_id)
        self.assertEqual(record.describe()["provider_instance_id"],
                         record.provider_instance_id)

    def test_two_connections_never_share_an_instance(self):
        first = make_manager(MockTradingPlatformAdapter(clock=CLOCK))
        second = make_manager(MockTradingPlatformAdapter(clock=CLOCK))
        self.assertNotEqual(first.get("paper").provider_instance_id,
                            second.get("paper").provider_instance_id,
                            "without a journal every process mints its own instance")

    def test_the_identity_table_is_created_alongside_the_existing_schema(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            tables = {row["name"] for row in journal._query(
                "SELECT name FROM sqlite_master WHERE type='table'", ())}
            self.assertIn("connection_instances", tables)
            self.assertIn("orders", tables)
            self.assertIn("order_fills", tables)
        finally:
            journal.close()


class RestartSafeProviderIdTests(unittest.TestCase):
    def test_paper_order_ids_do_not_repeat_after_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "alpha.sqlite3")

            def start_process():
                repository = SQLiteJournalRepository(path)
                adapter = LocalPaperAdapter(clock=CLOCK)
                manager = ConnectionManager(registry=registry_for(adapter),
                                            clock=CLOCK, journal=repository)
                manager.add(connection_id="paper", provider_id=str(adapter.identify()),
                            label="Local paper account", mode=ExecutionMode.PAPER,
                            adapter=adapter)
                return repository, adapter, manager

            repository_a, adapter_a, manager_a = start_process()
            first = adapter_a.place_order(OrderRequest("ABC", Direction.LONG, 1, 100.0))
            second = adapter_a.place_order(OrderRequest("ABC", Direction.LONG, 2, 100.0))
            instance_a = manager_a.get("paper").provider_instance_id
            self.assertEqual(first.order_id, f"PAPER-{instance_a}-000001")
            self.assertEqual(second.order_id, f"PAPER-{instance_a}-000002")
            repository_a.close()

            repository_b, adapter_b, manager_b = start_process()
            try:
                instance_b = manager_b.get("paper").provider_instance_id
                self.assertEqual(instance_b, instance_a,
                                 "the durable instance id must be recovered, not re-minted")
                after_restart = adapter_b.place_order(
                    OrderRequest("ABC", Direction.LONG, 3, 100.0))
                self.assertEqual(after_restart.order_id, f"PAPER-{instance_b}-000003")
                self.assertNotIn(after_restart.order_id, {first.order_id, second.order_id})
            finally:
                repository_b.close()


class IdentityCollisionTests(unittest.TestCase):
    def test_one_instance_may_not_hand_the_same_identity_to_two_orders(self):
        journal = SQLiteJournalRepository(":memory:")
        try:
            service = lifecycle(journal)
            submitted(service, client="OWNER-1")
            service.create(intent("NEWCOMER"), connection_id="paper",
                           provider_instance_id="instance-1")
            with self.assertRaises(ProviderIdentityCollision) as caught:
                service.mark_submitted("NEWCOMER", "PROV-1",
                                       provider_instance_id="instance-1")

            collision = caught.exception
            self.assertEqual(collision.conflicting_order_id, "OWNER-1")
            self.assertEqual(collision.provider_order_id, "PROV-1")
            self.assertEqual(service.get("OWNER-1").provider_order_id, "PROV-1",
                             "the order that owns the identity keeps it")
            self.assertIsNone(service.get("NEWCOMER").provider_order_id)
            self.assertIs(service.get("NEWCOMER").status, OrderStatus.NEW)

            findings = [event for event in journal.events()
                        if event["event_type"] == "RECONCILIATION_DISCREPANCY"]
            self.assertTrue(findings)
            self.assertEqual(findings[0]["context_json"]["kind"],
                             DiscrepancyKind.PROVIDER_ORDER_ID_COLLISION.value)
            self.assertEqual(findings[0]["context_json"]["conflicting_order_id"],
                             "OWNER-1")
        finally:
            journal.close()

    def test_provably_different_scopes_may_share_a_provider_number(self):
        service = lifecycle()
        submitted(service, client="OWNER-1", connection_id="paper",
                  provider_instance_id="instance-1")
        # A different connection is a different identity...
        service.create(intent("OTHER-CONNECTION"), connection_id="client",
                       provider_instance_id="instance-1")
        service.mark_submitted("OTHER-CONNECTION", "PROV-1",
                               provider_instance_id="instance-1")
        # ...and so is a different provider instance on the same connection.
        service.create(intent("OTHER-INSTANCE"), connection_id="paper",
                       provider_instance_id="instance-2")
        service.mark_submitted("OTHER-INSTANCE", "PROV-1",
                               provider_instance_id="instance-2")
        self.assertEqual(len(service.list()), 3)

    def test_an_unknown_scope_fails_closed(self):
        service = lifecycle()
        service.create(intent("FIRST"))
        service.mark_submitted("FIRST", "PROV-1")
        service.create(intent("SECOND"))
        with self.assertRaises(ProviderIdentityCollision):
            service.mark_submitted("SECOND", "PROV-1")


class PipelineCollisionTests(unittest.TestCase):
    def build(self):
        journal = SQLiteJournalRepository(":memory:")
        adapter = ReissuedIdentityAdapter(clock=CLOCK)
        pipeline, _, adapter, _, _ = make_pipeline(adapter=adapter, journal=journal)
        return journal, pipeline, adapter

    def test_a_reissued_identity_is_refused_rather_than_adopted(self):
        journal, pipeline, adapter = self.build()
        try:
            first = pipeline.execute(make_intent(setup=trade_setup("ABC")))
            self.assertEqual(first.code, PipelineCode.SUBMITTED)
            self.assertEqual(first.order.provider_order_id, GHOST_IDENTITY)

            # A different symbol so the risk step approves it too: risk
            # decides once, before execution, exactly as in production.
            second = pipeline.execute(make_intent(setup=trade_setup("XYZ")))

            self.assertEqual(second.code, PipelineCode.PROVIDER_ID_COLLISION)
            self.assertEqual(len(adapter.requests), 2,
                             "the provider really was sent the order")
            self.assertIsNotNone(second.reason)
            self.assertIn(GHOST_IDENTITY, second.reason)
            self.assertIs(second.order.status, OrderStatus.FAILED)
            self.assertIsNone(second.order.provider_order_id,
                              "a colliding identity is never registered")
            owner = pipeline.orders.get(first.order.client_order_id)
            self.assertEqual(owner.provider_order_id, GHOST_IDENTITY,
                             "the order that owns the identity keeps it")
            self.assertEqual(len(pipeline.orders.pending_reservations()), 1,
                             "a failed order must not keep holding capacity")

            rejected = [event for event in journal.events()
                        if event["event_type"] == "EXECUTION_REJECTED"]
            self.assertTrue(any(event["context_json"].get("code")
                                == PipelineCode.PROVIDER_ID_COLLISION
                                for event in rejected))
        finally:
            journal.close()


class ScopedReconciliationTests(unittest.TestCase):
    def test_one_provider_instance_matches_and_another_never_does(self):
        service = lifecycle()
        submitted(service, connection_id="paper", provider_instance_id="instance-1")
        agreeing = service.reconciler.reconcile(
            snapshot(orders=(remote(),), provider_instance_id="instance-1"))
        self.assertIs(agreeing.status, ReconcileStatus.MATCH)
        self.assertEqual(agreeing.matched_order_count, 1)

        rival = service.reconciler.reconcile(
            snapshot(orders=(remote(),), provider_instance_id="instance-2"))
        self.assertIs(rival.status, ReconcileStatus.DISCREPANCIES)
        self.assertEqual(rival.local_order_count, 0,
                         "an order minted by another instance is not in this view")
        self.assertFalse(rival.has(DiscrepancyKind.LOCAL_ORDER_MISSING_REMOTELY))
        self.assertTrue(rival.has(DiscrepancyKind.REMOTE_ORDER_UNKNOWN_LOCALLY))

    def test_an_order_on_another_connection_is_not_this_snapshots_business(self):
        service = lifecycle()
        submitted(service, client="CLIENT-2", connection_id="client",
                  provider_instance_id="instance-2")
        report = service.reconciler.reconcile(snapshot(orders=(), connection_id="paper"))
        self.assertFalse(report.has(DiscrepancyKind.LOCAL_ORDER_MISSING_REMOTELY))
        self.assertIs(report.status, ReconcileStatus.MATCH)
        self.assertEqual(report.local_order_count, 0)

    def test_duplicate_local_identities_inside_one_scope_are_reported(self):
        store = OrderStore()
        for client in ("FIRST", "SECOND"):
            store.save(Order(intent=intent(client), status=OrderStatus.ACCEPTED,
                             created_at=NOW, provider_order_id="PROV-1",
                             connection_id="paper",
                             provider_instance_id="instance-1"))
        report = OrderReconciler(store).reconcile(snapshot(orders=(remote(),)))
        self.assertTrue(report.has(DiscrepancyKind.PROVIDER_ORDER_ID_COLLISION))
        self.assertIs(report.status, ReconcileStatus.DISCREPANCIES)

    def test_the_same_number_from_two_instances_is_not_a_collision(self):
        store = OrderStore()
        for client, instance in (("FIRST", "instance-1"), ("SECOND", "instance-2")):
            store.save(Order(intent=intent(client), status=OrderStatus.ACCEPTED,
                             created_at=NOW, provider_order_id="PROV-1",
                             connection_id="paper", provider_instance_id=instance))
        report = OrderReconciler(store).reconcile(
            snapshot(orders=(remote(),), provider_instance_id=None))
        self.assertFalse(report.has(DiscrepancyKind.PROVIDER_ORDER_ID_COLLISION))


class CancellationAfterRestartTests(unittest.TestCase):
    def test_a_submitted_order_is_still_cancellable_after_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "alpha.sqlite3")
            # The provider is external to our process: it keeps its state
            # across the restart while the platform side is rebuilt.
            provider = ScriptedAdapter(authenticated=True, clock=CLOCK)

            def start_process():
                repository = SQLiteJournalRepository(path)
                manager = ConnectionManager(registry=registry_for(provider),
                                            clock=CLOCK, journal=repository)
                manager.add(connection_id="paper", provider_id=str(provider.identify()),
                            label="Scripted provider", mode=ExecutionMode.PAPER,
                            adapter=provider)
                manager.verify("paper")
                pipeline = TradingPipeline(
                    connections=manager, risk_engine=SpyRisk(),
                    portfolio=Portfolio(100_000), journal=repository,
                    clock=CLOCK, connection_id="paper")
                return repository, manager, pipeline

            repository_a, manager_a, pipeline_a = start_process()
            result = pipeline_a.execute(make_intent(setup=trade_setup("ABC"),
                                                    limit_price=100.0,
                                                    requested_quantity=10))
            self.assertEqual(result.code, PipelineCode.SUBMITTED,
                             (result.code, result.reason))
            order_id = result.order.client_order_id
            provider_order_id = result.order.provider_order_id
            instance_a = manager_a.get("paper").provider_instance_id
            repository_a.close()

            repository_b, manager_b, pipeline_b = start_process()
            try:
                restored = pipeline_b.orders.get(order_id)
                self.assertIsNotNone(restored, "the open order must survive the restart")
                self.assertEqual(restored.provider_order_id, provider_order_id)
                self.assertEqual(manager_b.get("paper").provider_instance_id,
                                 instance_a,
                                 "the connection must restart as the same instance")
                self.assertEqual(restored.provider_instance_id, instance_a)

                outcome = pipeline_b.orders.cancel(order_id)

                self.assertIs(outcome.outcome, CancellationOutcome.CANCELLED,
                              outcome.message)
                self.assertEqual(outcome.provider_order_id, provider_order_id,
                                 "cancellation must target the identity the "
                                 "provider gave before the restart")
                self.assertIs(provider.get_order(provider_order_id).status,
                              OrderStatus.CANCELLED,
                              "the provider must actually be told to cancel")
                self.assertIs(pipeline_b.orders.get(order_id).status,
                              OrderStatus.CANCELLED)
                self.assertEqual(pipeline_b.orders.pending_reservations(), ())
            finally:
                repository_b.close()


if __name__ == "__main__":
    unittest.main()
