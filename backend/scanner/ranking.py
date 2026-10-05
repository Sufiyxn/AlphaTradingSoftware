from collections.abc import Iterable

from .models import Opportunity


def filter_and_rank(opportunities: Iterable[Opportunity], minimum_reward_risk: float = 2.0) -> tuple[Opportunity, ...]:
    """Return valid candidates meeting the hard reward/risk floor, strongest first."""
    eligible = (
        item for item in opportunities
        if item.valid
        and item.direction.value != "NONE"
        and item.reward_risk_ratio is not None
        and item.reward_risk_ratio >= minimum_reward_risk
    )
    return tuple(sorted(eligible, key=lambda item: (-item.score, item.symbol)))
