"""Provider-agnostic normalized trading signals.

``NormalizedSignal`` is the only signal representation the trading system
consumes. Provider payloads (TradingView alerts today, other sources later)
are mapped to this model at the integration boundary; nothing downstream reads
a provider-specific field name, and no provider field is ever treated as a
market price.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from backend.strategies.models import Direction, TradeSetup


class SignalAction(str, Enum):
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT = "EXIT"


class SignalValidationError(Exception):
    """A signal failed validation with a stable machine-readable reason."""

    def __init__(self, message: str, *, reason: str = "INVALID_PAYLOAD"):
        super().__init__(message)
        self.message = message
        self.reason = reason

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class NormalizedSignal:
    """One validated, provider-agnostic trading instruction."""

    source: str
    signal_id: str
    symbol: str
    action: SignalAction
    timestamp: datetime
    quantity: int | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    reported_price: float | None = None
    strategy: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.source:
            raise SignalValidationError("signal source is required", reason="INVALID_PAYLOAD")
        if not self.signal_id:
            raise SignalValidationError("signal id is required", reason="INVALID_PAYLOAD")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise SignalValidationError("signal timestamp must include a timezone",
                                        reason="INVALID_TIMESTAMP")

    @property
    def strategy_name(self) -> str:
        return f"{self.source}:{self.strategy}" if self.strategy else self.source

    @property
    def is_entry(self) -> bool:
        return self.action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT)

    @property
    def direction(self) -> Direction:
        if self.action is SignalAction.ENTER_LONG:
            return Direction.LONG
        if self.action is SignalAction.ENTER_SHORT:
            return Direction.SHORT
        return Direction.NONE

    def to_trade_setup(self) -> TradeSetup:
        """Build the canonical risk/execution input for an entry signal.

        Entry, stop, and target come from the validated signal only. Nothing is
        derived from a live price, and a missing level is an error rather than
        a guess. The reported alert price is entry metadata; it is never used as
        an ongoing market price.
        """
        if not self.is_entry:
            raise SignalValidationError("an exit signal has no trade setup",
                                        reason="UNSUPPORTED_ACTION")
        missing = [name for name, value in (("entry_price", self.entry_price),
                                            ("stop_price", self.stop_price),
                                            ("target_price", self.target_price))
                   if value is None]
        if missing:
            raise SignalValidationError(f"missing required field: {missing[0]}",
                                        reason="MISSING_FIELD")
        risk = abs(self.entry_price - self.stop_price)
        reward = abs(self.target_price - self.entry_price)
        return TradeSetup(
            symbol=self.symbol,
            direction=self.direction,
            entry_price=self.entry_price,
            stop_price=self.stop_price,
            target_price=self.target_price,
            risk_per_share=risk,
            reward_per_share=reward,
            reward_risk_ratio=(reward / risk) if risk > 0 else 0.0,
            timestamp=self.timestamp,
            strategy_name=self.strategy_name,
            score=1.0,
            explanation=f"normalized {self.source} signal {self.signal_id}",
        )
