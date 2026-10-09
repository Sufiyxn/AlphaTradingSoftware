"""Phase 8 proof: RiskEngine is the deterministic enforcement boundary.

Every production trading decision is validated against portfolio, exposure,
sizing, loss, duplicate-symbol, price, reward:risk, and operational safety
constraints before execution. These tests exercise the real RiskEngine,
Portfolio, RiskConfig loader, TradingPipeline, PaperTradingEngine, and
SQLite journal - not string mocks.

Covered: position sizing, max trade risk, reward:risk (2:1 floor), price
sanity, exposure limits, duplicate-symbol policy, daily loss limit (with the
real journal's restart persistence), max open positions, kill switch,
market-data freshness states, configuration validation, pipeline integration
(one risk call, no bypass, gate/connection still enforced), and auditability.
"""
import pathlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from backend.config import load_config
from backend.connections import (
    ConnectionManager,
    ExecutionProviderDescriptor,
    default_execution_providers,
)
from backend.database import SQLiteJournalRepository
from backend.execution import ExecutionMode, ExecutionSafetyGate, MockTradingPlatformAdapter
from backend.paper_trading import PaperTradeRecord, PaperTradingEngine, reconcile_journal
from backend.pipeline import ExecutionIntent, PipelineCode, TradingPipeline
from backend.portfolio import Portfolio
from backend.portfolio.models import PortfolioSnapshot, Position, PriceState
from backend.risk import (
    ALL_RISK_CODES,
    RiskCode,
    RiskConfig,
    RiskDecision,
    RiskEngine,
    RiskStatus,
)
from backend.risk.sizing import size_position, sizing_terms
from backend.strategies.models import Direction, TradeSetup

NOW = datetime(2024, 1, 8, 15, tzinfo=timezone.utc)
CLOCK = lambda: NOW  # noqa: E731 - deterministic fixture clock


def trade_setup(symbol="ABC", ratio=2.0, direction=Direction.LONG,
                entry=100.0, stop=None, target=None):
    sign = 1 if direction is Direction.LONG else -1
    risk = 5.0 if stop is None else abs(entry - stop)
    stop = entry - sign * risk if stop is None else stop
    if target is None:
        target = entry + sign * risk * ratio
    reward = abs(target - entry)
    return TradeSetup(
        symbol, direction, entry, stop, target, risk, reward,
        (reward / risk) if risk > 0 else 0.0, NOW, "phase8-fixture", 0.5, "test")


def snapshot(*, cash=100_000.0, equity=None, exposure=0.0, daily_pnl=0.0):
    return PortfolioSnapshot(
        cash=cash,
        equity=cash if equity is None else equity,
        available_capital=max(0.0, cash),
        exposure=exposure,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        daily_pnl=daily_pnl,
        trade_count=0,
        positions=(),
        valuation_status="COMPLETE",
        priced_position_count=0,
        unpriced_position_count=0,
    )


def open_position(portfolio, symbol, direction=Direction.LONG, quantity=1,
                  price=10.0, at=NOW):
    return portfolio.open_position(symbol, direction, quantity, price, at)


