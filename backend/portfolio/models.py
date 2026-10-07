from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from backend.strategies.models import Direction


class PriceState(str, Enum):
    """How a paper position's current price was obtained.

    FRESH  - a real quote observed inside the configured staleness window.
    STALE  - a real quote, but older than that window; never treated as fresh.
    UNPRICED - no usable real price; ``market_price`` is ``None``.
    """

    FRESH = "FRESH"
    STALE = "STALE"
    UNPRICED = "UNPRICED"


AWAITING_FIRST_QUOTE = "awaiting the first real quote from a market-data provider"
RESTORED_AWAITING_QUOTE = "restored from the trade journal; awaiting a real quote"


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    direction: Direction
    quantity: int
    average_entry_price: float
    market_price: float | None
    opened_at: datetime
    price_as_of: datetime | None = None
    price_state: PriceState = PriceState.UNPRICED
    price_reason: str | None = None

    @property
    def priced(self) -> bool:
        """True only when a real market price has been observed for this position."""
        return self.market_price is not None

    @property
    def price_fresh(self) -> bool:
        return self.price_state is PriceState.FRESH

    @property
    def auto_exit_enabled(self) -> bool:
        """Automatic stop-loss/take-profit needs a fresh real price."""
        return self.price_state is PriceState.FRESH

    @property
    def exposure(self) -> float:
        """Gross notional. Uses the observed price when marked, otherwise the
        committed entry notional - a realized fact, never a market quote."""
        reference = self.average_entry_price if self.market_price is None else self.market_price
        return self.quantity * reference

    @property
    def unrealized_pnl(self) -> float | None:
        """Unrealized P&L, or ``None`` when there is no real price to value it with."""
        if self.market_price is None:
            return None
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
    valuation_status: str = "COMPLETE"
    priced_position_count: int = 0
    unpriced_position_count: int = 0
