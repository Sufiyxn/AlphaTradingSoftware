import unittest
from datetime import datetime, timedelta, timezone

from backend.config import load_config
from backend.market_data import MarketDataBridge, MarketDataService, Quote
from backend.market_data.bridge import (
    CONNECTED,
    ERROR,
    NOT_CONFIGURED,
    PRICED,
    QUOTE_UNPRICED,
    REASON_NO_REFRESH,
    REASON_NOT_CONFIGURED,
    STALE,
    UNAVAILABLE,
)
from backend.portfolio import Portfolio
from backend.portfolio.models import PriceState
from backend.strategies.models import Direction


NOW = datetime(2024, 1, 5, 15, 0, tzinfo=timezone.utc)


class QuoteProvider:
    """Configured provider serving only hand-written quotes."""

    is_configured = True
    provider = "test_fixture"

    def __init__(self, quotes=(), *, fail=False):
        self._quotes = tuple(quotes)
        self.fail = fail
        self.calls = 0

    def get_historical(self, symbol, start, end):
        return ()

    def get_latest(self, symbols):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider offline")
        wanted = {symbol.strip().upper() for symbol in symbols}
        return tuple(quote for quote in self._quotes if quote.symbol in wanted)


class UnreachableProvider:
    """Marked unconfigured; any query would be a bug in the bridge."""

    is_configured = False
    provider = "none"
    reason = "no market data provider is connected"

    def get_historical(self, symbol, start, end):
        raise AssertionError("an unconfigured provider must never be queried")

    def get_latest(self, symbols):
        raise AssertionError("an unconfigured provider must never be queried")


def make_bridge(provider, *, stale_after=300.0, clock=None):
    return MarketDataBridge(MarketDataService(provider), clock=clock or (lambda: NOW),
                            stale_after_seconds=stale_after)


class MarketDataBridgeTests(unittest.TestCase):
    def test_unconfigured_provider_reports_not_configured_and_is_never_queried(self):
        report = make_bridge(UnreachableProvider()).refresh(["AAA"])
        self.assertEqual(report.availability, NOT_CONFIGURED)
        self.assertEqual(report.reason, REASON_NOT_CONFIGURED)
        self.assertEqual([item.state for item in report.valuations], [QUOTE_UNPRICED])
        self.assertIsNone(report.valuations[0].price)
        self.assertEqual(report.valuations[0].reason, REASON_NOT_CONFIGURED)

    def test_availability_is_honest_before_the_first_refresh(self):
        connected = make_bridge(QuoteProvider((Quote("AAA", NOW, 100.0, 1),)))
        self.assertEqual(connected.availability()["state"], UNAVAILABLE)
        self.assertEqual(connected.availability()["reason"], REASON_NO_REFRESH)
        disconnected = make_bridge(UnreachableProvider())
        self.assertEqual(disconnected.availability()["state"], NOT_CONFIGURED)
        self.assertEqual(disconnected.availability()["reason"], REASON_NOT_CONFIGURED)

    def test_fresh_quote_is_priced_and_reports_connected(self):
        observed = NOW - timedelta(seconds=10)
        report = make_bridge(QuoteProvider((Quote("AAA", observed, 110.5, 100),))).refresh(["AAA"])
        valuation = report.by_symbol["AAA"]
        self.assertEqual(valuation.state, PRICED)
        self.assertEqual(valuation.price, 110.5)
        self.assertEqual(valuation.as_of, observed)
        self.assertIsNone(valuation.reason)
        self.assertEqual(report.availability, CONNECTED)
        self.assertIsNone(report.reason)
        self.assertEqual(report.last_quote_at, observed)

    def test_quote_older_than_the_window_is_stale_and_never_reported_fresh(self):
        observed = NOW - timedelta(seconds=900)
        report = make_bridge(QuoteProvider((Quote("AAA", observed, 110.5, 100),))).refresh(["AAA"])
        valuation = report.by_symbol["AAA"]
        self.assertEqual(valuation.state, STALE)
        self.assertEqual(valuation.price, 110.5)
        self.assertEqual(valuation.as_of, observed)
        self.assertIn("staleness window", valuation.reason)
        self.assertEqual(report.availability, STALE)

    def test_staleness_window_comes_from_configuration_not_a_constant(self):
        quote = QuoteProvider((Quote("AAA", NOW - timedelta(seconds=900), 110.5, 100),))
        self.assertEqual(make_bridge(quote, stale_after=60).refresh(["AAA"]).availability, STALE)
        self.assertEqual(make_bridge(quote, stale_after=1800).refresh(["AAA"]).availability, CONNECTED)
        defaults = load_config(env={})
        self.assertEqual(defaults.market_data.stale_after_seconds, 300.0)
        configured = load_config(env={"MARKET_DATA_STALE_AFTER_SECONDS": "45"})
        self.assertEqual(configured.market_data.stale_after_seconds, 45.0)
        self.assertEqual(
            make_bridge(quote, stale_after=configured.market_data.stale_after_seconds)
            .refresh(["AAA"]).availability, STALE)

    def test_missing_quote_is_unavailable_and_never_invents_a_price(self):
        report = make_bridge(QuoteProvider(())).refresh(["AAA", "BBB"])
        self.assertEqual(report.availability, UNAVAILABLE)
        self.assertIn("no quote", report.reason)
        self.assertTrue(all(item.state == QUOTE_UNPRICED for item in report.valuations))
        self.assertTrue(all(item.price is None for item in report.valuations))
        self.assertTrue(all(item.as_of is None for item in report.valuations))

    def test_provider_failure_reports_error_and_never_invents_a_price(self):
        report = make_bridge(QuoteProvider(fail=True)).refresh(["AAA"])
        self.assertEqual(report.availability, ERROR)
        self.assertIn("request failed", report.reason)
        self.assertTrue(all(item.state == QUOTE_UNPRICED for item in report.valuations))
        self.assertTrue(all(item.price is None for item in report.valuations))

    def test_bridge_never_treats_an_entry_price_as_a_market_price(self):
        portfolio = Portfolio(10_000)
        portfolio.open_position("AAA", Direction.LONG, 10, 100.0, NOW)
        report = make_bridge(UnreachableProvider()).refresh(["AAA"])
        self.assertEqual(report.by_symbol["AAA"].state, QUOTE_UNPRICED)
        position = portfolio.positions[0]
        self.assertIsNone(position.market_price)
        self.assertIsNone(position.unrealized_pnl)
        self.assertEqual(position.price_state, PriceState.UNPRICED)
        self.assertEqual(position.average_entry_price, 100.0)

    def test_rejects_a_non_positive_staleness_window(self):
        with self.assertRaises(ValueError):
            make_bridge(QuoteProvider(), stale_after=0)


if __name__ == "__main__":
    unittest.main()
