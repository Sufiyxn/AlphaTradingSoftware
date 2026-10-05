import unittest
from dataclasses import replace
from datetime import datetime, timezone

from backend.portfolio import Portfolio
from backend.risk import RiskConfig, RiskEngine, RiskStatus
from backend.strategies.models import Direction, TradeSetup


NOW = datetime(2024, 1, 8, tzinfo=timezone.utc)


def setup(ratio=2.0, entry=100.0, direction=Direction.LONG):
    risk = 5.0
    signed = 1 if direction is Direction.LONG else -1
    return TradeSetup("ABC", direction, entry, entry - signed * risk, entry + signed * risk * ratio,
                      risk, risk * ratio, ratio, NOW, "fixture", 1, "test")


class RiskTests(unittest.TestCase):
    def test_reward_risk_exact_and_above_approved(self):
        portfolio = Portfolio(100_000).snapshot()
        for ratio in (2.0, 2.5):
            decision = RiskEngine().evaluate(setup(ratio), portfolio)
            self.assertEqual(decision.status, RiskStatus.APPROVED)
            self.assertGreater(decision.quantity, 0)

    def test_below_two_rejected(self):
        decision = RiskEngine().evaluate(setup(1.99), Portfolio().snapshot())
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertIn("reward/risk is below minimum", decision.reasons)

    def test_excessive_risk_and_position_size(self):
        cfg = RiskConfig(max_risk_per_trade=.001, max_risk_amount=4, max_position_notional=100)
        decision = RiskEngine(cfg).evaluate(setup(), Portfolio(100_000).snapshot())
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertTrue(decision.reasons)

    def test_exposure_and_simultaneous_position_limits(self):
        portfolio = Portfolio(10_000)
        portfolio.open_position("OTHER", Direction.LONG, 60, 100, NOW)
        cfg = RiskConfig(max_portfolio_exposure_fraction=.5, max_simultaneous_positions=1)
        decision = RiskEngine(cfg).evaluate(setup(), portfolio.snapshot())
        self.assertIn("maximum simultaneous positions reached", decision.reasons)
        self.assertIn("maximum portfolio exposure exceeded", decision.reasons)

    def test_daily_loss_limit_and_duplicate_position(self):
        portfolio = Portfolio(10_000)
        portfolio.open_position("OLD", Direction.LONG, 1, 10, NOW)
        portfolio.close_position("OLD", price=10, fees=400, timestamp=NOW)
        engine = RiskEngine(RiskConfig(max_daily_loss_fraction=.03))
        self.assertIn("maximum daily loss reached", engine.evaluate(setup(), portfolio.snapshot()).reasons)
        duplicate_portfolio = Portfolio(10_000)
        duplicate_portfolio.open_position("ABC", Direction.LONG, 1, 100, NOW)
        self.assertIn("duplicate symbol position prohibited", engine.evaluate(setup(), duplicate_portfolio.snapshot()).reasons)

    def test_insufficient_capital_and_kill_switch(self):
        engine = RiskEngine()
        self.assertIn("insufficient available capital or position capacity", engine.evaluate(setup(), Portfolio(1).snapshot()).reasons)
        engine.set_kill_switch(True)
        self.assertIn("kill switch enabled", engine.evaluate(setup(), Portfolio(100_000).snapshot()).reasons)

    def test_invalid_prices_and_stop(self):
        risk = RiskEngine()
        portfolio = Portfolio().snapshot()
        invalid = replace(setup(), entry_price=0)
        wrong_stop = replace(setup(), stop_price=105)
        self.assertTrue(risk.evaluate(invalid, portfolio).reasons)
        self.assertIn("stop is on the wrong side of entry", risk.evaluate(wrong_stop, portfolio).reasons)

    def test_deterministic_sizing(self):
        risk = RiskEngine()
        portfolio = Portfolio(100_000).snapshot()
        self.assertEqual(risk.evaluate(setup(), portfolio), risk.evaluate(setup(), portfolio))


if __name__ == "__main__":
    unittest.main()
