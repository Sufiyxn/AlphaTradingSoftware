from datetime import date, datetime, timezone

from backend.strategies.models import Direction

from .models import PortfolioSnapshot, Position


class Portfolio:
    """In-memory portfolio ledger shared by simulated adapters and paper trading."""

    def __init__(self, starting_cash: float = 100_000.0):
        if starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
        self.cash = float(starting_cash)
        self._positions: dict[str, Position] = {}
        self.realized_pnl = 0.0
        self.daily_pnl = 0.0
        self.trade_count = 0
        self._day: date | None = None

    @property
    def positions(self) -> tuple[Position, ...]:
        return tuple(self._positions[key] for key in sorted(self._positions))

    def open_position(self, symbol: str, direction: Direction, quantity: int, price: float, timestamp: datetime | None = None) -> Position:
        symbol = symbol.strip().upper()
        if not symbol or direction not in (Direction.LONG, Direction.SHORT) or quantity <= 0 or price <= 0:
            raise ValueError("invalid position opening parameters")
        timestamp = timestamp or datetime.now(timezone.utc)
        existing = self._positions.get(symbol)
        if existing and existing.direction is not direction:
            raise ValueError("cannot net opposite directions in one position")
        total_quantity = quantity + (existing.quantity if existing else 0)
        average = ((existing.average_entry_price * existing.quantity + price * quantity) / total_quantity) if existing else price
        position = Position(symbol, direction, total_quantity, average, price, existing.opened_at if existing else timestamp)
        self._positions[symbol] = position
        sign = 1 if direction is Direction.LONG else -1
        # Longs debit cash; shorts reserve sale proceeds while exposure remains gross notional.
        self.cash -= sign * quantity * price
        self._roll_day(timestamp)
        return position

    def update_price(self, symbol: str, price: float) -> Position:
        if price <= 0:
            raise ValueError("price must be positive")
        position = self._positions[symbol.strip().upper()]
        updated = Position(position.symbol, position.direction, position.quantity, position.average_entry_price, price, position.opened_at)
        self._positions[position.symbol] = updated
        return updated

    def close_position(self, symbol: str, quantity: int | None = None, price: float | None = None, *, fees: float = 0.0, timestamp: datetime | None = None) -> float:
        symbol = symbol.strip().upper()
        position = self._positions[symbol]
        quantity = position.quantity if quantity is None else quantity
        price = position.market_price if price is None else price
        if quantity <= 0 or quantity > position.quantity or price <= 0 or fees < 0:
            raise ValueError("invalid close parameters")
        sign = 1 if position.direction is Direction.LONG else -1
        pnl = (price - position.average_entry_price) * sign * quantity - fees
        self.cash += sign * quantity * price - fees
        self.realized_pnl += pnl
        self.daily_pnl += pnl
        self.trade_count += 1
        timestamp = timestamp or datetime.now(timezone.utc)
        self._roll_day(timestamp)
        remaining = position.quantity - quantity
        if remaining:
            self._positions[symbol] = Position(symbol, position.direction, remaining, position.average_entry_price, position.market_price, position.opened_at)
        else:
            del self._positions[symbol]
        return pnl

    def snapshot(self) -> PortfolioSnapshot:
        positions = self.positions
        unrealized = sum(position.unrealized_pnl for position in positions)
        exposure = sum(position.exposure for position in positions)
        equity = self.cash + sum((1 if p.direction is Direction.LONG else -1) * p.quantity * p.market_price for p in positions)
        return PortfolioSnapshot(self.cash, equity, max(0.0, self.cash), exposure, self.realized_pnl, unrealized,
                                 self.daily_pnl, self.trade_count, positions)

    def _roll_day(self, timestamp: datetime) -> None:
        day = timestamp.date()
        if self._day is not None and day != self._day:
            self.daily_pnl = 0.0
        self._day = day
