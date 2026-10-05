import unittest
from datetime import datetime, timedelta, timezone

from backend.market_data import Bar, DataValidationError
from backend.strategies import (
    BaselineTrendMomentumStrategy,
    Direction,
    StrategyConfig,
    StrategyEngine,
    atr,
    ema,
    rsi,
)


T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def trend_bars(symbol: str, closes: list[float], *, wide: bool = False) -> tuple[Bar, ...]:
    result = []
    for index, close in enumerate(closes):
        low = close - 0.6
        high = close + 0.6
        if index == len(closes) - 1:
            low = close - 0.6
            high = close + 0.6
        result.append(Bar(symbol, T0 + timedelta(minutes=index), close, high, low, close, 100))
    if wide:
        last = result[-1]
        high, low = (last.close + 5.6, last.close - 0.6) if closes[-1] > closes[0] else (last.close + 0.6, last.close - 5.6)
        result[-1] = Bar(symbol, last.timestamp, last.open, high, low, last.close, last.volume)
    return tuple(result)


def with_last_range(data: tuple[Bar, ...], direction: Direction, distance: float) -> tuple[Bar, ...]:
    rows = list(data)
    last = rows[-1]
    high = last.close + distance if direction is Direction.LONG else last.close + 0.6
    low = last.close - distance if direction is Direction.SHORT else last.close - 0.6
    rows[-1] = Bar(last.symbol, last.timestamp, last.open, high, low, last.close, last.volume)
    return tuple(rows)


def config(**overrides) -> StrategyConfig:
    values = dict(
        fast_ema_period=2, slow_ema_period=3, rsi_period=2, atr_period=2,
        atr_multiplier=1.0, max_stop_distance_pct=0.05,
        bullish_rsi_min=50, bullish_rsi_max=100,
        bearish_rsi_min=0, bearish_rsi_max=50,
    )
    values.update(overrides)
    return StrategyConfig(**values)


def setup_bars(final_high: float) -> tuple[Bar, ...]:
    closes = [100, 101, 102, 103, 104, 106, 107]
    data = [Bar("UP", T0 + timedelta(minutes=i), close, close + 1, close - 1, close, 100) for i, close in enumerate(closes)]
    last = data[-1]
    data[-1] = Bar(last.symbol, last.timestamp, last.open, final_high, last.close - 1, last.close, last.volume)
    return tuple(data)


class StrategyTests(unittest.TestCase):
    def test_ema_calculation(self):
        self.assertAlmostEqual(ema([1, 2, 3, 4], 2), 3.5)

    def test_rsi_calculation(self):
        self.assertEqual(rsi([1, 2, 3], 2), 100.0)
        self.assertEqual(rsi([3, 2, 1], 2), 0.0)

    def test_atr_calculation(self):
        sample = (
            Bar("X", T0, 10, 11, 9, 10, 1),
            Bar("X", T0 + timedelta(minutes=1), 10, 12, 9, 11, 1),
            Bar("X", T0 + timedelta(minutes=2), 11, 13, 10, 12, 1),
        )
        self.assertAlmostEqual(atr(sample, 2), 3.0)

    def test_bullish_setup_with_exact_two_to_one(self):
        baseline = setup_bars(110.0)
        risk = atr(baseline[:-1], 2) * 0.5
        last = baseline[-1]
        data = baseline[:-1] + (Bar(last.symbol, last.timestamp, last.open, last.close + 2 * risk, last.low, last.close, last.volume),)
        signal = StrategyEngine(BaselineTrendMomentumStrategy(), config(atr_multiplier=0.5)).evaluate(data)
        self.assertEqual(signal.direction, Direction.LONG)
        self.assertIsNotNone(signal.setup)
        setup = signal.setup
        self.assertAlmostEqual(setup.reward_risk_ratio, 2.0)
        self.assertAlmostEqual(setup.target_price, setup.entry_price + 2 * setup.risk_per_share)

    def test_bearish_setup(self):
        data = trend_bars("DOWN", [110, 109, 108, 107, 106, 104, 103], wide=True)
        signal = StrategyEngine(BaselineTrendMomentumStrategy(), config(atr_multiplier=0.5)).evaluate(data)
        self.assertEqual(signal.direction, Direction.SHORT)
        self.assertGreater(signal.setup.stop_price, signal.setup.entry_price)
        self.assertGreaterEqual(signal.setup.reward_risk_ratio, 2.0)

    def test_no_setup_when_observed_range_does_not_support_target(self):
        data = trend_bars("UP", [100, 101, 102, 103, 104, 105, 106])
        signal = StrategyEngine(BaselineTrendMomentumStrategy(), config()).evaluate(data)
        self.assertEqual(signal.direction, Direction.NONE)
        self.assertIsNone(signal.setup)

    def test_no_setup_for_non_aligned_trend_and_momentum(self):
        data = trend_bars("FLAT", [100, 100.1, 99.9, 100, 100.1, 99.9])
        signal = StrategyEngine(BaselineTrendMomentumStrategy(), config()).evaluate(data)
        self.assertIsNone(signal.setup)

    def test_insufficient_history(self):
        signal = StrategyEngine(config=config()).evaluate(trend_bars("UP", [100, 101]))
        self.assertIn("Insufficient", signal.reason)
        self.assertIsNone(signal.setup)

    def test_risk_above_configured_maximum_rejected(self):
        signal = StrategyEngine(config=config(atr_multiplier=10, max_stop_distance_pct=.01)).evaluate(
            trend_bars("UP", [100, 101, 102, 103, 104, 106, 107], wide=True)
        )
        self.assertIsNone(signal.setup)
        self.assertIn("exceeds", signal.reason)

    def test_invalid_market_data_rejected(self):
        invalid = Bar("X", T0, 10, 8, 9, 10, 1)
        with self.assertRaises(DataValidationError):
            StrategyEngine(config=config()).evaluate([invalid])

    def test_missing_or_empty_market_data_returns_no_trade(self):
        signal = StrategyEngine(config=config()).evaluate([])
        self.assertEqual(signal.direction, Direction.NONE)
        self.assertIsNone(signal.setup)

    def test_multiple_symbols_and_repeatability(self):
        engine = StrategyEngine(config=config())
        histories = {
            "UP": trend_bars("UP", [100, 101, 102, 103, 104, 106, 107], wide=True),
            "DOWN": trend_bars("DOWN", [110, 109, 108, 107, 106, 104, 103], wide=True),
        }
        first = engine.evaluate_many(histories)
        second = engine.evaluate_many(histories)
        self.assertEqual(first, second)
        self.assertEqual([signal.symbol for signal in first], ["UP", "DOWN"])

    def test_below_two_to_one_never_emits_setup(self):
        strategy = BaselineTrendMomentumStrategy()
        data = with_last_range(trend_bars("UP", [100, 101, 102, 103, 104, 106, 107]), Direction.LONG, 2.0)
        signal = strategy.evaluate(data, config())
        self.assertIsNone(signal.setup)


if __name__ == "__main__":
    unittest.main()