class PositionSizingTests(unittest.TestCase):
    def test_documented_sizing_formula_is_deterministic(self):
        # risk_budget = min(100k*1%, 100k*1%) = 1000; distance 5 -> 200 shares
        # exposure room 50k -> 500; cash -> 1000; notional 25k -> 250;
        # symbol room 25k -> 250; max quantity 10000.  min = 200.
        decision = RiskEngine().evaluate(trade_setup(), snapshot())
        self.assertTrue(decision.approved, decision.reasons)
        self.assertEqual(decision.quantity, 200)
        self.assertEqual(decision.risk_amount, 5.0 * 200)
        self.assertEqual(decision.current_exposure, 0.0)
        self.assertEqual(decision.projected_exposure, 20_000.0)
        again = RiskEngine().evaluate(trade_setup(), snapshot())
        self.assertEqual(decision.quantity, again.quantity)

    def test_requested_quantity_is_only_ever_reduced(self):
        engine = RiskEngine()
        small = engine.evaluate(trade_setup(), snapshot(), requested_quantity=50)
        self.assertTrue(small.approved, small.reasons)
        self.assertEqual(small.quantity, 50)
        self.assertEqual(small.requested_quantity, 50)
        self.assertEqual(small.risk_amount, 5.0 * 50)
        big = engine.evaluate(trade_setup(), snapshot(), requested_quantity=5_000)
        self.assertEqual(big.requested_quantity, 5_000)
        self.assertEqual(big.quantity, 200, "safe quantity caps an oversized request")
        self.assertLessEqual(big.quantity, 5_000)

    def test_configured_maximum_quantity_caps_the_order(self):
        decision = RiskEngine(RiskConfig(max_quantity=25)).evaluate(
            trade_setup(), snapshot())
        self.assertTrue(decision.approved, decision.reasons)
        self.assertEqual(decision.quantity, 25)

    def test_sizing_terms_are_share_counts_and_the_budget_stays_dollars(self):
        # The dollar risk budget never leaves this function as a "budget":
        # sizing_terms divides it by the stop distance first, so every value
        # it returns is a whole-share count under a quantity-named key.
        config = RiskConfig()
        snap = snapshot()
        terms = sizing_terms(100.0, 95.0, snap, config,
                             symbol="ABC", direction=Direction.LONG)
        self.assertEqual(set(terms), {"risk_quantity", "portfolio_exposure",
                                      "cash", "position_notional",
                                      "max_quantity", "symbol_exposure"})
        budget_dollars = min(snap.available_capital * config.risk_fraction,
                             snap.equity * config.max_risk_per_trade)
        self.assertEqual(budget_dollars, 1_000.00)
        self.assertEqual(terms["risk_quantity"],
                         int(budget_dollars // 5.0))
        self.assertEqual(terms["risk_quantity"], 200, "shares, not dollars")
        self.assertEqual(size_position(100.0, 95.0, snap, config,
                                       symbol="ABC", direction=Direction.LONG),
                         200)

    def test_one_hundred_dollar_stock_worked_example(self):
        engine = RiskEngine()
        config = RiskConfig()
        budget = min(100_000.0 * config.risk_fraction,
                     100_000.0 * config.max_risk_per_trade)
        self.assertEqual(budget, 1_000.00)                  # DOLLARS
        self.assertEqual(abs(100.0 - 95.0), 5.00)           # $/share stop
        self.assertEqual(int(budget // 5.00), 200)          # SHARES

        decision = engine.evaluate(trade_setup(), snapshot())
        self.assertTrue(decision.approved, decision.reasons)
        self.assertEqual(decision.quantity, 200)            # shares
        self.assertEqual(decision.risk_amount, 1_000.00)    # dollars 5.00 x 200
        self.assertEqual(decision.projected_exposure, 20_000.00)  # 200 x 100.00

        self.assertEqual(engine.evaluate(
            trade_setup(), snapshot(), requested_quantity=50).quantity, 50)
        self.assertEqual(engine.evaluate(
            trade_setup(), snapshot(), requested_quantity=500).quantity, 200)
        self.assertEqual(engine.evaluate(
            trade_setup(), snapshot(), requested_quantity=None).quantity, 200)

        smaller = engine.evaluate(trade_setup(), snapshot(cash=20_000.0))
        self.assertTrue(smaller.approved, smaller.reasons)
        self.assertEqual(smaller.quantity, 40)              # 200.00 / 5.00
        self.assertEqual(smaller.risk_amount, 200.00)

    def test_zero_safe_quantity_rejects(self):
        decision = RiskEngine().evaluate(trade_setup(), snapshot(cash=1.0))
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.quantity, 0)
        self.assertEqual(decision.code, RiskCode.ZERO_SAFE_QUANTITY)
        self.assertIn("insufficient available capital or position capacity",
                      decision.reasons)
        self.assertIn("position sizing found no room inside the risk budget",
                      decision.reasons)

    def test_invalid_requested_quantity_is_rejected(self):
        decision = RiskEngine().evaluate(
            trade_setup(), snapshot(), requested_quantity=-3)
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.code, RiskCode.INVALID_REQUEST)
        self.assertEqual(decision.quantity, 0)


class MaxTradeRiskTests(unittest.TestCase):
    def test_valid_risk_is_approved_and_recorded(self):
        decision = RiskEngine().evaluate(trade_setup(), snapshot())
        self.assertTrue(decision.approved)
        self.assertEqual(decision.risk_per_unit, 5.0)
        self.assertEqual(decision.reward_per_unit, 10.0)
        self.assertEqual(decision.risk_amount, decision.quantity * 5.0)

    def test_excessive_risk_falls_to_zero_and_rejects(self):
        # A 4.0 risk budget cannot afford even one 5.0-risk share.
        decision = RiskEngine(RiskConfig(max_risk_amount=4.0)).evaluate(
            trade_setup(), snapshot())
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.code, RiskCode.ZERO_SAFE_QUANTITY)
        self.assertEqual(decision.quantity, 0)
        self.assertEqual(decision.risk_amount, 0.0)
        self.assertIn("position sizing found no room inside the risk budget",
                      decision.reasons)

    def test_missing_stop_rejected_without_inventing_one(self):
        for missing in (None, 0, -5):
            with self.subTest(stop=missing):
                decision = RiskEngine().evaluate(
                    replace(trade_setup(), stop_price=missing), snapshot())
                self.assertEqual(decision.status, RiskStatus.REJECTED)
                self.assertEqual(decision.code, RiskCode.INVALID_PRICE)
                self.assertIn("entry, stop, and target must be positive finite prices",
                              decision.reasons)

    def test_invalid_stop_prices_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(stop=bad):
                decision = RiskEngine().evaluate(
                    replace(trade_setup(), stop_price=bad), snapshot())
                self.assertEqual(decision.status, RiskStatus.REJECTED)
                self.assertEqual(decision.code, RiskCode.INVALID_PRICE)

    def test_zero_risk_rejected(self):
        decision = RiskEngine().evaluate(
            replace(trade_setup(), stop_price=100.0), snapshot())
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertIn("trade risk per share must be positive", decision.reasons)
        self.assertIn("stop is on the wrong side of entry", decision.reasons)

    def test_per_share_budget_guard_runs(self):
        decision = RiskEngine().evaluate(trade_setup(), snapshot(cash=10.0))
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertIn("risk per share exceeds configured per-trade risk limit",
                      decision.reasons)


class RewardRiskTests(unittest.TestCase):
    def test_exactly_two_to_one_accepted(self):
        decision = RiskEngine().evaluate(trade_setup(ratio=2.0), snapshot())
        self.assertTrue(decision.approved, decision.reasons)
        self.assertEqual(decision.reward_risk, 2.0)

    def test_above_two_to_one_accepted(self):
        decision = RiskEngine().evaluate(trade_setup(ratio=2.5), snapshot())
        self.assertTrue(decision.approved, decision.reasons)
        self.assertGreaterEqual(decision.reward_risk, 2.0)

    def test_below_two_to_one_rejected(self):
        decision = RiskEngine().evaluate(trade_setup(ratio=1.99), snapshot())
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.code, RiskCode.REWARD_RISK_TOO_LOW)
        self.assertIn("reward/risk is below minimum", decision.reasons)

    def test_missing_or_equal_target_rejected(self):
        decision = RiskEngine().evaluate(
            replace(trade_setup(), target_price=100.0), snapshot())
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.code, RiskCode.TARGET_SIDE_INVALID)
        self.assertIn("target is on the wrong side of entry", decision.reasons)

    def test_long_geometry_stop_and_target(self):
        wrong_stop = RiskEngine().evaluate(
            replace(trade_setup(), stop_price=101.0), snapshot())
        self.assertEqual(wrong_stop.code, RiskCode.STOP_SIDE_INVALID)
        wrong_target = RiskEngine().evaluate(
            replace(trade_setup(), target_price=99.0), snapshot())
        self.assertEqual(wrong_target.code, RiskCode.TARGET_SIDE_INVALID)

    def test_short_geometry_and_ratio(self):
        valid = RiskEngine().evaluate(
            trade_setup(direction=Direction.SHORT, ratio=2.0), snapshot())
        self.assertTrue(valid.approved, valid.reasons)
        self.assertEqual(valid.risk_per_unit, 5.0)     # stop 105 - entry 100
        self.assertEqual(valid.reward_per_unit, 10.0)  # entry 100 - target 90
        self.assertEqual(valid.reward_risk, 2.0)
        wrong_stop = RiskEngine().evaluate(
            trade_setup(direction=Direction.SHORT, ratio=2.0, stop=95.0),
            snapshot())
        self.assertEqual(wrong_stop.code, RiskCode.STOP_SIDE_INVALID)
        wrong_target = RiskEngine().evaluate(
            replace(trade_setup(direction=Direction.SHORT, ratio=2.0),
                    target_price=101.0), snapshot())
        self.assertEqual(wrong_target.code, RiskCode.TARGET_SIDE_INVALID)

    def test_minimum_reward_risk_cannot_be_lowered_by_configuration(self):
        with self.assertRaises(ValueError):
            RiskConfig(min_reward_risk=1.99)
        with self.assertRaises(ValueError):
            load_config(env={"RISK_MIN_REWARD_RISK": "1.5"})


