import unittest
from datetime import datetime, timezone

from backend.portfolio import Portfolio
from backend.strategies.models import Direction


NOW = datetime(2024, 1, 5, 15, tzinfo=timezone.utc)


class PortfolioTests(unittest.TestCase):
    def test_open_add_mark_and_close(self):
        portfolio = Portfolio(10_000)
        portfolio.open_position("abc", Direction.LONG, 10, 100, NOW)
        portfolio.open_position("ABC", Direction.LONG, 10, 110, NOW)
        position = portfolio.positions[0]
        self.assertEqual(position.quantity, 20)
        self.assertEqual(position.average_entry_price, 105)
        self.assertEqual(portfolio.snapshot().cash, 7_900)

        portfolio.update_price("ABC", 115)
        snapshot = portfolio.snapshot()
        self.assertEqual(snapshot.unrealized_pnl, 200)
        self.assertEqual(snapshot.exposure, 2_300)
        self.assertEqual(snapshot.equity, 10_200)

        realized = portfolio.close_position("ABC", 5, 120, fees=2, timestamp=NOW)
        self.assertEqual(realized, 73)
        self.assertEqual(portfolio.positions[0].quantity, 15)
        self.assertEqual(portfolio.snapshot().trade_count, 1)

    def test_full_close_removes_position_and_tracks_daily_pnl(self):
        portfolio = Portfolio(5_000)
        portfolio.open_position("XYZ", Direction.SHORT, 10, 50, NOW)
        pnl = portfolio.close_position("XYZ", price=45, timestamp=NOW)
        self.assertEqual(pnl, 50)
        snapshot = portfolio.snapshot()
        self.assertEqual(snapshot.positions, ())
        self.assertEqual(snapshot.realized_pnl, 50)
        self.assertEqual(snapshot.daily_pnl, 50)
        self.assertEqual(snapshot.cash, 5_050)

    def test_multiple_symbols(self):
        portfolio = Portfolio(10_000)
        portfolio.open_position("AAA", Direction.LONG, 10, 100, NOW)
        portfolio.open_position("BBB", Direction.SHORT, 5, 200, NOW)
        self.assertEqual([position.symbol for position in portfolio.positions], ["AAA", "BBB"])
        self.assertEqual(portfolio.snapshot().exposure, 2_000)

    def test_daily_pnl_resets_on_new_date(self):
        portfolio = Portfolio(1_000)
        portfolio.open_position("A", Direction.LONG, 1, 10, NOW)
        portfolio.close_position("A", price=12, timestamp=NOW)
        next_day = datetime(2024, 1, 6, 15, tzinfo=timezone.utc)
        portfolio.open_position("B", Direction.LONG, 1, 10, next_day)
        portfolio.close_position("B", price=9, timestamp=next_day)
        self.assertEqual(portfolio.snapshot().daily_pnl, -1)
        self.assertEqual(portfolio.snapshot().realized_pnl, 1)

    def test_invalid_operations_rejected(self):
        portfolio = Portfolio()
        with self.assertRaises(ValueError):
            portfolio.open_position("", Direction.LONG, 1, 10, NOW)
        portfolio.open_position("A", Direction.LONG, 1, 10, NOW)
        with self.assertRaises(ValueError):
            portfolio.open_position("A", Direction.SHORT, 1, 10, NOW)


if __name__ == "__main__":
    unittest.main()
