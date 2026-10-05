from typing import Sequence

from backend.market_data.models import Bar
from backend.strategies.models import TradeSetup

from .interface import AIAnalysisProvider
from .models import SetupAnalysis


class AIAnalysisService:
    """Validate advisory analysis output before exposing it to consumers."""

    def __init__(self, provider: AIAnalysisProvider):
        self.provider = provider

    def analyze(self, setup: TradeSetup, market_context: Sequence[Bar] = ()) -> SetupAnalysis:
        result = self.provider.analyze(setup, market_context)
        if result.symbol.strip().upper() != setup.symbol.strip().upper():
            raise ValueError("AI provider returned analysis for an unexpected symbol")
        if not 0.0 <= result.score <= 1.0:
            raise ValueError("AI analysis score must be in [0, 1]")
        return result
