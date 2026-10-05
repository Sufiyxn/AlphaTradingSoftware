from datetime import datetime, timedelta, timezone
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

    def __init__(self, *, fail: bool = False, demo_history: bool = False):
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
        if demo_history:
            self._bars.update({symbol: _demo_history(symbol, base) for symbol, base in {
                "F": 10.0, "SOFI": 8.0, "PLTR": 25.0, "AAPL": 92.0, "BAC": 30.0,
            }.items()})

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._bars))

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


def _demo_history(symbol: str, base: float) -> tuple[Bar, ...]:
    """Build a fixed, explicitly synthetic history for local pipeline demonstrations."""
    scale = base / 25.0
    closes = [base]
    for _ in range(46):
        closes.append(closes[-1] + .025 * scale)
    for index in range(47, 57):
        closes.append(closes[-1] + (-.08 if index % 3 else .02) * scale)
    for _ in range(3):
        closes.append(closes[-1] + .21 * scale)
    closes.append(closes[-1])
    closes.append(closes[-1])
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    bars = []
    for index, close in enumerate(closes):
        open_price = closes[index - 1] if index else close
        high_extension = 1.30 * scale if index >= len(closes) - 3 else .07 * scale
        bars.append(Bar(symbol, start + timedelta(minutes=index), open_price,
                        max(open_price, close) + high_extension,
                        min(open_price, close) - .07 * scale, close, 100_000))
    return tuple(bars)