class PriceSanityTests(unittest.TestCase):
    def test_zero_negative_nan_infinite_prices_rejected(self):
        cases = {
            "zero entry": dict(entry_price=0.0),
            "negative entry": dict(entry_price=-1.0),
            "zero stop": dict(stop_price=0.0),
            "negative target": dict(target_price=-110.0),
            "nan entry": dict(entry_price=float("nan")),
            "nan target": dict(target_price=float("nan")),
            "infinite stop": dict(stop_price=float("inf")),
        }
        for label, fields in cases.items():
            with self.subTest(label=label):
                decision = RiskEngine().evaluate(
                    replace(trade_setup(), **fields), snapshot())
                self.assertEqual(decision.status, RiskStatus.REJECTED)
                self.assertEqual(decision.code, RiskCode.INVALID_PRICE)

    def test_long_ordering_stop_entry_target(self):
        # stop < entry < target is the only valid long geometry
        bad = RiskEngine().evaluate(
            replace(trade_setup(), stop_price=100.5), snapshot())
        self.assertEqual(bad.code, RiskCode.STOP_SIDE_INVALID)
        bad = RiskEngine().evaluate(
            replace(trade_setup(), target_price=99.5), snapshot())
        self.assertEqual(bad.code, RiskCode.TARGET_SIDE_INVALID)

    def test_short_ordering_target_entry_stop(self):
        # target < entry < stop is the only valid short geometry
        bad = RiskEngine().evaluate(
            replace(trade_setup(direction=Direction.SHORT), stop_price=99.5),
            snapshot())
        self.assertEqual(bad.code, RiskCode.STOP_SIDE_INVALID)
        bad = RiskEngine().evaluate(
            replace(trade_setup(direction=Direction.SHORT), target_price=100.5),
            snapshot())
        self.assertEqual(bad.code, RiskCode.TARGET_SIDE_INVALID)

    def test_entry_above_configured_ceiling_rejected(self):
        setup = trade_setup(entry=150.0, stop=145.0, target=160.0)
        decision = RiskEngine().evaluate(setup, snapshot())
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.code, RiskCode.ENTRY_PRICE_LIMIT)
        self.assertIn("entry price 150 exceeds the configured maximum 100",
                      decision.reasons)

    def test_entry_at_configured_ceiling_allowed(self):
        decision = RiskEngine().evaluate(
            trade_setup(entry=100.0, stop=95.0), snapshot())
        self.assertTrue(decision.approved, decision.reasons)


