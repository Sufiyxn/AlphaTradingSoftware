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


def mark(portfolio, symbol, price=100.0, timestamp=NOW):
    """Simulate a connected provider delivering a real quote for the position.

    Automatic stop-loss/take-profit is gated on a FRESH real mark, so a test
    that wants an exit must first supply one - exactly like production.
    """
    portfolio.update_price(symbol, price, timestamp=timestamp)


class PaperTradingTests(unittest.TestCase):
    def test_long_fill_and_target_exit(self):
        engine, portfolio = paper(config=PaperTradingConfig(commission_per_share=.01, slippage_per_share=.1))
        result = engine.submit(setup())
        self.assertTrue(result.accepted)
        self.assertEqual(result.order.status, OrderStatus.FILLED)
        self.assertGreater(result.trade.entry_price, 100)
        mark(portfolio, "ABC")
        closed = engine.on_bar("ABC", NOW, 111, 99)
        self.assertEqual(len(closed), 1)
        self.assertEqual(portfolio.snapshot().positions, ())
        self.assertEqual(portfolio.snapshot().trade_count, 1)

    def test_stop_exit_and_same_bar_stop_first(self):
        engine, portfolio = paper()
        engine.submit(setup())
        mark(portfolio, "ABC")
        engine.on_bar("ABC", NOW, 111, 94)
        self.assertLess(portfolio.snapshot().realized_pnl, -1000)

    def test_short_fill_and_exit(self):
        engine, portfolio = paper()
        result = engine.submit(setup("SHORT", Direction.SHORT))
        self.assertTrue(result.accepted)
        mark(portfolio, "SHORT")
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
        mark(portfolio, "ABC")
        self.assertEqual(engine.on_bar("ABC", NOW, 104, 96), ())
        self.assertEqual(len(portfolio.positions), 1)

    def test_unpriced_position_is_never_exited_automatically(self):
        engine, portfolio = paper()
        engine.submit(setup())
        self.assertIsNone(portfolio.positions[0].market_price)
        self.assertEqual(engine.on_bar("ABC", NOW, 0.01, 0.01), ())
        self.assertEqual(engine.on_quote("ABC", NOW, 0.01), ())
        self.assertEqual(len(portfolio.positions), 1)

    def test_paper_trading_operates_through_universal_router(self):
        registry = AdapterRegistry()
        registry.register(MockTradingPlatformAdapter())
        router = ExecutionRouter(registry, "mock")
        engine, portfolio = paper(execution=router)
        result = engine.submit(setup())
        self.assertTrue(result.accepted)
        self.assertEqual(len(portfolio.positions), 1)
        provider_order = router.get_order(result.order.provider_order_id)
        self.assertIsNotNone(provider_order,
                             "the provider must hold the order under its own id")
        self.assertEqual(provider_order.order_id, result.order.provider_order_id)
        self.assertEqual(provider_order.status, OrderStatus.FILLED)
        self.assertNotEqual(result.order.order_id, provider_order.order_id,
                            "the platform's order id is never the provider's")

    def test_kill_switch_blocks_a_previously_approved_submission(self):
        provider = MockExecutionProvider()
        risk = RiskEngine()
        engine, _ = paper(execution=provider, risk=risk)
        decision = risk.evaluate(setup(), engine.portfolio.snapshot())
        self.assertTrue(decision.approved)
        risk.set_kill_switch(True)
        result = engine.submit_decision(decision)
        self.assertFalse(result.accepted)
        self.assertIn("kill switch enabled", result.risk.reasons)
        self.assertEqual(provider.get_orders(), ())


if __name__ == "__main__":
    unittest.main()
