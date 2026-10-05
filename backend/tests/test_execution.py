import unittest

from backend.execution import MockExecutionProvider, OrderRequest, OrderStatus
from backend.strategies.models import Direction


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


if __name__ == "__main__":
    unittest.main()