class ExposureLimitTests(unittest.TestCase):
    def test_below_limit_approved(self):
        decision = RiskEngine().evaluate(trade_setup(), snapshot(cash=100_000))
        self.assertTrue(decision.approved)
        self.assertLess(decision.projected_exposure, 50_000)

    def test_exactly_at_limit_approved(self):
        # equity 50k, limits 5%/5%/50%: safe = 250 shares = 25k notional
        # which lands exactly on the 50% exposure ceiling (breach is ">").
        engine = RiskEngine(RiskConfig(risk_fraction=0.05, max_risk_per_trade=0.05))
        decision = engine.evaluate(trade_setup(), snapshot(cash=50_000))
        self.assertTrue(decision.approved, decision.reasons)
        self.assertEqual(decision.projected_exposure, 25_000.0)
        self.assertEqual(decision.projected_exposure, decision.equity * 0.5)

    def test_over_limit_portfolio_rejected_with_named_rule(self):
        decision = RiskEngine(RiskConfig(max_portfolio_exposure_fraction=0.5)).evaluate(
            trade_setup(), snapshot(cash=4_000, equity=4_000, exposure=6_000))
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.code, RiskCode.ZERO_SAFE_QUANTITY)
        self.assertIn("no capacity remains under the portfolio exposure limit",
                      decision.reasons)
        self.assertIn("maximum portfolio exposure exceeded", decision.reasons)

    def test_existing_position_is_counted_once(self):
        portfolio = Portfolio(100_000)
        open_position(portfolio, "OTHER", quantity=100, price=100.0)
        decision = RiskEngine().evaluate(trade_setup(), portfolio.snapshot())
        self.assertTrue(decision.approved, decision.reasons)
        self.assertEqual(decision.current_exposure, 10_000.0)
        self.assertEqual(decision.projected_exposure,
                         10_000.0 + decision.quantity * 100.0)
        self.assertEqual(decision.quantity, 180)

    def test_symbol_exposure_limit_is_enforced(self):
        portfolio = Portfolio(100_000)
        open_position(portfolio, "ABC", quantity=100, price=100.0)
        config = RiskConfig(allow_duplicate_symbol=True, max_symbol_exposure=10_000.0)
        decision = RiskEngine(config).evaluate(trade_setup(), portfolio.snapshot())
        # The held 10k already sits at the 10k symbol ceiling, so sizing has
        # no room left for a single share.
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.quantity, 0)
        self.assertIn("insufficient available capital or position capacity",
                      decision.reasons)
        self.assertIn("no capacity remains under the symbol exposure limit",
                      decision.reasons)


class DuplicateSymbolTests(unittest.TestCase):
    def test_existing_position_rejected(self):
        portfolio = Portfolio(100_000)
        open_position(portfolio, "ABC", quantity=10, price=100.0)
        decision = RiskEngine().evaluate(trade_setup(), portfolio.snapshot())
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.code, RiskCode.DUPLICATE_SYMBOL)
        self.assertIn("duplicate symbol position prohibited", decision.reasons)
        self.assertTrue(decision.symbol_open)

    def test_no_existing_position_accepted(self):
        decision = RiskEngine().evaluate(trade_setup(), snapshot())
        self.assertTrue(decision.approved)
        self.assertFalse(decision.symbol_open)

    def test_historical_closed_position_does_not_block(self):
        portfolio = Portfolio(100_000)
        open_position(portfolio, "ABC", quantity=10, price=100.0)
        portfolio.close_position("ABC", price=105.0, timestamp=NOW)
        decision = RiskEngine().evaluate(trade_setup(), portfolio.snapshot())
        self.assertTrue(decision.approved, decision.reasons)

    def test_duplicates_allowed_by_configuration_land_on_symbol_ceiling(self):
        portfolio = Portfolio(100_000)
        open_position(portfolio, "ABC", quantity=100, price=100.0)
        config = RiskConfig(allow_duplicate_symbol=True)
        decision = RiskEngine(config).evaluate(trade_setup(), portfolio.snapshot())
        self.assertTrue(decision.approved, decision.reasons)
        # held 10k + 15k of new notional == the 25k symbol ceiling exactly.
        self.assertEqual(decision.quantity, 150)
        self.assertEqual(decision.projected_exposure, 25_000.0)

    def test_duplicate_policy_is_configurable_but_not_the_default(self):
        self.assertFalse(RiskConfig().allow_duplicate_symbol)
        self.assertTrue(
            RiskConfig(allow_duplicate_symbol=True).allow_duplicate_symbol)


