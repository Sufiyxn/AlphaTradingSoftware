from datetime import datetime, timedelta, timezone
from typing import Protocol

from backend.market_data import DataValidationError, MarketDataProviderError, MarketDataService

from .analysis import AnalysisContext, PerSymbolAnalyzer
from .models import Opportunity, SymbolResult
from .ranking import filter_and_rank
from .universe import SymbolUniverse


class MarketScanner(Protocol):
    def scan(self, universe: SymbolUniverse) -> tuple[Opportunity, ...]:
        ...


class MarketScannerService:
    """Collect validated data, isolate per-symbol failures, analyze, filter, and rank."""

    def __init__(
        self,
        market_data: MarketDataService,
        analyzer: PerSymbolAnalyzer,
        *,
        lookback: timedelta = timedelta(minutes=3),
        minimum_reward_risk: float = 2.0,
        clock=lambda: datetime.now(timezone.utc),
    ):
        if lookback <= timedelta(0):
            raise ValueError("lookback must be positive")
        if minimum_reward_risk < 2.0:
            raise ValueError("minimum_reward_risk cannot be below 2.0")
        self._market_data = market_data
        self._analyzer = analyzer
        self._lookback = lookback
        self._minimum_reward_risk = minimum_reward_risk
        self._clock = clock

    def scan(self, universe: SymbolUniverse) -> tuple[Opportunity, ...]:
        return self.scan_detailed(universe)[0]

    def scan_detailed(self, universe: SymbolUniverse) -> tuple[tuple[Opportunity, ...], tuple[SymbolResult, ...]]:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("scanner clock must return a timezone-aware datetime")
        now = now.astimezone(timezone.utc)
        start = now - self._lookback
        opportunities: list[Opportunity] = []
        results: list[SymbolResult] = []
        # Batch per bounded chunk through the existing market-data service; errors stay isolated by symbol.
        for symbol in universe.symbols:
            try:
                bars = self._market_data.get_historical(symbol, start, now)
                if not bars:
                    results.append(SymbolResult(symbol, None, "No historical data available"))
                    continue
                opportunity = self._analyzer.analyze(AnalysisContext(symbol, bars, bars[-1].timestamp))
                if opportunity.symbol != symbol:
                    raise DataValidationError("analyzer returned an opportunity for a different symbol")
                opportunities.append(opportunity)
                results.append(SymbolResult(symbol, opportunity))
            except (DataValidationError, MarketDataProviderError, ValueError) as error:
                results.append(SymbolResult(symbol, None, str(error)))
            except Exception as error:
                results.append(SymbolResult(symbol, None, f"analysis failed: {error}"))
        ranked = filter_and_rank(opportunities, self._minimum_reward_risk)
        return ranked, tuple(results)
