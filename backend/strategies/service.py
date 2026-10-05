from collections.abc import Mapping, Sequence

from backend.market_data.models import Bar

from .base import Strategy
from .baseline import BaselineTrendMomentumStrategy
from .models import StrategyConfig, StrategySignal


class StrategyEngine:
    """Evaluate per-symbol normalized history with a replaceable strategy."""

    def __init__(self, strategy: Strategy | None = None, config: StrategyConfig | None = None):
        self.strategy = strategy or BaselineTrendMomentumStrategy()
        self.config = config or StrategyConfig()

    def evaluate(self, bars: Sequence[Bar]) -> StrategySignal:
        return self.strategy.evaluate(bars, self.config)

    def evaluate_many(self, histories: Mapping[str, Sequence[Bar]]) -> tuple[StrategySignal, ...]:
        return tuple(self.evaluate(history) for history in histories.values())