class DailyLossLimitTests(unittest.TestCase):
    def test_below_limit_approved(self):
        decision = RiskEngine().evaluate(
            trade_setup(), snapshot(cash=10_000, daily_pnl=-299.0))
        self.assertTrue(decision.approved, decision.reasons)

    def test_exactly_at_limit_rejected(self):
        decision = RiskEngine().evaluate(
            trade_setup(), snapshot(cash=10_000, daily_pnl=-300.0))
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.code, RiskCode.DAILY_LOSS_LIMIT)
        self.assertIn("maximum daily loss reached", decision.reasons)
        self.assertEqual(decision.daily_pnl, -300.0)

    def test_limit_exceeded_rejected(self):
        decision = RiskEngine().evaluate(
            trade_setup(), snapshot(cash=10_000, daily_pnl=-350.0))
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.code, RiskCode.DAILY_LOSS_LIMIT)

    def test_daily_window_rolls_on_utc_date_boundary(self):
        engine = RiskEngine()
        day_one = snapshot(cash=10_000, daily_pnl=-350.0)
        self.assertEqual(engine.evaluate(trade_setup(), day_one).code,
                         RiskCode.DAILY_LOSS_LIMIT)
        day_two = snapshot(cash=10_000, daily_pnl=0.0)
        self.assertTrue(engine.evaluate(trade_setup(), day_two).approved)

    def test_close_on_new_day_counts_only_the_new_day(self):
        # A close landing on a new UTC day must start that day's window:
        # previously the roll ran AFTER accumulation and wiped the loss.
        portfolio = Portfolio(1_000)
        open_position(portfolio, "A", quantity=1, price=10.0, at=NOW)
        portfolio.close_position("A", price=8.0, timestamp=NOW)
        self.assertEqual(portfolio.snapshot().daily_pnl, -2.0)
        next_day = NOW + timedelta(days=1)
        open_position(portfolio, "B", quantity=1, price=10.0, at=next_day)
        portfolio.close_position("B", price=9.0, timestamp=next_day)
        self.assertEqual(portfolio.snapshot().daily_pnl, -1.0)
        self.assertEqual(portfolio.snapshot().realized_pnl, -3.0)

    def test_close_only_on_new_day_still_counts(self):
        # Position opened day one, closed day two with no day-two open.
        portfolio = Portfolio(1_000)
        open_position(portfolio, "A", quantity=1, price=10.0, at=NOW)
        next_day = NOW + timedelta(days=1)
        portfolio.close_position("A", price=9.0, timestamp=next_day)
        self.assertEqual(portfolio.snapshot().daily_pnl, -1.0,
                         "the new day's loss must not be wiped by the day roll")
        self.assertEqual(portfolio.snapshot().realized_pnl, -1.0)

    def test_restart_preserves_daily_loss_through_the_real_journal(self):
        for exit_at, expected_daily, expected_approved in (
                (NOW, -300.0, False),                 # same UTC day: state persists
                (NOW - timedelta(days=1), 0.0, True),  # yesterday: new window
        ):
            with self.subTest(exit_at=exit_at.date()):
                repository = SQLiteJournalRepository(":memory:")
                portfolio = Portfolio(10_000, clock=CLOCK)
                risk = RiskEngine(clock=CLOCK)
                engine = PaperTradingEngine(risk, portfolio, clock=CLOCK)
                record = PaperTradeRecord("ABC", Direction.LONG, NOW, 100.0,
                                          95.0, 110.0, 10, "fixture", "T-1")
                setup = trade_setup()
                try:
                    repository.record_trade_open(record, setup=setup, risk=None,
                                                 analysis=None)
                    repository.record_trade_close("T-1", exit_at, 97.0, -300.0)
                    portfolio = Portfolio(10_000, clock=CLOCK)
                    reconcile_journal(portfolio=portfolio, engine=engine,
                                      repository=repository,
                                      starting_cash=10_000, clock=CLOCK)
                    self.assertEqual(portfolio.snapshot().daily_pnl, expected_daily)
                    self.assertEqual(portfolio.snapshot().realized_pnl, -300.0)
                    decision = RiskEngine(clock=CLOCK).evaluate(
                        setup, portfolio.snapshot())
                    self.assertEqual(decision.approved, expected_approved,
                                     (decision.code, decision.reasons))
                finally:
                    repository.close()


class MaxOpenPositionTests(unittest.TestCase):
    def _portfolio_with(self, count):
        portfolio = Portfolio(1_000_000)
        for index in range(count):
            open_position(portfolio, f"S{index}", quantity=1, price=10.0)
        return portfolio

    def test_below_limit_accepted(self):
        decision = RiskEngine().evaluate(
            trade_setup(), self._portfolio_with(4).snapshot())
        self.assertTrue(decision.approved, decision.reasons)

    def test_at_limit_rejected(self):
        decision = RiskEngine().evaluate(
            trade_setup(), self._portfolio_with(5).snapshot())
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.code, RiskCode.MAX_POSITIONS_REACHED)
        self.assertIn("maximum simultaneous positions reached", decision.reasons)

    def test_above_limit_rejected(self):
        decision = RiskEngine().evaluate(
            trade_setup(), self._portfolio_with(6).snapshot())
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertIn("maximum simultaneous positions reached", decision.reasons)


class KillSwitchTests(unittest.TestCase):
    def test_active_kill_switch_rejects_before_anything_else(self):
        engine = RiskEngine()
        engine.set_kill_switch(True)
        decision = engine.evaluate(trade_setup(), snapshot())
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.code, RiskCode.KILL_SWITCH)
        self.assertEqual(decision.reasons[0], "kill switch enabled")
        self.assertEqual(decision.quantity, 0)

    def test_inactive_kill_switch_is_transparent(self):
        engine = RiskEngine()
        engine.set_kill_switch(False)
        decision = engine.evaluate(trade_setup(), snapshot())
        self.assertTrue(decision.approved, decision.reasons)
        self.assertEqual(decision.code, RiskCode.APPROVED)

    def test_disabled_engine_rejects(self):
        decision = RiskEngine(RiskConfig(enabled=False)).evaluate(
            trade_setup(), snapshot())
        self.assertEqual(decision.code, RiskCode.RISK_DISABLED)


