from math import isfinite
from typing import Sequence

from backend.market_data.models import Bar
from backend.strategies.models import TradeSetup

from .interface import AIAnalysisProvider
from .models import AIRecommendation, SetupAnalysis


class MockAIAnalysisProvider:
    """Local deterministic setup reviewer; requires no API key or network."""

    def analyze(self, setup: TradeSetup, market_context: Sequence[Bar] = ()) -> SetupAnalysis:
        ratio = setup.reward_risk_ratio
        score = max(0.0, min(1.0, 0.5 + (ratio - 2.0) * 0.1)) if isfinite(ratio) else 0.0
        warnings = () if ratio >= 2.0 else ("reward/risk is below 2:1",)
        if market_context and market_context[-1].symbol.strip().upper() != setup.symbol.strip().upper():
            warnings += ("market context symbol does not match setup",)
        factors = (f"{setup.direction.value} setup from {setup.strategy_name}", f"reward/risk {ratio:.2f}")
        notes = (f"Reviewed {len(market_context)} normalized market bars",)
        recommendation = AIRecommendation.REJECT if warnings else AIRecommendation.SUPPORT
        assessment = "Setup structure supports review" if not warnings else "Setup has review warnings"
        return SetupAnalysis(setup.symbol.strip().upper(), setup.timestamp, setup.strategy_name,
                             assessment, score, factors, warnings, notes, recommendation)


AIProvider = AIAnalysisProvider
