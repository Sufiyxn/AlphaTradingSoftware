from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Quote:
    """One observed trade price.

    ``bid``/``ask`` are optional because many read-only trade endpoints do not
    publish a book; they stay ``None`` rather than being estimated.
    """

    symbol: str
    timestamp: datetime
    price: float
    volume: float
    bid: float | None = None
    ask: float | None = None
