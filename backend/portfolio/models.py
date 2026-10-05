from dataclasses import dataclass
from datetime import datetime

from backend.strategies.models import Direction


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    direction: Direction
    quantity: int
    average_entry_price: float
    market_price: float
    opened_at: datetime

    @property
    def exposure(self) -> float:
        return self.quantity * self.market_price

    @property
    def unrealized_pnl(self) -> float:
        sign = 1 if self.direction is Direction.LONG else -1
        return (self.market_price - self.average_entry_price) * sign * self.quantity


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    cash: float
    equity: float
    available_capital: float
    exposure: float
    realized_pnl: float
    unrealized_pnl: float
    daily_pnl: float
    trade_count: int
    positions: tuple[Position, ...]
