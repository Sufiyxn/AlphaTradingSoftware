import unittest
from datetime import datetime, timezone

from backend.execution import AdapterRegistry, ExecutionRouter, MockExecutionProvider, MockTradingPlatformAdapter, OrderStatus
from backend.paper_trading import PaperTradingConfig, PaperTradingEngine
from backend.portfolio import Portfolio
from backend.risk import RiskEngine
from backend.strategies.models import Direction, TradeSetup


NOW = datetime(2024, 1, 8, 15, tzinfo=timezone.utc)


def setup(symbol="ABC", direction=Direction.LONG, ratio=2.0):
    entry, risk = 100.0, 5.0
    sign = 1 if direction is Direction.LONG else -1
    return TradeSetup(symbol, direction, entry, entry - sign * risk, entry + sign * risk * ratio,
                      risk, risk * ratio, ratio, NOW, "paper-test", .5, "deterministic")


def paper(*, execution=None, config=None, risk=None):
    portfolio = Portfolio(100_000)
    engine = PaperTradingEngine(risk or RiskEngine(), portfolio, execution, config)
    return engine, portfolio


class PaperTradingTests(unittest.TestCase):
    def test_long_fill_and_target_exit(self):
        engine, portfolio = paper(config=PaperTradingConfig(commission_per_share=.01, slippage_per_share=.1))
        result = engine.submit(setup())
        self.assertTrue(result.accepted)
        self.assertEqual(result.order.status, OrderStatus.FILLED)
        self.assertGreater(result.trade.entry_price, 100)
        closed = engine.on_bar("ABC", NOW, 111, 99)
        self.assertEqual(len(closed), 1)
        self.assertEqual(portfolio.snapshot().positions, ())
        self.assertEqual(portfolio.snapshot().trade_count, 1)

    def test_stop_exit_and_same_bar_stop_first(self):
        engine, portfolio = paper()
        engine.submit(setup())
        engine.on_bar("ABC", NOW, 111, 94)
        self.assertLess(portfolio.snapshot().realized_pnl, -1000)

    def test_short_fill_and_exit(self):
        engine, portfolio = paper()
        result = engine.submit(setup("SHORT", Direction.SHORT))
        self.assertTrue(result.accepted)
        engine.on_bar("SHORT", NOW, 100, 89)
        self.assertGreater(portfolio.snapshot().realized_pnl, 0)

    def test_risk_rejection_never_reaches_provider(self):
        provider = MockExecutionProvider()
        engine, _ = paper(execution=provider)
        result = engine.submit(setup(ratio=1.5))
        self.assertFalse(result.accepted)
        self.assertEqual(provider.get_orders(), ())

    def test_multiple_symbols_and_partial_fill(self):
        provider = MockExecutionProvider(partial_fill_quantity=2)
        engine, portfolio = paper(execution=provider)
        first, second = engine.submit(setup("AAA")), engine.submit(setup("BBB"))
        self.assertEqual(first.order.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(first.trade.quantity, 2)
        self.assertEqual(len(portfolio.positions), 2)

    def test_failed_fill_representation(self):
        engine, portfolio = paper(execution=MockExecutionProvider(fail_orders=True))
        result = engine.submit(setup())
        self.assertFalse(result.accepted)
        self.assertEqual(result.order.status, OrderStatus.REJECTED)
        self.assertEqual(portfolio.positions, ())

    def test_manual_close_fees_and_slippage(self):
        engine, portfolio = paper(config=PaperTradingConfig(commission_per_share=.1, slippage_per_share=.2))
        engine.submit(setup())
        pnl = engine.close("ABC", 110, NOW)
        self.assertLess(pnl, 2000)
        self.assertEqual(portfolio.positions, ())

    def test_open_position_does_not_close_when_levels_untouched(self):
        engine, portfolio = paper()
        engine.submit(setup())
        self.assertEqual(engine.on_bar("ABC", NOW, 104, 96), ())
        self.assertEqual(len(portfolio.positions), 1)

    def test_paper_trading_operates_through_universal_router(self):
        registry = AdapterRegistry()
        registry.register(MockTradingPlatformAdapter())
        router = ExecutionRouter(registry, "mock")
        engine, portfolio = paper(execution=router)
        result = engine.submit(setup())
        self.assertTrue(result.accepted)
        self.assertEqual(len(portfolio.positions), 1)
        self.assertEqual(router.get_order(result.order.order_id), result.order)


if __name__ == "__main__":
    unittest.main()
