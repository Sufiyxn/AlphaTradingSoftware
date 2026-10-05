import unittest
from datetime import datetime, time, timedelta, timezone

from backend.backtesting import BacktestConfig, BacktestEngine
from backend.backtesting.sizing import position_size
from backend.backtesting.metrics import calculate_metrics
from backend.backtesting.models import SimulatedTrade
from backend.market_data import Bar
from backend.strategies.models import Direction, StrategySignal, TradeSetup
from backend.strategies.service import StrategyEngine


BASE = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)  # 09:30 New York


def bar(symbol: str, minute: int, open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(symbol, BASE + timedelta(minutes=minute), open_, high, low, close, 1000)


class FixedSetupStrategy:
    name = "deterministic_fixture"

    def __init__(self, direction=Direction.LONG, stop=95.0, target=110.0):
        self.direction, self.stop, self.target = direction, stop, target

    def evaluate(self, bars, config):
        latest = bars[-1]
        entry = latest.close
        risk, reward = abs(entry - self.stop), abs(self.target - entry)
        setup = TradeSetup(latest.symbol, self.direction, entry, self.stop, self.target, risk, reward,
                           reward / risk, latest.timestamp, self.name, 1.0, "fixture")
        return StrategySignal(latest.symbol, latest.timestamp, self.direction, setup, "fixture", self.name)


class SequenceStrategy:
    name = "sequence_fixture"

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = 0

    def evaluate(self, bars, config):
        latest = bars[-1]
        won = self.outcomes[self.calls]
        self.calls += 1
        stop, target = (95.0, 110.0) if won else (95.0, 109.0)
        setup = TradeSetup(latest.symbol, Direction.LONG, latest.close, stop, target,
                           latest.close - stop, target - latest.close,
                           (target - latest.close) / (latest.close - stop), latest.timestamp,
                           self.name, 1.0, "sequence fixture")
        return StrategySignal(latest.symbol, latest.timestamp, Direction.LONG, setup, "fixture", self.name)


class OneShotSequenceStrategy(SequenceStrategy):
    def __init__(self, outcomes):
        super().__init__(outcomes)
        self.fired = False

    def evaluate(self, bars, config):
        if self.fired:
            return StrategySignal(bars[-1].symbol, bars[-1].timestamp, Direction.NONE, None, "done", self.name)
        self.fired = True
        return super().evaluate(bars, config)


def engine(strategy=None, **config_values):
    defaults = dict(starting_capital=10_000, risk_fraction=.01, max_exposure_fraction=1.0,
                    max_simultaneous_positions=5, commission_per_share=0, slippage_per_share=0,
                    session_open=time(9, 30), session_close=time(16, 0))
    defaults.update(config_values)
    return BacktestEngine(strategy, BacktestConfig(**defaults))


class BacktestingTests(unittest.TestCase):
    def test_long_winner_take_profit_and_one_symbol(self):
        result = engine(FixedSetupStrategy()).run({"ABC": [bar("ABC", 0, 100, 101, 99, 100), bar("ABC", 1, 100, 101, 99, 100), bar("ABC", 2, 100, 101, 99, 100), bar("ABC", 3, 100, 111, 99, 109)]})
        self.assertEqual(result.metrics.total_trades, 1)
        self.assertTrue(result.trades[0].won)
        self.assertEqual(result.trades[0].exit_reason, "take_profit")

    def test_long_loser_stop_loss(self):
        result = engine(FixedSetupStrategy()).run({"ABC": [bar("ABC", 0, 100, 101, 99, 100), bar("ABC", 1, 100, 101, 99, 100), bar("ABC", 2, 100, 101, 99, 100), bar("ABC", 3, 100, 102, 94, 96)]})
        self.assertEqual(result.trades[0].exit_reason, "stop_loss")
        self.assertFalse(result.trades[0].won)

    def test_short_winner_and_loser(self):
        short = FixedSetupStrategy(Direction.SHORT, stop=105, target=90)
        win = engine(short).run({"ABC": [bar("ABC", 0, 100, 101, 99, 100), bar("ABC", 1, 100, 101, 99, 100), bar("ABC", 2, 100, 101, 99, 100), bar("ABC", 3, 100, 101, 89, 91)]})
        lose = engine(short).run({"ABC": [bar("ABC", 0, 100, 101, 99, 100), bar("ABC", 1, 100, 101, 99, 100), bar("ABC", 2, 100, 101, 99, 100), bar("ABC", 3, 100, 106, 99, 104)]})
        self.assertTrue(win.trades[0].won)
        self.assertEqual(win.trades[0].exit_reason, "take_profit")
        self.assertFalse(lose.trades[0].won)
        self.assertEqual(lose.trades[0].exit_reason, "stop_loss")

    def test_same_bar_stop_and_target_uses_stop_first(self):
        result = engine(FixedSetupStrategy()).run({"ABC": [bar("ABC", 0, 100, 101, 99, 100), bar("ABC", 1, 100, 101, 99, 100), bar("ABC", 2, 100, 101, 99, 100), bar("ABC", 3, 100, 111, 94, 100)]})
        self.assertIn("stop_first", result.trades[0].exit_reason)
        self.assertFalse(result.trades[0].won)

    def test_reward_risk_acceptance_and_rejection(self):
        ok = engine(FixedSetupStrategy(stop=95, target=110)).run({"ABC": [bar("ABC", 0, 100, 101, 99, 100), bar("ABC", 1, 100, 101, 99, 100), bar("ABC", 2, 100, 101, 99, 100), bar("ABC", 3, 100, 111, 99, 100)]})
        bad = engine(FixedSetupStrategy(stop=95, target=109)).run({"ABC": [bar("ABC", 0, 100, 101, 99, 100), bar("ABC", 1, 100, 101, 99, 100), bar("ABC", 2, 100, 101, 99, 100), bar("ABC", 3, 100, 111, 99, 100)]})
        self.assertEqual(ok.trades[0].reward_risk_ratio, 2.0)
        self.assertEqual(bad.trades, ())
        self.assertFalse(bad.qualified)

    def test_threshold_win_rates(self):
        def metrics_with_wins(wins, total):
            rows = tuple(SimulatedTrade("X", Direction.LONG, BASE, 100, 95, 110,
                                        BASE + timedelta(minutes=1), 110 if i < wins else 95,
                                        5, 10, 2, 10 if i < wins else -5,
                                        10 if i < wins else -5, i < wins, "fixture",
                                        "take_profit" if i < wins else "stop_loss", 1, 0)
                         for i in range(total))
            return calculate_metrics(rows, 10_000)

        self.assertAlmostEqual(metrics_with_wins(51, 100).win_rate, 51.0)
        self.assertAlmostEqual(metrics_with_wins(50, 100).win_rate, 50.0)
        self.assertAlmostEqual(metrics_with_wins(55, 100).win_rate, 55.0)

    def test_multiple_symbols_and_symbol_statistics(self):
        histories = {
            "AAA": [bar("AAA", 0, 100, 101, 99, 100), bar("AAA", 1, 100, 101, 99, 100), bar("AAA", 2, 100, 101, 99, 100), bar("AAA", 3, 100, 111, 99, 100)],
            "BBB": [bar("BBB", 0, 100, 101, 99, 100), bar("BBB", 1, 100, 101, 99, 100), bar("BBB", 2, 100, 101, 99, 100), bar("BBB", 3, 100, 102, 94, 96)],
        }
        result = engine(FixedSetupStrategy()).run(histories)
        self.assertEqual(result.metrics.trades_per_symbol, {"AAA": 1, "BBB": 1})
        self.assertEqual(result.metrics.win_rate_per_symbol, {"AAA": 100.0, "BBB": 0.0})

    def test_position_sizing_and_exposure_limits(self):
        cfg = BacktestConfig(starting_capital=10_000, risk_fraction=.01, max_exposure_fraction=.1,
                             commission_per_share=0, slippage_per_share=0)
        self.assertEqual(position_size(10_000, 100, 5, cfg), 10)
        self.assertEqual(position_size(10_000, 100, 5, cfg, current_exposure=1_000), 0)
        result = BacktestEngine(FixedSetupStrategy(), config=cfg).run({"ABC": [bar("ABC", 0, 100, 101, 99, 100), bar("ABC", 1, 100, 101, 99, 100), bar("ABC", 2, 100, 101, 99, 100), bar("ABC", 3, 100, 111, 99, 100)]})
        self.assertLessEqual(result.trades[0].shares * result.trades[0].entry_price, 1000)

    def test_insufficient_capital_and_fees_slippage(self):
        poor = engine(FixedSetupStrategy(), starting_capital=1, risk_fraction=.01).run({"ABC": [bar("ABC", 0, 100, 101, 99, 100), bar("ABC", 1, 100, 101, 99, 100), bar("ABC", 2, 100, 101, 99, 100), bar("ABC", 3, 100, 111, 99, 100)]})
        self.assertTrue(poor.errors)
        cost_data = {"ABC": [bar("ABC", 0, 100, 101, 99, 100), bar("ABC", 1, 100, 101, 99, 100), bar("ABC", 2, 100, 101, 99, 100), bar("ABC", 3, 100, 111, 99, 100)]}
        costly = engine(FixedSetupStrategy(stop=95, target=112), commission_per_share=.01, slippage_per_share=.01).run(
            {"ABC": [*cost_data["ABC"][:3], bar("ABC", 3, 100, 113, 99, 100)]}
        )
        self.assertGreater(costly.trades[0].fees, 0)
        baseline = engine(FixedSetupStrategy(stop=95, target=112)).run({"ABC": [*cost_data["ABC"][:3], bar("ABC", 3, 100, 113, 99, 100)]})
        self.assertLess(costly.trades[0].pnl, baseline.trades[0].pnl)

    def test_drawdown_and_repeatability(self):
        data = {"ABC": [bar("ABC", 0, 100, 101, 99, 100), bar("ABC", 1, 100, 111, 99, 100),
                        bar("ABC", 2, 100, 101, 99, 100), bar("ABC", 3, 100, 102, 94, 96)]}
        first, second = engine().run(data), engine().run(data)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first.metrics.maximum_drawdown, 0)

    def test_invalid_history_fails_qualification(self):
        bad = engine().run({"ABC": [bar("ABC", 0, 100, 98, 99, 100)]})
        self.assertFalse(bad.completed)
        self.assertFalse(bad.qualified)


if __name__ == "__main__":
    unittest.main()
