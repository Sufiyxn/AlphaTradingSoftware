import unittest
from datetime import datetime, timedelta, timezone

from backend.market_data import (
    Bar,
    DataValidationError,
    MarketDataProviderError,
    MarketDataService,
    Quote,
    normalize_bar,
)
from backend.market_data.mock_provider import MockMarketDataProvider


START = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
END = START + timedelta(minutes=3)


class MarketDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MarketDataService(MockMarketDataProvider())

    def test_valid_bar_normalizes_symbol_and_timestamp(self) -> None:
        bar = normalize_bar(Bar(" aapl ", START.astimezone(timezone(timedelta(hours=2))), 10, 12, 9, 11, 20))
        self.assertEqual(bar.symbol, "AAPL")
        self.assertEqual(bar.timestamp, START)

    def test_invalid_ohlc_rejected(self) -> None:
        with self.assertRaises(DataValidationError):
            normalize_bar(Bar("AAPL", START, 10, 9, 8, 10, 2))

    def test_missing_value_rejected(self) -> None:
        with self.assertRaisesRegex(DataValidationError, "missing bar field: close"):
            normalize_bar({"symbol": "AAPL", "timestamp": START, "open": 10, "high": 11, "low": 9, "volume": 1})

    def test_naive_timestamp_rejected(self) -> None:
        with self.assertRaisesRegex(DataValidationError, "timezone"):
            normalize_bar(Bar("AAPL", datetime(2024, 1, 2), 10, 11, 9, 10, 1))

    def test_historical_range_and_order(self) -> None:
        bars = self.service.get_historical("aapl", START, END)
        self.assertEqual(len(bars), 3)
        self.assertEqual([bar.timestamp for bar in bars], sorted(bar.timestamp for bar in bars))

    def test_multiple_symbols_latest(self) -> None:
        quotes = self.service.get_latest(["AAPL", "MSFT", "AAPL"])
        self.assertEqual([quote.symbol for quote in quotes], ["AAPL", "MSFT"])
        self.assertTrue(all(isinstance(quote, Quote) for quote in quotes))

    def test_provider_failure_is_normalized(self) -> None:
        service = MarketDataService(MockMarketDataProvider(fail=True))
        with self.assertRaises(MarketDataProviderError):
            service.get_historical("AAPL", START, END)


if __name__ == "__main__":
    unittest.main()
