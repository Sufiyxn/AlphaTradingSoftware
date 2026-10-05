from dataclasses import dataclass
from datetime import timedelta

from backend.scanner.universe import SymbolUniverse


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    universe: SymbolUniverse
    scan_interval: timedelta = timedelta(seconds=30)
    history_lookback: timedelta = timedelta(days=90)
    max_candidate_price: float = 100.0

    def __post_init__(self) -> None:
        if self.scan_interval <= timedelta(0) or self.history_lookback <= timedelta(0):
            raise ValueError("scan interval and history lookback must be positive")
        if self.max_candidate_price <= 0:
            raise ValueError("maximum candidate price must be positive")
