from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class AIRecommendation(str, Enum):
    SUPPORT = "SUPPORT"
    CAUTION = "CAUTION"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class SetupAnalysis:
    symbol: str
    timestamp: datetime
    strategy_name: str
    setup_assessment: str
    score: float
    supporting_factors: tuple[str, ...]
    warnings: tuple[str, ...]
    market_context_notes: tuple[str, ...]
    recommendation: AIRecommendation
