from typing import Protocol, Sequence

from backend.market_data.models import Bar

from .models import StrategyConfig, StrategySignal


class Strategy(Protocol):
    name: str

    def evaluate(self, bars: Sequence[Bar], config: StrategyConfig) -> StrategySignal:
        """Evaluate validated normalized bars for a single symbol."""
        ...
