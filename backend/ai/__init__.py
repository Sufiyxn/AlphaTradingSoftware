"""Advisory-only analysis interfaces and local deterministic provider."""

from .interface import AIAnalysisProvider
from .models import AIRecommendation, SetupAnalysis
from .provider import MockAIAnalysisProvider
from .service import AIAnalysisService

__all__ = ["AIAnalysisProvider", "AIAnalysisService", "AIRecommendation", "MockAIAnalysisProvider", "SetupAnalysis"]
