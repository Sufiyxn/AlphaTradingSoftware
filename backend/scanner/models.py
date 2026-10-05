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


@dataclass(frozen=True, slots=True)
class SymbolResult:
    symbol: str
    opportunity: Opportunity | None
    error: str | None = None
