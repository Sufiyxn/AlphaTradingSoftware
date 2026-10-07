from datetime import date, datetime, timezone

from backend.strategies.models import Direction

from .models import AWAITING_FIRST_QUOTE, PortfolioSnapshot, Position, PriceState


class Portfolio:
    """In-memory portfolio ledger shared by simulated adapters and paper trading.

    A position carries a market price only when a market-data provider has
    actually observed one. Nothing here derives a current price from an entry
    price, a signal, or an arithmetic guess.
    """

    def __init__(self, starting_cash: float = 100_000.0, *, clock=None):
        if starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
        self.cash = float(starting_cash)
        self._positions: dict[str, Position] = {}
        self.realized_pnl = 0.0
        self.daily_pnl = 0.0
        self.trade_count = 0
        self._day: date | None = None
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def positions(self) -> tuple[Position, ...]:
        return tuple(self._positions[key] for key in sorted(self._positions))

    def open_position(self, symbol: str, direction: Direction, quantity: int, price: float,
                      timestamp: datetime | None = None) -> Position:
        symbol = symbol.strip().upper()
        if not symbol or direction not in (Direction.LONG, Direction.SHORT) or quantity <= 0 or price <= 0:
            raise ValueError("invalid position opening parameters")
        timestamp = timestamp or self._clock()
        existing = self._positions.get(symbol)
        if existing and existing.direction is not direction:
            raise ValueError("cannot net opposite directions in one position")
        total_quantity = quantity + (existing.quantity if existing else 0)
        average = ((existing.average_entry_price * existing.quantity + price * quantity) / total_quantity) if existing else price
        if existing:
            market_price, price_as_of, price_state, price_reason = (
                existing.market_price, existing.price_as_of, existing.price_state, existing.price_reason)
        else:
            # Entry price is what was paid, not what the market is worth now.
            market_price, price_as_of, price_state, price_reason = (
                None, None, PriceState.UNPRICED, AWAITING_FIRST_QUOTE)
        position = Position(symbol, direction, total_quantity, average, market_price,
                            existing.opened_at if existing else timestamp,
                            price_as_of=price_as_of, price_state=price_state, price_reason=price_reason)
        self._positions[symbol] = position
        sign = 1 if direction is Direction.LONG else -1
        # Longs debit cash; shorts reserve sale proceeds while exposure remains gross notional.
        self.cash -= sign * quantity * price
        self._roll_day(timestamp)
        return position

    def restore_position(self, symbol: str, direction: Direction, quantity: int, entry_price: float,
                         opened_at: datetime, *, reason: str) -> Position:
        """Rebuild an open position from persisted history without moving cash.

        Used by journal reconciliation so a restart restores state instead of
        silently diverging from the recorded trade book. The restored position
        starts UNPRICED; only a real quote can mark it.
        """
        symbol = symbol.strip().upper()
        if not symbol or direction not in (Direction.LONG, Direction.SHORT) or quantity <= 0 or entry_price <= 0:
            raise ValueError("invalid position restoration parameters")
        if symbol in self._positions:
            raise ValueError(f"position for {symbol} already exists")
        position = Position(symbol, direction, quantity, entry_price, None, opened_at,
                            price_state=PriceState.UNPRICED, price_reason=reason)
        self._positions[symbol] = position
        return position

    def update_price(self, symbol: str, price: float, *, timestamp: datetime,
                     state: PriceState = PriceState.FRESH, reason: str | None = None) -> Position:
        """Mark a position with a real observed price and when it was observed."""
        if price <= 0:
            raise ValueError("price must be positive")
        if timestamp is None:
            raise ValueError("a real price must carry the time it was observed")
        position = self._positions[symbol.strip().upper()]
        updated = Position(position.symbol, position.direction, position.quantity,
                           position.average_entry_price, price, position.opened_at,
                           price_as_of=timestamp, price_state=state, price_reason=reason)
        self._positions[position.symbol] = updated
        return updated

    def mark_unpriced(self, symbol: str, *, reason: str, timestamp: datetime | None = None) -> Position:
        """Drop the market price when no usable real quote exists any more."""
        if not reason:
            raise ValueError("an unpriced position must record why it cannot be valued")
        position = self._positions[symbol.strip().upper()]
        updated = Position(position.symbol, position.direction, position.quantity,
                           position.average_entry_price, None, position.opened_at,
                           price_as_of=timestamp, price_state=PriceState.UNPRICED, price_reason=reason)
        self._positions[position.symbol] = updated
        return updated

    def close_position(self, symbol: str, quantity: int | None = None, price: float | None = None,
                       *, fees: float = 0.0, timestamp: datetime | None = None) -> float:
        symbol = symbol.strip().upper()
        position = self._positions[symbol]
        quantity = position.quantity if quantity is None else quantity
        price = position.market_price if price is None else price
        if price is None:
            raise ValueError(
                f"{symbol} has no real market price; an explicit exit price is required "
                "before an unpriced position can be closed")
        if quantity <= 0 or quantity > position.quantity or price <= 0 or fees < 0:
            raise ValueError("invalid close parameters")
        sign = 1 if position.direction is Direction.LONG else -1
        pnl = (price - position.average_entry_price) * sign * quantity - fees
        self.cash += sign * quantity * price - fees
        self.realized_pnl += pnl
        self.daily_pnl += pnl
        self.trade_count += 1
        timestamp = timestamp or self._clock()
        self._roll_day(timestamp)
        remaining = position.quantity - quantity
        if remaining:
            self._positions[symbol] = Position(
                symbol, position.direction, remaining, position.average_entry_price,
                position.market_price, position.opened_at, price_as_of=position.price_as_of,
                price_state=position.price_state, price_reason=position.price_reason)
        else:
            del self._positions[symbol]
        return pnl

    def restore_ledger(self, *, cash: float, realized_pnl: float = 0.0, daily_pnl: float = 0.0,
                       trade_count: int = 0, day: date | None = None) -> None:
        """Replace the in-memory ledger with values rebuilt from persisted history."""
        if cash < 0:
            raise ValueError("restored cash cannot be negative")
        self.cash = float(cash)
        self.realized_pnl = float(realized_pnl)
        self.daily_pnl = float(daily_pnl)
        self.trade_count = int(trade_count)
        self._day = day

    def snapshot(self) -> PortfolioSnapshot:
        positions = self.positions
        priced = [position for position in positions if position.market_price is not None]
        unpriced_count = len(positions) - len(priced)
        unrealized = sum(position.unrealized_pnl for position in priced if position.unrealized_pnl is not None)
        exposure = sum(position.exposure for position in positions)
        # Equity only admits positions carrying a real price; an unpriced position
        # contributes neither value nor P&L until a provider marks it.
        equity = self.cash + sum(
            (1 if position.direction is Direction.LONG else -1) * position.quantity * position.market_price
            for position in priced)
        return PortfolioSnapshot(
            self.cash, equity, max(0.0, self.cash), exposure, self.realized_pnl, unrealized,
            self.daily_pnl, self.trade_count, positions,
            valuation_status="COMPLETE" if unpriced_count == 0 else "PARTIAL",
            priced_position_count=len(priced), unpriced_position_count=unpriced_count)

    def _roll_day(self, timestamp: datetime) -> None:
        day = timestamp.date()
        if self._day is not None and day != self._day:
            self.daily_pnl = 0.0
        self._day = day
