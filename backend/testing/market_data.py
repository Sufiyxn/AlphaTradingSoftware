from datetime import datetime, timedelta, timezone
from typing import Sequence

from backend.market_data.models import Bar, Quote

_TIMES = (
    datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
    datetime(2024, 1, 2, 14, 31, tzinfo=timezone.utc),
    datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc),
)


class MockMarketDataProvider:
    """Small deterministic source for automated tests.

    It serves a fixed, hand-written set of bars and quotes. It contains no
    price generator, so nothing reachable from this class can trend, oscillate,
    or invent a market.
    """

    is_configured = True
    provider = "test_fixture"

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


def fixture_history(symbol: str, *, end: datetime, count: int = 60,
                    base_price: float = 50.0) -> tuple[Bar, ...]:
    """Deterministic, hand-specified series for automated tests only.

    The shape is a mild two-up/one-down staircase so a real EMA/RSI strategy
    sees both gains and losses instead of a one-way ramp. Nothing outside this
    module calls it, and no production module imports it.
    """
    symbol = symbol.strip().upper()
    end = end.astimezone(timezone.utc)
    step = round(base_price * 0.003, 6)
    extension = round(base_price * 0.0006, 6)
    reward_extension = round(base_price * 0.02, 6)
    closes: list[float] = []
    price = base_price
    for index in range(count):
        price += step if index % 3 != 2 else -step
        closes.append(round(price, 4))

    bars: list[Bar] = []
    previous_close = round(base_price, 4)
    for index, close in enumerate(closes):
        timestamp = end - timedelta(minutes=count - index)
        open_price = previous_close
        if index == count - 1:
            # Last bar reaches the reward the strategy requires from observed range.
            high = round(close + reward_extension, 4)
        else:
            high = round(max(open_price, close) + extension, 4)
        low = round(min(open_price, close) - extension, 4)
        bars.append(Bar(symbol, timestamp, open_price, high, low, close, 100_000))
        previous_close = close
    return tuple(bars)


class FixtureHistoryProvider:
    """Serves :func:`fixture_history` bars for a fixed symbol set. Tests only."""

    is_configured = True
    provider = "test_fixture"

    def __init__(self, symbols: Sequence[str], *, end: datetime, count: int = 60,
                 base_price: float = 50.0):
        self._bars = {
            symbol.strip().upper(): fixture_history(symbol, end=end, count=count,
                                                    base_price=base_price)
            for symbol in symbols
        }

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._bars))

    def get_historical(self, symbol: str, start: datetime, end: datetime) -> Sequence[Bar]:
        return tuple(bar for bar in self._bars.get(symbol.upper(), ()) if start <= bar.timestamp < end)

    def get_latest(self, symbols: Sequence[str]) -> Sequence[Quote]:
        results = []
        for symbol in dict.fromkeys(s.upper() for s in symbols):
            bars = self._bars.get(symbol)
            if bars:
                latest = bars[-1]
                results.append(Quote(latest.symbol, latest.timestamp, latest.close, latest.volume))
        return tuple(results)
