from datetime import datetime
from typing import Sequence

from .models import Bar, Quote


class UnconfiguredMarketDataProvider:
    """Stand-in used when no real market-data provider is connected.

    It returns no bars and no quotes. It never synthesizes a price, a candle,
    or a quote, so consumers report NOT_CONFIGURED instead of consuming
    fabricated data.
    """

    is_configured = False

    def __init__(self, provider: str = "none", reason: str = "no market data provider is connected"):
        self.provider = provider
        self.reason = reason

    @property
    def symbols(self) -> tuple[str, ...]:
        return ()

    def get_historical(self, symbol: str, start: datetime, end: datetime) -> Sequence[Bar]:
        return ()

    def get_latest(self, symbols: Sequence[str]) -> Sequence[Quote]:
        return ()
