import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from backend.market_data import Bar, MarketDataProviderError, MarketDataService
from backend.scanner import (
    AnalysisContext,
    Direction,
    MarketScannerService,
    Opportunity,
    SymbolUniverse,
    TemporaryTestAnalyzer,
)
from backend.testing import MockMarketDataProvider


NOW = datetime(2024, 1, 2, 14, 33, tzinfo=timezone.utc)
START = NOW - timedelta(minutes=3)


def bars(symbol: str, prices: tuple[float, float]) -> tuple[Bar, ...]:
    first, last = prices
    return (
        Bar(symbol, START, first, first + 1, first - 1, first, 100),
        Bar(symbol, NOW - timedelta(minutes=1), (first + last) / 2, max(first, last) + 1, min(first, last) - 1, (first + last) / 2, 110),
        Bar(symbol, NOW - timedelta(seconds=1), last, max(first, last) + 1, min(first, last) - 1, last, 120),
    )


class FixtureProvider:
    def __init__(self, data: dict[str, tuple[Bar, ...]], failures: set[str] | None = None):
        self.data = data
        self.failures = failures or set()

    def get_historical(self, symbol, start, end):
        if symbol in self.failures:
            raise RuntimeError("fixture failure")
        return tuple(item for item in self.data.get(symbol, ()) if start <= item.timestamp < end)

    def get_latest(self, symbols):
        return ()


class FixedAnalyzer:
    def __init__(self, opportunities: dict[str, Opportunity]):
        self.opportunities = opportunities

    def analyze(self, context: AnalysisContext) -> Opportunity:
        return self.opportunities[context.symbol]


def opportunity(symbol: str, *, direction: Direction, score: float, ratio: float = 2.0, valid: bool = True) -> Opportunity:
    entry, risk = 100.0, 2.0
    reward = risk * ratio
    signed = 1 if direction is Direction.LONG else -1
    return Opportunity(symbol, NOW, direction, score, entry, entry - signed * risk, entry + signed * reward, risk, reward, ratio, ("fixture",), valid)


class ScannerTests(unittest.TestCase):
    def scanner(self, provider, analyzer=None):
        return MarketScannerService(
            MarketDataService(provider), analyzer or TemporaryTestAnalyzer(),
            clock=lambda: NOW, lookback=timedelta(minutes=3),
        )

    def test_one_symbol_long_opportunity(self):
        result = self.scanner(FixtureProvider({"UP": bars("UP", (100, 110))})).scan(SymbolUniverse(["UP"]))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].direction, Direction.LONG)
        self.assertTrue(result[0].valid)

    def test_multiple_symbols_and_rank_order(self):
        data = {symbol: bars(symbol, (100, 101)) for symbol in ("A", "B", "C")}
        candidates = {"A": opportunity("A", direction=Direction.LONG, score=.1), "B": opportunity("B", direction=Direction.SHORT, score=.8), "C": opportunity("C", direction=Direction.LONG, score=.3)}
        result = self.scanner(FixtureProvider(data), FixedAnalyzer(candidates)).scan(SymbolUniverse(data))
        self.assertEqual([item.symbol for item in result], ["B", "C", "A"])

    def test_short_opportunity(self):
        result = self.scanner(FixtureProvider({"DOWN": bars("DOWN", (110, 100))})).scan(SymbolUniverse(["DOWN"]))
        self.assertEqual(result[0].direction, Direction.SHORT)

    def test_invalid_missing_data_and_provider_failure_are_isolated(self):
        data = {"GOOD": bars("GOOD", (100, 110)), "BAD": (Bar("BAD", START, 10, 9, 8, 10, 1),)}
        scanner = self.scanner(FixtureProvider(data, {"FAIL"}))
        ranked, results = scanner.scan_detailed(SymbolUniverse(["GOOD", "BAD", "MISSING", "FAIL"]))
        self.assertEqual([item.symbol for item in ranked], ["GOOD"])
        self.assertEqual(len(results), 4)
        self.assertTrue(all(row.error for row in results[1:]))

    def test_risk_reward_below_exact_and_above_floor(self):
        cases = {
            "BELOW": opportunity("BELOW", direction=Direction.LONG, score=3, ratio=1.99),
            "EXACT": opportunity("EXACT", direction=Direction.LONG, score=2, ratio=2.0),
            "ABOVE": opportunity("ABOVE", direction=Direction.SHORT, score=1, ratio=2.5),
        }
        data = {symbol: bars(symbol, (100, 101)) for symbol in cases}
        result = self.scanner(FixtureProvider(data), FixedAnalyzer(cases)).scan(SymbolUniverse(cases))
        self.assertEqual([item.symbol for item in result], ["EXACT", "ABOVE"])

    def test_universe_deduplicates_and_empty_scan(self):
        universe = SymbolUniverse([" aapl ", "AAPL", " "])
        self.assertEqual(universe.symbols, ("AAPL",))
        self.assertEqual(self.scanner(MockMarketDataProvider()).scan(SymbolUniverse([])), ())

    def test_invalid_candidate_not_returned(self):
        candidate = replace(opportunity("X", direction=Direction.LONG, score=1, ratio=2), valid=False)
        result = self.scanner(FixtureProvider({"X": bars("X", (100, 101))}), FixedAnalyzer({"X": candidate})).scan(SymbolUniverse(["X"]))
        self.assertEqual(result, ())


if __name__ == "__main__":
    unittest.main()
