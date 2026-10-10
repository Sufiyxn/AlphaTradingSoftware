from dataclasses import dataclass
from datetime import timedelta

from backend.scanner.universe import SymbolUniverse


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    universe: SymbolUniverse
    scan_interval: timedelta = timedelta(seconds=30)
    history_lookback: timedelta = timedelta(days=90)
    max_candidate_price: float = 100.0
    #: Phase 12: how old the newest bar may be before a candidate is refused
    #: as stale. ``None`` disables the gate (the direct unit-test default);
    #: the runtime always wires it to the provider's freshness window.
    #: ``data_interval`` is the spacing of one candle for the connected
    #: provider, so a daily frame is never judged against a five-minute
    #: threshold.
    max_data_age: timedelta | None = None
    data_interval: timedelta = timedelta(seconds=60)

    def __post_init__(self) -> None:
        if self.scan_interval <= timedelta(0) or self.history_lookback <= timedelta(0):
            raise ValueError("scan interval and history lookback must be positive")
        if self.max_candidate_price <= 0:
            raise ValueError("maximum candidate price must be positive")
        if self.data_interval <= timedelta(0):
            raise ValueError("data interval must be positive")
        if self.max_data_age is not None and self.max_data_age <= timedelta(0):
            raise ValueError("maximum data age must be positive")

    @property
    def data_age_limit(self) -> float | None:
        """Seconds a bar may age before it is stale, or None when disabled."""
        if self.max_data_age is None:
            return None
        return max(self.max_data_age.total_seconds(),
                   2.0 * self.data_interval.total_seconds())
