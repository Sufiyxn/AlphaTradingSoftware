"""Watchlist rows from validated candles: prices, indicators, honest gaps."""
import unittest
from datetime import datetime, timedelta, timezone

from backend.config import load_config
from backend.market_data.series import CandleSeriesService
from backend.scanner.watchlist import (
    INDICATORS_INSUFFICIENT,
    INDICATORS_OK,
    INDICATORS_UNAVAILABLE,
    PRICED,
    UNPRICED,
    WatchlistService,
)
from backend.strategies.indicators import atr, ema, rsi
from backend.tests.test_market_data_series import FakeProvider, NOW, scripted_bars


def watchlist(provider=None, symbols=("AAA", "BBB"), *, clock=lambda: NOW):
    series = CandleSeriesService(
        load_config(env={}).market_data, clock=clock,
        provider_factory=(None if provider is None
                          else (lambda _settings: provider)))
    return WatchlistService(series, symbols)


class WatchlistTests(unittest.TestCase):
    def test_without_a_provider_every_row_says_not_configured(self):
        snapshot = watchlist().snapshot()
        self.assertEqual(snapshot.state, "NOT_CONFIGURED")
        self.assertIn("no market data provider", snapshot.reason)
        self.assertEqual([row.symbol for row in snapshot.rows], ["AAA", "BBB"])
        for row in snapshot.rows:
            self.assertEqual(row.state, UNPRICED)
            self.assertIsNone(row.last_price)
            self.assertEqual(row.indicator_state, INDICATORS_UNAVAILABLE)
        self.assertEqual(snapshot.rows[0].payload()["last_price"], None)

    def test_priced_rows_carry_price_change_volume_and_indicators(self):
        provider = FakeProvider({"AAA": scripted_bars("AAA", count=40)})
        snapshot = watchlist(provider).snapshot()
        self.assertEqual(snapshot.state, "OK")
        row = snapshot.rows[0]
        self.assertEqual(row.state, PRICED)
        self.assertEqual(row.bar_count, 40)
        self.assertIsNotNone(row.ema)
        self.assertIsNotNone(row.rsi)
        self.assertIsNotNone(row.atr)
        self.assertEqual(row.indicator_state, INDICATORS_OK)
        self.assertEqual(row.last_price, 50.25)
        self.assertEqual(row.previous_close, 50.25)
        closes = [bar.close for bar in scripted_bars("AAA", count=40)]
        bars = scripted_bars("AAA", count=40)
        self.assertAlmostEqual(row.ema, ema(closes, 20))
        self.assertAlmostEqual(row.rsi, rsi(closes, 14))
        self.assertAlmostEqual(row.atr, atr(bars, 14))
        self.assertAlmostEqual(row.change_percent, 0.0)

    def test_a_symbol_without_enough_bars_keeps_its_row_and_says_why(self):
        provider = FakeProvider({
            "AAA": scripted_bars("AAA", count=40),
            "BBB": scripted_bars("BBB", count=3),
        })
        rows = {row.symbol: row for row in watchlist(provider).snapshot().rows}
        self.assertEqual(rows["BBB"].state, PRICED)
        self.assertEqual(rows["BBB"].indicator_state, INDICATORS_INSUFFICIENT)
        self.assertIn("at least 20", rows["BBB"].indicator_reason)
        self.assertIsNone(rows["BBB"].ema)
        self.assertEqual(rows["BBB"].last_price, 50.25)

    def test_a_symbol_the_provider_returns_nothing_for_is_reported_not_dropped(self):
        provider = FakeProvider({"AAA": scripted_bars("AAA", count=40)})
        rows = {row.symbol: row for row in watchlist(provider, symbols=("AAA", "ZZZ")).snapshot().rows}
        self.assertEqual(rows["AAA"].state, PRICED)
        self.assertEqual(rows["ZZZ"].state, UNPRICED)
        self.assertIsNone(rows["ZZZ"].last_price)
        self.assertTrue(rows["ZZZ"].reason)

    def test_change_percent_compares_against_the_previous_close(self):
        bars = list(scripted_bars("AAA", count=5, price=50.0))
        last = bars[-1]
        from backend.market_data import Bar
        bars[-1] = Bar(last.symbol, last.timestamp, 51.0, 52.5, 50.5, 52.0, last.volume)
        provider = FakeProvider({"AAA": tuple(bars)})
        row = watchlist(provider).snapshot().rows[0]
        self.assertEqual(row.state, PRICED)
        self.assertAlmostEqual(row.change_percent, (52.0 - 50.25) / 50.25 * 100.0)
        self.assertEqual(row.last_price, 52.0)

    def test_rows_age_honestly_against_the_clock(self):
        provider = FakeProvider({"AAA": scripted_bars("AAA", count=40)})
        later = NOW + timedelta(seconds=600)
        row = watchlist(provider, clock=lambda: later).snapshot().rows[0]
        self.assertEqual(row.freshness, "STALE")
        self.assertGreater(row.age_seconds, 300)


if __name__ == "__main__":
    unittest.main()
