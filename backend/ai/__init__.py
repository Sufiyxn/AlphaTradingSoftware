"""Advisory-only analysis interfaces and local deterministic provider."""

from .interface import AIAnalysisProvider
from .models import AIRecommendation, SetupAnalysis
from .provider import LocalRuleBasedAnalysisProvider, MockAIAnalysisProvider
from .service import AIAnalysisService

__all__ = [
    "AIAnalysisProvider", "AIAnalysisService", "AIRecommendation",
    "LocalRuleBasedAnalysisProvider", "MockAIAnalysisProvider", "SetupAnalysis",
]
