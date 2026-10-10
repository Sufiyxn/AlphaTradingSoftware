"""Provider-independent candle series: validation, freshness, and honesty."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from backend.config import load_config
from backend.core.server import _json_default
from backend.market_data import Bar, MarketDataProviderError
from backend.market_data.series import (
    MAX_POINTS,
    CandleSeriesService,
    normalize_symbol,
    normalize_timeframe,
)

NOW = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc)


class FakeProvider:
    """Scripted bars per symbol; records every historical request it serves."""

    provider = "fixture"
    is_configured = True

    def __init__(self, bars_by_symbol=(), *, error=None):
        self.bars = {symbol: tuple(values) for symbol, values in dict(bars_by_symbol).items()}
        self.error = error
        self.requests = []

    def get_historical(self, symbol, start, end):
        self.requests.append((symbol, start, end))
        if self.error is not None:
            raise self.error
        return tuple(bar for bar in self.bars.get(symbol, ())
                     if start <= bar.timestamp < end)

    def get_latest(self, symbols):
        return ()


def scripted_bars(symbol, *, end=NOW, count=60, step_minutes=1, price=50.0):
    return tuple(
        Bar(symbol, end - timedelta(minutes=step_minutes * (count - index)),
            price, price + 1.0, price - 1.0, price + 0.25, 1000.0)
        for index in range(count))


def service(provider=None, *, clock=lambda: NOW, settings=None):
    return CandleSeriesService(
        settings or load_config(env={}).market_data, clock=clock,
        provider_factory=(None if provider is None
                          else (lambda _settings: provider)))


class CandleSeriesTests(unittest.TestCase):
    def test_without_a_provider_the_series_is_empty_and_says_so(self):
        series = service().get("AAPL")
        self.assertEqual(series.state, "NOT_CONFIGURED")
        self.assertEqual(series.bars, ())
        self.assertEqual(series.payload()["candles"], [])
        self.assertEqual(series.payload()["provider"], "none")
        self.assertIn("no market data provider", series.reason)

    def test_real_bars_are_served_with_chronological_validated_candles(self):
        provider = FakeProvider({"AAA": scripted_bars("AAA")})
        series = service(provider).get("AAA", timeframe="1min")
        self.assertEqual(series.state, "OK")
        self.assertEqual(series.bar_count, 60)
        self.assertEqual(series.provider, "fixture")
        payload = series.payload()
        self.assertEqual(len(payload["candles"]), 60)
        times = [candle["time"] for candle in payload["candles"]]
        self.assertEqual(times, sorted(times))
        self.assertTrue(all(candle["high"] >= candle["low"]
                            for candle in payload["candles"]))
        self.assertEqual(payload["candles"][-1]["time"],
                         int((NOW - timedelta(minutes=1)).timestamp()))
        json.dumps(payload, default=_json_default)

    def test_a_bar_older_than_the_freshness_window_reports_stale(self):
        provider = FakeProvider({"AAA": scripted_bars("AAA", count=5)})
        later = NOW + timedelta(seconds=600)
        series = service(provider, clock=lambda: later).get("AAA", timeframe="1min")
        self.assertEqual(series.state, "OK")
        self.assertEqual(series.freshness, "STALE")
        self.assertGreater(series.age_seconds, series.max_age_seconds)

    def test_a_recent_bar_reports_fresh(self):
        provider = FakeProvider({"AAA": scripted_bars("AAA", count=5)})
        series = service(provider).get("AAA", timeframe="1min")
        self.assertEqual(series.freshness, "FRESH")
        self.assertLessEqual(series.age_seconds, series.max_age_seconds)

    def test_a_provider_failure_is_reported_not_raised(self):
        provider = FakeProvider(error=MarketDataProviderError("fixture outage"))
        series = service(provider).get("AAA")
        self.assertEqual(series.state, "ERROR")
        self.assertIn("fixture outage", series.reason)
        self.assertEqual(series.bars, ())

    def test_no_bars_in_the_window_reports_no_data(self):
        provider = FakeProvider({"AAA": scripted_bars("AAA", count=0)})
        series = service(provider).get("AAA")
        self.assertEqual(series.state, "NO_DATA")
        self.assertIn("no candles", series.reason)

    def test_a_timeframe_the_provider_does_not_advertise_is_refused(self):
        settings = load_config(env={"MARKET_DATA_PROVIDER": "alphavantage",
                                    "ALPHAVANTAGE_API_KEY": "present"}).market_data
        series = CandleSeriesService(settings, clock=lambda: NOW)
        with self.assertRaises(ValueError) as caught:
            series.get("AAPL", timeframe="1day")
        self.assertIn("not supported", str(caught.exception))
        self.assertIn("60min", str(caught.exception))

    def test_an_unknown_timeframe_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            service(FakeProvider()).get("AAA", timeframe="3weeks")
        self.assertIn("unknown timeframe", str(caught.exception))

    def test_symbol_and_days_inputs_are_validated(self):
        provider = FakeProvider({"AAA": scripted_bars("AAA")})
        target = service(provider)
        with self.assertRaises(ValueError):
            target.get("")
        with self.assertRaises(ValueError):
            target.get("AAA; DROP TABLE")
        with self.assertRaises(ValueError):
            target.get("AAA", days=0)
        with self.assertRaises(ValueError):
            target.get("AAA", days=-3)

    def test_a_long_window_is_clamped_to_the_timeframe_limit(self):
        provider = FakeProvider({"AAA": scripted_bars("AAA", count=5)})
        series = service(provider).get("AAA", timeframe="1min", days=9999)
        window = series.requested_end - series.requested_start
        self.assertLessEqual(window, timedelta(days=2))
        self.assertEqual(series.state, "OK")

    def test_more_points_than_the_ceiling_are_trimmed_honestly(self):
        provider = FakeProvider({
            "AAA": scripted_bars("AAA", count=MAX_POINTS + 120, step_minutes=1)})
        series = service(provider).get("AAA", timeframe="1min", days=2)
        self.assertTrue(series.truncated)
        self.assertEqual(series.bar_count, MAX_POINTS)
        self.assertEqual(len(series.bars), MAX_POINTS)

    def test_normalize_symbol_and_timeframe_helpers(self):
        self.assertEqual(normalize_symbol(" aapl "), "AAPL")
        for bad in ("", "   ", "A" * 17, "AA PL"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_symbol(bad)
        self.assertEqual(normalize_timeframe("1MIN", ("1min", "1day")), "1min")
        with self.assertRaises(ValueError):
            normalize_timeframe("1day", ("1min",))


if __name__ == "__main__":
    unittest.main()