class MarketDataFreshnessTests(unittest.TestCase):
    def _with_position(self, *, market_price, price_as_of, price_state):
        position = Position("ABC", Direction.LONG, 100, 100.0, market_price,
                            NOW, price_as_of=price_as_of, price_state=price_state)
        portfolio = Portfolio(100_000)
        portfolio._positions["ABC"] = position  # canonical store, no cash movement
        return portfolio

    def test_fresh_mark_satisfies_the_freshness_policy(self):
        portfolio = self._with_position(
            market_price=101.0, price_as_of=NOW, price_state=PriceState.FRESH)
        config = RiskConfig(require_fresh_valuation=True)
        decision = RiskEngine(config).evaluate(
            trade_setup(symbol="XYZ"), portfolio.snapshot())
        self.assertTrue(decision.approved, decision.reasons)

    def test_stale_mark_blocks_when_policy_requires_freshness(self):
        portfolio = self._with_position(
            market_price=101.0, price_as_of=NOW - timedelta(minutes=30),
            price_state=PriceState.STALE)
        config = RiskConfig(require_fresh_valuation=True)
        decision = RiskEngine(config).evaluate(
            trade_setup(symbol="XYZ"), portfolio.snapshot())
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.code, RiskCode.VALUATION_NOT_FRESH)
        self.assertIn(
            "portfolio valuation is not fresh: at least one open position "
            "has no fresh real quote", decision.reasons)

    def test_unpriced_position_blocks_when_policy_requires_freshness(self):
        portfolio = self._with_position(
            market_price=None, price_as_of=None, price_state=PriceState.UNPRICED)
        strict = RiskEngine(RiskConfig(require_fresh_valuation=True))
        self.assertEqual(strict.evaluate(trade_setup(symbol="XYZ"),
                                         portfolio.snapshot()).code,
                         RiskCode.VALUATION_NOT_FRESH)
        # Default policy: an unpriced position contributes no market value to
        # equity (never a fabricated price) but does not block new entries.
        relaxed = RiskEngine()
        decision = relaxed.evaluate(trade_setup(symbol="XYZ"), portfolio.snapshot())
        self.assertTrue(decision.approved, decision.reasons)
        self.assertEqual(decision.equity, 100_000.0,
                         "unpriced shares must not add invented value to equity")

    def test_equity_excludes_unpriced_position_value(self):
        portfolio = Portfolio(50_000)
        open_position(portfolio, "ABC", quantity=100, price=100.0)
        marked = portfolio.snapshot()
        self.assertEqual(marked.valuation_status, "PARTIAL")
        decision = RiskEngine().evaluate(trade_setup(symbol="XYZ"), marked)
        self.assertEqual(decision.equity, 40_000.0,
                         "cash only; the unpriced entry notional is not equity")

    def test_risk_uses_only_real_observed_marks(self):
        portfolio = Portfolio(50_000)
        open_position(portfolio, "ABC", quantity=100, price=100.0)
        portfolio.update_price("ABC", 110.0, timestamp=NOW, state=PriceState.FRESH)
        self.assertEqual(portfolio.snapshot().equity, 51_000.0)
        portfolio.update_price("ABC", 110.0,
                               timestamp=NOW - timedelta(minutes=10),
                               state=PriceState.STALE)
        decision = RiskEngine().evaluate(trade_setup(symbol="XYZ"),
                                         portfolio.snapshot())
        self.assertEqual(decision.equity, 51_000.0,
                         "a real (stale) mark stays a real mark; nothing is invented")


