from typing import Protocol, Sequence

from backend.market_data.models import Bar
from backend.strategies.models import TradeSetup

from .models import SetupAnalysis


class AIAnalysisProvider(Protocol):
    """Advisory provider only; it has no execution or risk-control methods."""

    def analyze(self, setup: TradeSetup, market_context: Sequence[Bar] = ()) -> SetupAnalysis: ...
