import unittest

from backend.execution import (
    AdapterRegistry, AssetType, ExecutionRouter, MockExecutionProvider,
    MockTradingPlatformAdapter, OrderRequest, OrderStatus, OrderType,
    PlatformCapabilities,
)
from backend.portfolio.models import Position
from backend.strategies.models import Direction
from datetime import datetime, timezone


class ExecutionTests(unittest.TestCase):
    def test_deterministic_order_lifecycle(self):
        provider = MockExecutionProvider()
        request = OrderRequest("ABC", Direction.LONG, 10, 100)
        order = provider.place_order(request)
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(order.order_id, "MOCK-000001")
        self.assertEqual(provider.get_order(order.order_id), order)
        self.assertEqual(provider.cancel_order(order.order_id).status, OrderStatus.CANCELLED)

    def test_partial_fill_and_rejection(self):
        partial = MockExecutionProvider(partial_fill_quantity=3).place_order(OrderRequest("X", Direction.SHORT, 10, 20))
        rejected = MockExecutionProvider(fail_orders=True).place_order(OrderRequest("X", Direction.SHORT, 10, 20))
        self.assertEqual((partial.status, partial.filled_quantity), (OrderStatus.PARTIALLY_FILLED, 3))
        self.assertEqual(rejected.status, OrderStatus.REJECTED)

    def test_repeatable_timestamps_and_snapshots(self):
        first = MockExecutionProvider().place_order(OrderRequest("X", Direction.LONG, 1, 10))
        second = MockExecutionProvider().place_order(OrderRequest("X", Direction.LONG, 1, 10))
        self.assertEqual(first, second)

    def test_registry_router_and_normalized_account_quote_and_position_views(self):
        registry = AdapterRegistry()
        expected_position = Position("XYZ", Direction.LONG, 2, 25, 26,
                                     datetime(2000, 1, 1, tzinfo=timezone.utc))
        adapter = MockTradingPlatformAdapter(positions=(expected_position,))
        registry.register(adapter)
        router = ExecutionRouter(registry, "MOCK")
        self.assertEqual(registry.names(), ("mock",))
        self.assertEqual(router.identify(), "mock")
        self.assertTrue(router.capabilities().market_orders)
        self.assertEqual(router.get_account().balances[0].currency, "USD")
        self.assertEqual(router.get_positions(), (expected_position,))
        self.assertEqual(router.get_orders(), ())
        self.assertEqual(router.get_quote("abc").symbol, "ABC")
        order = router.place_order(OrderRequest(" abc ", Direction.LONG, 1, 10))
        self.assertEqual(order.request.symbol, "ABC")
        router.disconnect()
        router.connect()
        self.assertFalse(router.is_authenticated())

    def test_capability_rejections_are_explicit_and_adapter_isolated(self):
        restricted = MockTradingPlatformAdapter(capabilities=PlatformCapabilities(
            asset_types=frozenset({AssetType.STOCK}), exchanges=frozenset({"NYSE"}),
            order_types=frozenset({OrderType.LIMIT}), short_selling=False,
            fractional_shares=False, order_replacement=False))
        registry = AdapterRegistry()
        registry.register(restricted, "restricted")
        router = ExecutionRouter(registry, "restricted")
        short = OrderRequest("ABC", Direction.SHORT, 1, 10)
        rejected = router.place_order(short)
        self.assertEqual(rejected.status, OrderStatus.REJECTED)
        self.assertEqual(rejected.rejection_reason, "short selling is not supported")
        self.assertEqual(restricted.get_orders(), ())
        fractional = router.place_order(OrderRequest("ABC", Direction.LONG, 1.5, 10, fractional=True))
        self.assertEqual(fractional.rejection_reason, "fractional shares are not supported")
        market = router.place_order(OrderRequest("ABC", Direction.LONG, 1, order_type=OrderType.MARKET))
        self.assertIn("unsupported order type", market.rejection_reason)
        wrong_exchange = router.place_order(OrderRequest("ABC", Direction.LONG, 1, 10, exchange="NASDAQ"))
        self.assertEqual(wrong_exchange.rejection_reason, "unsupported exchange/market: NASDAQ")
        self.assertEqual(restricted.get_orders(), ())

    def test_unsupported_read_capabilities_are_not_silently_emulated(self):
        registry = AdapterRegistry()
        registry.register(MockTradingPlatformAdapter(capabilities=PlatformCapabilities(
            account_retrieval=False, position_retrieval=False, order_retrieval=False,
            market_data=False)))
        router = ExecutionRouter(registry, "mock")
        for operation in (router.get_account, router.get_positions, router.get_orders,
                          lambda: router.get_quote("ABC")):
            with self.assertRaises(NotImplementedError):
                operation()

    def test_router_place_cancel_replace_and_deterministic_orders(self):
        registry = AdapterRegistry()
        registry.register(MockTradingPlatformAdapter())
        router = ExecutionRouter(registry, "mock")
        original = router.place_order(OrderRequest("ABC", Direction.LONG, 2, 10))
        self.assertEqual((original.status, original.filled_quantity), (OrderStatus.FILLED, 2))
        self.assertEqual(len(original.fills), 1)
        replaced = router.replace_order(original.order_id, OrderRequest("ABC", Direction.LONG, 3, 11))
        self.assertEqual(replaced.request.quantity, 3)
        self.assertEqual(replaced.status, OrderStatus.FILLED)
        self.assertEqual(router.cancel_order(original.order_id).status, OrderStatus.CANCELLED)
        self.assertEqual(router.place_order(OrderRequest("ABC", Direction.LONG, 1, 10)).order_id, "MOCK-000002")

    def test_registry_rejects_duplicate_and_reports_unknown_adapter(self):
        registry = AdapterRegistry()
        registry.register(MockTradingPlatformAdapter())
        with self.assertRaises(ValueError):
            registry.register(MockTradingPlatformAdapter())
        with self.assertRaisesRegex(KeyError, "unknown trading adapter"):
            registry.get("not-configured")


if __name__ == "__main__":
    unittest.main()
