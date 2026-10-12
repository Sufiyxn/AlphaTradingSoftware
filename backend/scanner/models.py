from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class Opportunity:
    symbol: str
    timestamp: datetime
    direction: Direction
    score: float
    entry_price: float | None
    stop_price: float | None
    target_price: float | None
    risk_per_share: float | None
    reward_per_share: float | None
    reward_risk_ratio: float | None
    reasons: tuple[str, ...]
    valid: bool
    #: The bar timeframe this opportunity was computed from (``1min``, ``1day``,
    #: …). ``None`` when the data source did not report one. Freshness limits
    #: are judged against this frame's spacing, so a daily bar is never measured
    #: with a five-minute staleness window.
    timeframe: str | None = None


@dataclass(frozen=True, slots=True)
class SymbolResult:
    symbol: str
    opportunity: Opportunity | None
    error: str | None = None
    #: Frame actually used for this symbol's bars, its count, and the newest
    #: bar time - the evidence behind the row's state.
    timeframe: str | None = None
    bar_count: int = 0
    latest_bar_at: datetime | None = None
