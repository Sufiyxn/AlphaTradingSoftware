"""Candidate detection driven by the product strategy rather than a fixture."""
from backend.strategies.models import Direction as StrategyDirection
from backend.strategies.service import StrategyEngine

from .analysis import AnalysisContext
from .models import Direction, Opportunity


class StrategyAnalyzer:
    """Rank symbols by running the real strategy and exposing its setup.

    Nothing is generated here: entry, stop, and target come from the strategy's
    own evaluation of validated bars, and a symbol with no qualifying setup
    yields an invalid opportunity instead of an invented candidate.
    """

    def __init__(self, engine: StrategyEngine, *, minimum_reward_risk: float = 2.0):
        if minimum_reward_risk < 2.0:
            raise ValueError("minimum_reward_risk cannot be below 2.0")
        self._engine = engine
        self._minimum_reward_risk = minimum_reward_risk

    def analyze(self, context: AnalysisContext) -> Opportunity:
        signal = self._engine.evaluate(context.bars)
        setup = signal.setup
        reasons = (signal.reason, f"strategy: {signal.strategy_name}")
        if setup is None:
            return Opportunity(
                symbol=context.symbol, timestamp=context.timestamp,
                direction=_direction(signal.direction), score=0.0,
                entry_price=None, stop_price=None, target_price=None,
                risk_per_share=None, reward_per_share=None, reward_risk_ratio=None,
                reasons=reasons, valid=False,
            )
        ratio = setup.reward_risk_ratio
        valid = ratio is not None and ratio >= self._minimum_reward_risk
        return Opportunity(
            symbol=setup.symbol, timestamp=setup.timestamp,
            direction=_direction(setup.direction), score=setup.score,
            entry_price=setup.entry_price, stop_price=setup.stop_price,
            target_price=setup.target_price, risk_per_share=setup.risk_per_share,
            reward_per_share=setup.reward_per_share, reward_risk_ratio=ratio,
            reasons=reasons, valid=valid,
        )


def _direction(direction) -> Direction:
    if direction is StrategyDirection.LONG:
        return Direction.LONG
    if direction is StrategyDirection.SHORT:
        return Direction.SHORT
    return Direction.NONE
