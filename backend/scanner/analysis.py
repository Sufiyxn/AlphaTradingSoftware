from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

from backend.market_data.models import Bar

from .models import Direction, Opportunity


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    symbol: str
    bars: tuple[Bar, ...]
    timestamp: datetime


class PerSymbolAnalyzer(Protocol):
    def analyze(self, context: AnalysisContext) -> Opportunity:
        """Evaluate one symbol and return a normalized opportunity."""
        ...


class TemporaryTestAnalyzer:
    """Deterministic pipeline fixture for automated tests only.

    ``ApplicationRuntime`` wires :class:`backend.scanner.StrategyAnalyzer`
    instead; this fixture is never part of the production service graph.
    """

    def __init__(self, minimum_reward_risk: float = 2.0):
        self.minimum_reward_risk = minimum_reward_risk

    def analyze(self, context: AnalysisContext) -> Opportunity:
        if not context.bars:
            return _none(context, "No historical bars available")

        first, latest = context.bars[0], context.bars[-1]
        delta = latest.close - first.open
        if delta == 0:
            return _none(context, "No price movement in analysis window")

        direction = Direction.LONG if delta > 0 else Direction.SHORT
        entry = latest.close
        risk = round(entry * 0.01, 8)
        reward = round(abs(delta), 8)
        ratio = round(reward / risk, 8) if risk else None
        stop = round(entry - risk if direction is Direction.LONG else entry + risk, 8)
        # This fixture reports a target only when observed movement supports the minimum ratio.
        target = round(entry + reward if direction is Direction.LONG else entry - reward, 8) if ratio is not None and ratio >= self.minimum_reward_risk else None
        valid = target is not None and ratio is not None and ratio >= self.minimum_reward_risk
        reason = "Deterministic test movement indicates upward direction" if direction is Direction.LONG else "Deterministic test movement indicates downward direction"
        if not valid:
            reason += "; observed movement does not meet minimum reward/risk"
        return Opportunity(
            symbol=context.symbol,
            timestamp=context.timestamp,
            direction=direction,
            score=abs(delta) / first.open,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            risk_per_share=risk,
            reward_per_share=reward,
            reward_risk_ratio=ratio,
            reasons=(reason, "Temporary test analysis only; not a trading strategy"),
            valid=valid,
        )


def _none(context: AnalysisContext, reason: str) -> Opportunity:
    return Opportunity(
        symbol=context.symbol,
        timestamp=context.timestamp,
        direction=Direction.NONE,
        score=0.0,
        entry_price=None,
        stop_price=None,
        target_price=None,
        risk_per_share=None,
        reward_per_share=None,
        reward_risk_ratio=None,
        reasons=(reason, "Temporary test analysis only; not a trading strategy"),
        valid=False,
    )