class RiskConfigurationTests(unittest.TestCase):
    def test_defaults_match_documented_shipped_values(self):
        config = RiskConfig()
        self.assertEqual(config.risk_fraction, 0.01)
        self.assertEqual(config.max_risk_per_trade, 0.01)
        self.assertEqual(config.max_position_notional, 25_000.0)
        self.assertEqual(config.max_portfolio_exposure_fraction, 0.50)
        self.assertEqual(config.max_simultaneous_positions, 5)
        self.assertEqual(config.max_daily_loss_fraction, 0.03)
        self.assertEqual(config.min_reward_risk, 2.0)
        self.assertFalse(config.allow_duplicate_symbol)
        self.assertEqual(config.max_quantity, 10_000)
        self.assertEqual(config.max_symbol_exposure, 25_000.0)
        self.assertEqual(config.max_entry_price, 100.0)
        self.assertFalse(config.require_fresh_valuation)

    def test_invalid_configuration_fails_safely(self):
        for kwargs in (
            {"max_quantity": 0},
            {"max_quantity": 100_001},
            {"max_symbol_exposure": 0.0},
            {"max_entry_price": 0.0},
            {"max_entry_price": 101.0},
            {"risk_fraction": 0.5},
            {"max_daily_loss_fraction": 0.5},
            {"min_reward_risk": 1.5},
            {"max_simultaneous_positions": 51},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    RiskConfig(**kwargs)

    def test_new_environment_variables_parse(self):
        config = load_config(env={
            "RISK_MAX_QUANTITY": "25",
            "RISK_MAX_SYMBOL_EXPOSURE": "12345",
            "RISK_MAX_ENTRY_PRICE": "80",
            "RISK_REQUIRE_FRESH_VALUATION": "true",
        }).risk
        self.assertEqual(config.max_quantity, 25)
        self.assertEqual(config.max_symbol_exposure, 12345.0)
        self.assertEqual(config.max_entry_price, 80)
        self.assertTrue(config.require_fresh_valuation)

    def test_invalid_environment_fails_at_startup(self):
        for env in (
            {"RISK_MAX_QUANTITY": "abc"},
            {"RISK_MAX_QUANTITY": "0"},
            {"RISK_MAX_QUANTITY": "1000000"},
            {"RISK_MAX_SYMBOL_EXPOSURE": "-1"},
            {"RISK_MAX_ENTRY_PRICE": "0"},
        ):
            with self.subTest(env=env):
                with self.assertRaises(ValueError):
                    load_config(env=env)

    def test_risk_can_only_tighten_the_candidate_price_limit(self):
        self.assertEqual(
            load_config(env={"MAX_CANDIDATE_PRICE": "50",
                             "RISK_MAX_ENTRY_PRICE": "80"}
                        ).risk.max_entry_price, 50.0)
        self.assertEqual(
            load_config(env={"MAX_CANDIDATE_PRICE": "50",
                             "RISK_MAX_ENTRY_PRICE": "30"}
                        ).risk.max_entry_price, 30.0)

    def test_configuration_describe_exposes_the_new_limits(self):
        described = load_config(env={}).describe()["risk"]
        for key in ("max_quantity", "max_symbol_exposure", "max_entry_price",
                    "require_fresh_valuation", "max_risk_amount"):
            self.assertIn(key, described)

    def test_limits_report_carries_no_secret_shaped_keys(self):
        for key, value in RiskConfig().limits:
            lowered = str(key).lower()
            for secret_word in ("secret", "token", "password", "key"):
                self.assertNotIn(secret_word, lowered)
            self.assertIsInstance(value, (int, float, bool))


class DecisionAuditabilityTests(unittest.TestCase):
    def test_approved_decision_exposes_every_audit_field(self):
        engine = RiskEngine(RiskConfig(max_quantity=500), clock=CLOCK)
        decision = engine.evaluate(
            trade_setup(), snapshot(cash=100_000, daily_pnl=-1.0),
            requested_quantity=75)
        self.assertTrue(decision.approved, decision.reasons)
        self.assertEqual(decision.code, RiskCode.APPROVED)
        self.assertEqual(decision.requested_quantity, 75)
        self.assertEqual(decision.quantity, 75)
        self.assertEqual(decision.entry_price, 100.0)
        self.assertEqual(decision.stop_price, 95.0)
        self.assertEqual(decision.target_price, 110.0)
        self.assertEqual(decision.risk_per_unit, 5.0)
        self.assertEqual(decision.reward_per_unit, 10.0)
        self.assertEqual(decision.reward_risk, 2.0)
        self.assertEqual(decision.current_exposure, 0.0)
        self.assertEqual(decision.projected_exposure, 7_500.0)
        self.assertEqual(decision.daily_pnl, -1.0)
        self.assertEqual(decision.equity, 100_000.0)
        self.assertFalse(decision.symbol_open)
        self.assertEqual(decision.decided_at, NOW)
        keys = {key for key, _ in decision.limits}
        self.assertTrue({"risk_fraction", "min_reward_risk", "max_quantity",
                         "max_symbol_exposure", "max_entry_price",
                         "allow_duplicate_symbol"} <= keys)

    def test_rejected_decision_names_the_first_rule_and_zeroes_quantity(self):
        portfolio = Portfolio(100_000)
        open_position(portfolio, "ABC", quantity=1, price=100.0)
        decision = RiskEngine(clock=CLOCK).evaluate(
            trade_setup(), portfolio.snapshot())
        self.assertEqual(decision.status, RiskStatus.REJECTED)
        self.assertEqual(decision.code, RiskCode.DUPLICATE_SYMBOL)
        self.assertEqual(decision.quantity, 0)
        self.assertEqual(decision.risk_amount, 0.0)
        self.assertEqual(decision.setup.symbol, "ABC")

    def test_every_documented_code_is_declared_exactly_once(self):
        self.assertEqual(len(set(ALL_RISK_CODES)), len(ALL_RISK_CODES))
        self.assertIn(RiskCode.APPROVED, ALL_RISK_CODES)
        self.assertIn(RiskCode.KILL_SWITCH, ALL_RISK_CODES)
        self.assertIn(RiskCode.SYMBOL_EXPOSURE_EXCEEDED, ALL_RISK_CODES)

    def test_manual_fixture_construction_still_works(self):
        # The five-positional form used across the journal/reporting suites.
        decision = RiskDecision(RiskStatus.REJECTED, trade_setup(), 0, 0.0,
                                ("risk limit",))
        self.assertFalse(decision.approved)
        self.assertEqual(decision.quantity, 0)


class PipelineIntegrationTests(unittest.TestCase):
    class SpyRisk(RiskEngine):
        def __init__(self, config=None, *, trail=None):
            super().__init__(config, clock=CLOCK)
            self.calls = []
            self.trail = trail if trail is not None else []

        def evaluate(self, proposed, portfolio, *, requested_quantity=None):
            self.calls.append((proposed.symbol, requested_quantity))
            self.trail.append("risk")
            return super().evaluate(proposed, portfolio,
                                    requested_quantity=requested_quantity)

    class SpyAdapter(MockTradingPlatformAdapter):
        def __init__(self, *, trail=None, **kwargs):
            super().__init__(**kwargs)
            self.trail = trail if trail is not None else []
            self.requests = []

        def place_order(self, request):
            self.requests.append(request)
            self.trail.append("adapter")
            return super().place_order(request)

    class RecordingGate(ExecutionSafetyGate):
        def __init__(self, *, trail=None, **kwargs):
            super().__init__(**kwargs)
            self.trail = trail if trail is not None else []

        def authorize(self, mode):
            self.trail.append("gate")
            return super().authorize(mode)

    def _build(self, *, trail, config=None, journal=None):
        """Wire one pipeline; the trail is cleared once setup verification ends."""
        trail = [] if trail is None else trail
        risk = self.SpyRisk(config, trail=trail)
        adapter = self.SpyAdapter(trail=trail, clock=CLOCK)
        gate = self.RecordingGate(
            trail=trail, kill_switch=lambda: risk.kill_switch_enabled)
        registry = default_execution_providers()
        provider_id = str(adapter.identify() or "").strip().lower() or "local_paper"
        if registry.find(provider_id) is None:
            registry.register(ExecutionProviderDescriptor(
                provider_id=provider_id, label=f"Fixture {provider_id}",
                implemented=True, adapter_factory=lambda **_: adapter,
                authentication="NONE",
                supported_modes=(ExecutionMode.PAPER,)))
        manager = ConnectionManager(registry=registry, clock=CLOCK, gate=gate)
        manager.add(connection_id="paper", provider_id=provider_id,
                    label="Fixture connection", mode=ExecutionMode.PAPER,
                    adapter=adapter)
        pipeline = TradingPipeline(connections=manager, risk_engine=risk,
                                   portfolio=Portfolio(100_000), journal=journal,
                                   clock=CLOCK, connection_id="paper")
        trail.clear()  # registration verification noise is not the system under test
        return pipeline, manager, adapter, risk, trail

    def test_risk_is_called_exactly_once_with_the_requested_quantity(self):
        trail = []
        journal = SQLiteJournalRepository(":memory:")
        pipeline, _, adapter, risk, trail = self._build(trail=trail, journal=journal)
        try:
            intent = ExecutionIntent(setup=trade_setup(), source="phase8",
                                     requested_quantity=5)
            result = pipeline.execute(intent)
            self.assertEqual(result.code, PipelineCode.ACCEPTED)
            self.assertEqual(risk.calls, [("ABC", 5)])
            self.assertEqual(result.risk.quantity, 5)
            self.assertEqual(result.risk.requested_quantity, 5)
            self.assertEqual(len(adapter.requests), 1)
            self.assertEqual(trail.count("risk"), 1)
            self.assertEqual(trail.count("adapter"), 1)
            self.assertLess(trail.index("risk"), trail.index("adapter"))
        finally:
            journal.close()

    def test_phase8_rejection_never_reaches_the_adapter(self):
        trail = []
        journal = SQLiteJournalRepository(":memory:")
        pipeline, _, adapter, risk, trail = self._build(trail=trail, journal=journal)
        try:
            portfolio = Portfolio(100_000)
            open_position(portfolio, "ABC", quantity=10, price=100.0)
            pipeline._portfolio = portfolio  # canonical state with an open ABC
            result = pipeline.execute(ExecutionIntent(setup=trade_setup(),
                                                      source="phase8"))
            self.assertEqual(result.code, PipelineCode.RISK_REJECTED)
            self.assertEqual(result.risk.code, RiskCode.DUPLICATE_SYMBOL)
            self.assertEqual(adapter.requests, [], "risk rejection must be final")
            self.assertEqual(risk.calls, [("ABC", None)])
            self.assertEqual(trail.count("risk"), 1)
            self.assertNotIn("adapter", trail)
            rows = journal.rows("risk_decisions")
            self.assertEqual(len(rows), 1)
            stored = rows[0]["context_json"]
            self.assertEqual(stored["code"], RiskCode.DUPLICATE_SYMBOL)
            self.assertEqual(stored["quantity"], 0)
            events = [event for event in journal.events()
                      if event["event_type"] == "EXECUTION_REJECTED"]
            self.assertTrue(events)
            self.assertEqual(events[-1]["context_json"]["risk_code"],
                             RiskCode.DUPLICATE_SYMBOL)
            self.assertFalse(events[-1]["context_json"]["risk_approved"])
            self.assertEqual(events[-1]["context_json"]["risk_approved_quantity"], 0)
        finally:
            journal.close()

    def test_approved_risk_still_runs_gate_connection_and_capabilities(self):
        trail = []
        pipeline, manager, adapter, risk, trail = self._build(trail=trail)
        result = pipeline.execute(ExecutionIntent(setup=trade_setup(),
                                                  source="phase8"))
        self.assertEqual(result.code, PipelineCode.ACCEPTED)
        self.assertEqual(risk.calls, [("ABC", None)])
        self.assertEqual(trail.count("risk"), 1)
        self.assertEqual(trail.count("adapter"), 1)
        self.assertGreaterEqual(trail.count("gate"), 1)
        self.assertLess(trail.index("risk"), trail.index("adapter"))
        self.assertTrue(manager.readiness("paper").ready)
        self.assertEqual(result.connection_id, "paper")
        self.assertEqual(result.provider_id, adapter.identify())
        self.assertTrue(result.safety_approved)

    def test_preapproved_decision_is_verified_and_not_reevaluated(self):
        trail = []
        pipeline, _, adapter, risk, trail = self._build(trail=trail)
        approved = RiskEngine(clock=CLOCK).evaluate(
            trade_setup(), Portfolio(100_000).snapshot())
        self.assertTrue(approved.approved)
        result = pipeline.execute_decision(approved)
        self.assertEqual(result.code, PipelineCode.ACCEPTED)
        self.assertEqual(risk.calls, [], "a verified decision is never re-evaluated")
        self.assertEqual(len(adapter.requests), 1)

    def test_risk_package_is_provider_agnostic(self):
        backend = pathlib.Path(__file__).resolve().parents[1]
        forbidden = ("place_order", "connectionmanager", "tradingpipeline",
                     "alpaca", "robinhood", "api_key", "http://", "urllib")
        scanned = 0
        for path in sorted((backend / "risk").glob("*.py")):
            scanned += 1
            haystack = path.read_text(encoding="utf-8").lower()
            for token in forbidden:
                self.assertNotIn(token, haystack,
                                 f"{path.name} must stay provider-agnostic")
        self.assertGreaterEqual(scanned, 5)


class KillSwitchPipelineTests(unittest.TestCase):
    def test_kill_switch_blocks_the_order_end_to_end(self):
        journal = SQLiteJournalRepository(":memory:")
        risk = RiskEngine(clock=CLOCK)
        adapter = MockTradingPlatformAdapter(clock=CLOCK)
        portfolio = Portfolio(100_000, clock=CLOCK)
        engine = PaperTradingEngine(risk, portfolio, adapter, journal=journal,
                                    clock=CLOCK)
        try:
            risk.set_kill_switch(True)
            result = engine.submit(trade_setup(), source="phase8")
            self.assertEqual(result.code, PipelineCode.KILL_SWITCH)
            self.assertEqual(result.risk.code, RiskCode.KILL_SWITCH)
            self.assertEqual(len(adapter.get_orders()), 0)
            self.assertEqual(list(engine.trades), [])
            risk.set_kill_switch(False)
            clear = engine.submit(trade_setup(symbol="XYZ"), source="phase8")
            self.assertEqual(clear.code, PipelineCode.ACCEPTED)
            self.assertEqual(len(adapter.get_orders()), 1)
            self.assertEqual(len(engine.trades), 1)
        finally:
            journal.close()


if __name__ == "__main__":
    unittest.main()
