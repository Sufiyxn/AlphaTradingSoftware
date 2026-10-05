from datetime import datetime, timezone
from typing import Sequence

from .models import Bar, Quote
from .provider import MarketDataProviderError


_TIMES = (
    datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
    datetime(2024, 1, 2, 14, 31, tzinfo=timezone.utc),
    datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc),
)


class MockMarketDataProvider:
    """Small deterministic data source for tests and local development."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self._bars = {
            "AAPL": (
                Bar("AAPL", _TIMES[0], 100, 102, 99, 101, 1000),
                Bar("AAPL", _TIMES[1], 101, 103, 100, 102, 1200),
                Bar("AAPL", _TIMES[2], 102, 104, 101, 103, 900),
            ),
            "MSFT": (
                Bar("MSFT", _TIMES[0], 200, 202, 199, 201, 800),
                Bar("MSFT", _TIMES[1], 201, 203, 200, 202, 950),
                Bar("MSFT", _TIMES[2], 202, 204, 201, 203, 700),
            ),
        }

    def get_historical(self, symbol: str, start: datetime, end: datetime) -> Sequence[Bar]:
        self._check()
        return tuple(bar for bar in self._bars.get(symbol.upper(), ()) if start <= bar.timestamp < end)

    def get_latest(self, symbols: Sequence[str]) -> Sequence[Quote]:
        self._check()
        results = []
        for symbol in dict.fromkeys(s.upper() for s in symbols):
            bars = self._bars.get(symbol)
            if bars:
                latest = bars[-1]
                results.append(Quote(latest.symbol, latest.timestamp, latest.close, latest.volume))
        return tuple(results)

    def _check(self) -> None:
        if self.fail:
            raise RuntimeError("mock provider failure")
